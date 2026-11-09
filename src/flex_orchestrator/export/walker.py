"""Walk a Pyomo model and export its values with units (PLAN.md D3).

Each component is classified by the Set it is indexed by:

- not indexed -> one scalar, keyed by its path;
- indexed by time only -> one timeseries, keyed by its path;
- indexed by time x custom sets, e.g. ``(unit, t)`` -> one timeseries per
  custom index value, keyed ``path[index]``;
- indexed by custom sets only -> one scalar per element, keyed by the element's
  Pyomo name (``costing.capital_cost[pump1]``).

"Time" means a registered time Set, compared **by identity**: flex-pse's time
set is just the integers ``0..N-1``, so any other integer Set of that length
would match by value. This module depends only on pyomo; flex-pse's TimeBlock is
found by its ``time_index`` / ``datetime_index`` attributes.
"""

import math
import warnings
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import pyomo.environ as pyo
from pyomo.core.base.component_namer import index_repr
from pyomo.core.base.set import SetProduct
from pyomo.environ import units as pyunits

from flex_orchestrator.errors import ExportError
from flex_orchestrator.export.model_data import (
    ExportedSeries,
    ModelData,
    path_matches,
)
from flex_orchestrator.export.units import UnitMapper

_CTYPES = (pyo.Var, pyo.Expression, pyo.Param)
_JSON_SCALARS = (str, int, float, bool, type(None))


def export_model_data(
    model: pyo.Block,
    keys: Iterable[str] | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    time_sets: Sequence[pyo.Set] | None = None,
    datetime_index: Sequence[Any] | None = None,
    unit_mapper: UnitMapper | None = None,
    max_scalars_per_component: int = 10_000,
) -> ModelData:
    """Export a model's values, classified by index and with their units.

    What is exported:

    - ``keys`` only, if given without ``include``;
    - otherwise every active Var, Expression and mutable Param, filtered by
      ``include`` / ``exclude`` glob patterns on the component path, plus any
      ``keys``. Components with a private (``_``-prefixed) name are skipped
      unless named in ``keys``.

    Args:
        model: The model (or block) to walk. Paths are relative to it.
        keys: Exact component paths, or element paths such as ``"x[pump1]"``.
            A key always exports, whatever its size; a key that names nothing
            raises.
        include: Glob patterns a component path must match, e.g. ``"costing.*"``.
        exclude: Glob patterns that drop a component.
        time_sets: The time Sets, main one first. Further entries are subsets of
            the main one, e.g. every point but the first; a subset counts as
            time only if listed here. A later Set that is its own block's
            ``time_index`` starts a new axis, for a model with several time
            blocks, and each block's values get that block's timestamps. When
            omitted, the main time Set of the model's TimeBlock is used, without
            subsets; for a flex-pse model, pass
            ``flex_orchestrator.flexpse.time_sets(model)`` (the default ``time_sets``
            hook) to include the subsets flex-pse builds.
        datetime_index: One timestamp per element of the main time Set, in
            order. When omitted, taken from the block that holds the main time
            Set (flex-pse's TimeBlock); without either, the raw time values are
            used as timestamps.
        unit_mapper: Maps Pyomo units to QUDT; a lenient :class:`UnitMapper` by
            default.
        max_scalars_per_component: A component indexed by custom sets only and
            with more elements than this is skipped with a warning, unless it is
            named in ``keys``.

    Returns:
        The exported values.

    Raises:
        ExportError: If a key names nothing (or not a Var, Expression or
            Param), the time sets are ambiguous or inconsistent, two values get
            the same key, or a named component has a non-numeric value.
        UnitError: If ``unit_mapper`` is strict and a unit doesn't map.

    Example:
        >>> data = export_model_data(model, include=["costing.*"])  # doctest: +SKIP
        >>> data["costing.aggregate_electrical_power"].unit_qudt  # doctest: +SKIP
        'KiloW'
    """
    timeline = _Timeline.build(model, time_sets, datetime_index)
    mapper = unit_mapper or UnitMapper()
    exported: list[ExportedSeries] = []
    seen: dict[str, str] = {}
    for component, explicit, only in _select(model, keys, include, exclude):
        for series in _export_component(
            model,
            component,
            timeline,
            mapper,
            explicit,
            only,
            max_scalars_per_component,
        ):
            if series.key in seen:
                raise ExportError(
                    f"Two values export as {series.key!r} (from {seen[series.key]!r} "
                    f"and {series.component!r}); their index values format the same."
                )
            seen[series.key] = series.component
            exported.append(series)
    return ModelData(exported)


class _Timeline:
    """The registered time sets and how their values map to timestamps."""

    def __init__(self, time_sets: Sequence[pyo.Set], datetime_index: Sequence | None):
        self.sets = list(time_sets)
        self.positions: dict[Any, int] = {}
        self.datetime_index = None
        if not self.sets:
            return
        main = self.sets[0]
        self.positions = {value: i for i, value in enumerate(main)}
        for subset in self.sets[1:]:
            missing = [v for v in subset if v not in self.positions]
            if missing:
                raise ExportError(
                    f"Time set {subset.name!r} isn't a subset of {main.name!r}: "
                    f"{missing[:3]} aren't in it."
                )
        if datetime_index is not None:
            self.datetime_index = list(datetime_index)
            if len(self.datetime_index) != len(main):
                raise ExportError(
                    f"datetime_index has {len(self.datetime_index)} entries but "
                    f"time set {main.name!r} has {len(main)}."
                )

    def axis(self, index_set: Any) -> "_Timeline":
        return self

    @classmethod
    def build(cls, model, time_sets, datetime_index) -> "_Timeline | _Timelines":
        axes = _split_axes(time_sets or [])
        if len(axes) > 1:
            if datetime_index is not None:
                raise ExportError(
                    "datetime_index= can't be used with several time blocks; each "
                    "block's own datetimes are used."
                )
            return _Timelines(
                [cls(axis, axis[0].parent_block().datetime_index) for axis in axes]
            )
        if time_sets is not None:
            if datetime_index is None and time_sets:
                # A flex-pse time set sits on its TimeBlock, next to the datetimes.
                owner = time_sets[0].parent_block()
                datetime_index = getattr(owner, "datetime_index", None)
            return cls(time_sets, datetime_index)
        blocks = [
            block
            for block in (
                model,
                *model.component_data_objects(pyo.Block, descend_into=True),
            )
            if isinstance(getattr(block, "time_index", None), pyo.Set)
            and getattr(block, "datetime_index", None) is not None
        ]
        if len(blocks) > 1:
            names = [block.name for block in blocks]
            raise ExportError(
                f"The model has several time blocks {names}; pass time_sets= and "
                "datetime_index= to say which is time."
            )
        if not blocks:
            return cls([], None)
        block = blocks[0]
        index = block.datetime_index if datetime_index is None else datetime_index
        return cls([block.time_index], index)

    def is_time(self, index_set: Any) -> bool:
        return any(index_set is time_set for time_set in self.sets)

    def timestamp(self, value: Any) -> Any:
        if self.datetime_index is None:
            return value
        return self.datetime_index[self.positions[value]]


class _Timelines:
    """One :class:`_Timeline` per time block of a model with several."""

    def __init__(self, axes: list[_Timeline]) -> None:
        self.axes = axes

    def is_time(self, index_set: Any) -> bool:
        return any(axis.is_time(index_set) for axis in self.axes)

    def axis(self, index_set: Any) -> _Timeline:
        factors = [index_set, *_factors(index_set)]
        return next(a for a in self.axes if any(a.is_time(f) for f in factors))


def _split_axes(time_sets: Sequence[pyo.Set]) -> list[list[pyo.Set]]:
    """Group time Sets by axis: a block's own ``time_index`` starts a new one."""
    axes: list[list[pyo.Set]] = []
    for time_set in time_sets:
        same_model = axes and time_set.model() is axes[0][0].model()
        owner = time_set.parent_block()
        if not axes or (same_model and getattr(owner, "time_index", None) is time_set):
            axes.append([])
        axes[-1].append(time_set)
    return axes


def _select(model, keys, include, exclude) -> Iterator[tuple[Any, bool, set | None]]:
    """Yield ``(component, named in keys, only these indices)`` to export."""
    done: set[int] = set()
    for key in keys or ():
        found = model.find_component(key)
        if found is None:
            raise ExportError(f"Key {key!r} names no component of the model.")
        component = found.parent_component()
        if component.ctype not in _CTYPES:
            raise ExportError(
                f"Key {key!r} is a {component.ctype.__name__}; only Vars, "
                "Expressions and Params can be exported."
            )
        only = None if found is component else {found.index()}
        done.add(id(component))
        yield component, True, only
    if keys is not None and include is None:
        return
    include, exclude = list(include or ()), list(exclude or ())
    for component in model.component_objects(_CTYPES, active=True, descend_into=True):
        if id(component) in done:
            continue
        if component.ctype is pyo.Param and not component.mutable:
            continue
        path = component.getname(fully_qualified=True, relative_to=model)
        if any(part.startswith("_") for part in path.split(".")):
            continue
        if include and not any(path_matches(path, p) for p in include):
            continue
        if any(path_matches(path, p) for p in exclude):
            continue
        yield component, False, None


def _export_component(
    model, component, timeline, mapper, explicit, only, limit
) -> Iterator[ExportedSeries]:
    path = component.getname(fully_qualified=True, relative_to=model)
    elements = [
        (index, data)
        for index, data in component.items()
        if only is None or index in only
    ]
    if not elements:
        return
    try:
        values = {index: _numeric(data) for index, data in elements}
    except ValueError:
        message = f"{path!r} has non-numeric values and can't be exported."
        if explicit:
            raise ExportError(message) from None
        warnings.warn(f"{message} Skipping it.", stacklevel=3)
        return
    mapping = mapper.map(_unit_of(path, elements[0][1]))

    def series(kind, key, index, points):
        return ExportedSeries(
            key=key,
            kind=kind,
            component=path,
            index=[_json_index(i) for i in index],
            values=[values[i] for i, _ in points],
            timestamps=None if kind == "scalar" else [t for _, t in points],
            unit_pyomo=mapping.pyomo,
            unit_qudt=mapping.qudt,
            unit_verified=mapping.verified,
            raw_index=tuple(index),
        )

    if not component.is_indexed():
        yield series("scalar", path, [], [(None, None)])
        return

    time_offset = _time_offset(component.index_set(), timeline)
    if time_offset is None:
        if len(elements) > limit and not explicit:
            warnings.warn(
                f"Skipping {path!r}: {len(elements)} elements is more than "
                f"max_scalars_per_component={limit}. Name it in keys= to export it.",
                stacklevel=3,
            )
            return
        for index, data in elements:
            raw = _as_tuple(index)
            key = data.getname(fully_qualified=True, relative_to=model)
            yield series("scalar", key, raw, [(index, None)])
        return

    axis = timeline.axis(component.index_set())
    groups: dict[tuple, list[tuple[Any, Any]]] = {}
    for index, _ in elements:
        raw = _as_tuple(index)
        t = raw[time_offset]
        custom = raw[:time_offset] + raw[time_offset + 1 :]
        groups.setdefault(custom, []).append((index, t))
    for custom, points in groups.items():
        points.sort(key=lambda point: axis.positions[point[1]])
        key = (
            path
            if not custom
            else path + index_repr(custom[0] if len(custom) == 1 else custom)
        )
        stamped = [(index, axis.timestamp(t)) for index, t in points]
        yield series("timeseries", key, custom, stamped)


def _time_offset(index_set, timeline) -> int | None:
    """Return where the time value sits in an index tuple, or ``None`` if untimed."""
    if timeline.is_time(index_set):
        return 0
    factors = _factors(index_set)
    hits = [i for i, factor in enumerate(factors) if timeline.is_time(factor)]
    if len(hits) != 1:
        return None
    dimens = [factor.dimen for factor in factors[: hits[0]]]
    if any(not isinstance(d, int) for d in dimens):
        return None
    return sum(dimens)


def _factors(index_set) -> list:
    """Flatten a (nested) Set product into its factor Sets."""
    if not isinstance(index_set, SetProduct):
        return [index_set]
    factors = []
    for subset in index_set.subsets(expand_all_set_operators=False):
        factors.extend(_factors(subset))
    return factors


def _numeric(data) -> float | None:
    """The element's value as a float; ``None`` for no value or NaN."""
    value = pyo.value(data, exception=False)
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise ValueError(value)
    value = float(value)
    return None if math.isnan(value) else value


def _unit_of(path: str, data) -> str | None:
    """The Pyomo unit string of one element; ``None`` if it can't be worked out."""
    try:
        unit = pyunits.get_units(data)
    except Exception as exc:
        warnings.warn(f"Can't work out the units of {path!r}: {exc}", stacklevel=4)
        return None
    return "dimensionless" if unit is None else str(unit)


def _as_tuple(index) -> tuple:
    return index if isinstance(index, tuple) else (index,)


def _json_index(value) -> Any:
    return value if isinstance(value, _JSON_SCALARS) else str(value)
