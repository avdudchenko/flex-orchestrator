"""Write exported model values to a backend, and read them back (PLAN.md D3 step 4).

Every stored result keeps the **whole model as a snapshot**, two outputs of its
run (PLAN.md D5):

- ``model_values``, a ``table``: a UTC ``time`` column and one Float64 column
  per timeseries key, null where the model had no value;
- ``model_manifest``, a JSON document: for every exported key, in walk order,
  its kind, component, index, Pyomo unit and QUDT unit, the value of each
  scalar, and the rows of ``model_values`` a timeseries covers when it isn't
  every row (a subset of time such as ``startup``).

Reads rebuild :class:`~flex_orchestrator.export.ModelData` from the snapshot
and convert units with pint when asked.

Keys a user asks for (``streams=``) are **also** written as native Acquirium
variables, so they can be queried and fitted: a timeseries becomes a
``timeseries`` variable labelled by its key, a scalar a ``scalar`` variable.
Their declarations carry the QUDT unit (only when verified, since Acquirium
rejects a unit it can't resolve) and the metadata ``pyomo_unit``,
``component`` and ``index``. A native label keeps the unit it was first
declared with: a later run exporting it in another unit of the same dimensions
is converted, and one whose units don't convert raises.
"""

import fnmatch
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import polars as pl

from flex_orchestrator.backend.base import StorageBackend, Variable, to_rows, to_utc
from flex_orchestrator.errors import ExportError, StorageError, UnitError
from flex_orchestrator.export.model_data import ExportedSeries, ModelData
from flex_orchestrator.export.units import conversion_factor

MODEL_VALUES = "model_values"
"""str: The label of a result's ``table`` of timeseries values."""
MODEL_MANIFEST = "model_manifest"
"""str: The label of a result's JSON manifest."""
SNAPSHOT_SCHEMA = "flexorch-model/1"
TIME = "time"
"""str: The time column of ``model_values``."""


# The snapshot


def snapshot(data: ModelData) -> tuple[pl.DataFrame | None, dict[str, Any]]:
    """Turn exported values into the ``model_values`` table and its manifest.

    Args:
        data: The exported values.

    Returns:
        ``(table, manifest)``; the table is ``None`` when nothing is a
        timeseries.

    Raises:
        StorageError: If a timeseries has no datetimes, or a key is ``time``.
    """
    # A model's series share a few time axes (the main one, startup's t >= 1),
    # so each distinct axis is converted and placed once, not once per key.
    axes: dict[tuple, list] = {}
    axis_of = {}
    for series in data.timeseries():
        axis = tuple(series.timestamps or ())
        if axis not in axes:
            axes[axis] = _stamps(series)
        axis_of[series.key] = axis
    times = sorted({when for found in axes.values() for when in found})
    position = {when: i for i, when in enumerate(times)}
    # None: the axis is every row of the table.
    rows_of = {
        axis: None if found == times else [position[when] for when in found]
        for axis, found in axes.items()
    }
    columns: dict[str, list[float | None]] = {}
    entries = []
    for series in data:
        entry: dict[str, Any] = {
            "key": series.key,
            "kind": series.kind,
            "component": series.component,
            "index": series.index,
            "pyomo_unit": series.unit_pyomo,
            "qudt_unit": series.unit_qudt,
            "unit_verified": series.unit_verified,
        }
        if series.kind == "scalar":
            entry["value"] = series.values[0]
        else:
            rows = rows_of[axis_of[series.key]]
            if rows is None:
                columns[series.key] = list(series.values)
            else:
                entry["rows"] = rows
                column: list[float | None] = [None] * len(times)
                for row, value in zip(rows, series.values, strict=True):
                    column[row] = value
                columns[series.key] = column
        entries.append(entry)
    table = None
    if columns:
        table = pl.DataFrame(
            [pl.Series(TIME, times, dtype=pl.Datetime("us", "UTC"))]
            + [pl.Series(key, v, dtype=pl.Float64) for key, v in columns.items()]
        )
    return table, {"schema": SNAPSHOT_SCHEMA, "series": entries}


def read_snapshot(table: pl.DataFrame | None, manifest: Mapping[str, Any]) -> ModelData:
    """Rebuild :class:`ModelData` from a result's table and manifest.

    Raises:
        StorageError: If the manifest's schema isn't one this version reads.
    """
    if manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise StorageError(
            f"Can't read a model snapshot with schema {manifest.get('schema')!r}; "
            f"this version reads {SNAPSHOT_SCHEMA!r}."
        )
    times = (
        [to_utc(when) for when in table[TIME].to_list()] if table is not None else []
    )
    series = []
    for entry in manifest["series"]:
        if entry["kind"] == "scalar":
            timestamps, values = None, [entry["value"]]
        else:
            column = table[entry["key"]].to_list()
            rows = entry.get("rows", range(len(times)))
            timestamps = [times[row] for row in rows]
            values = [column[row] for row in rows]
        series.append(
            ExportedSeries(
                key=entry["key"],
                kind=entry["kind"],
                component=entry["component"],
                index=list(entry["index"]),
                values=values,
                timestamps=timestamps,
                unit_pyomo=entry["pyomo_unit"],
                unit_qudt=entry["qudt_unit"],
                unit_verified=entry["unit_verified"],
                raw_index=tuple(entry["index"]),
            )
        )
    return ModelData(series)


def convert(data: ModelData, units: Mapping[str, str]) -> ModelData:
    """Convert values to other Pyomo units, with pint.

    Args:
        data: The values.
        units: ``{key or component path: Pyomo unit}``; a component path
            converts all of its keys.

    Returns:
        The values, with ``unit_pyomo`` set to the new unit and ``unit_qudt``
        cleared where converted.

    Raises:
        ExportError: If ``units`` names a key or component that isn't there.
        UnitError: If a value has no unit, or its unit doesn't convert.
    """
    known = set(data.keys()) | {series.component for series in data}
    unknown = sorted(set(units) - known)
    if unknown:
        raise ExportError(f"units= names keys that weren't stored: {unknown}.")
    converted = []
    for series in data:
        target = units.get(series.key, units.get(series.component))
        if target is None or target == series.unit_pyomo:
            converted.append(series)
            continue
        if series.unit_pyomo is None:
            raise UnitError(f"{series.key!r} has no unit to convert from.")
        factor = conversion_factor(series.unit_pyomo, target)
        converted.append(
            replace(
                series,
                values=[None if v is None else v * factor for v in series.values],
                unit_pyomo=target,
                unit_qudt=None,
                unit_verified=False,
            )
        )
    return ModelData(converted)


def select(data: ModelData, patterns: Iterable[str]) -> ModelData:
    """Pick the values a list of keys, component paths or globs names.

    A pattern matches a key or a component path exactly, or as a glob
    (``"costing.*"``).

    Raises:
        ExportError: If a pattern matches nothing.
    """
    patterns = list(patterns)
    chosen, used = [], set()
    for series in data:
        for pattern in patterns:
            if pattern in (series.key, series.component) or any(
                fnmatch.fnmatchcase(name, pattern)
                for name in (series.key, series.component)
            ):
                chosen.append(series)
                used.add(pattern)
                break
    unused = [p for p in patterns if p not in used]
    if unused:
        raise ExportError(f"streams= patterns match no exported key: {unused}.")
    return ModelData(chosen)


# Native streams


@dataclass(frozen=True)
class _Write:
    series: ExportedSeries
    declared: Variable | None
    value: Any


def plan_writes(backend: StorageBackend, study: str, data: ModelData) -> list[_Write]:
    """Check values against the study's declarations and prepare native writes.

    Nothing is written, so a run is never started for values that can't be
    stored.

    Args:
        backend: The store.
        study: The study the run will belong to.
        data: The values to write natively.

    Returns:
        One write per value, in walk order, with values converted to the
        declared units. A timeseries leaves out points with no value.

    Raises:
        StorageError: If a key is declared with another kind, or a timeseries
            has no datetimes.
        UnitError: If a value's unit doesn't convert to its declared unit.
    """
    writes = []
    for series in data:
        declared = backend.variable(study, series.key)
        factor = 1.0
        if declared is not None:
            if declared.kind != series.kind:
                raise StorageError(
                    f"{series.key!r} is stored as a {declared.kind} in study "
                    f"{study!r}, but this model exports it as a {series.kind}."
                )
            factor = _factor(series, declared.metadata.get("pyomo_unit"))
        values = [None if v is None else v * factor for v in series.values]
        if series.kind == "timeseries":
            _stamps(series)
            value: Any = to_rows(series.timestamps, values)
        else:
            value = values[0]
        writes.append(_Write(series, declared, value))
    return writes


def declare(backend: StorageBackend, study: str, writes: Sequence[_Write]) -> None:
    """Declare every native value that isn't declared yet."""
    for write in writes:
        if write.declared is not None:
            continue
        series = write.series
        backend.declare(
            study,
            series.key,
            role="output",
            kind=series.kind,
            unit=series.unit_qudt if series.unit_verified else None,
            metadata={
                "pyomo_unit": series.unit_pyomo,
                "component": series.component,
                "index": json.dumps(series.index),
            },
        )


def record(backend: StorageBackend, run_id: str, writes: Sequence[_Write]) -> None:
    """Record the prepared native values in a running run."""
    for write in writes:
        backend.record(run_id, write.series.key, write.value)


def _factor(series: ExportedSeries, declared_unit: str | None) -> float:
    if declared_unit == series.unit_pyomo:
        return 1.0
    if declared_unit is None or series.unit_pyomo is None:
        raise UnitError(
            f"{series.key!r} was declared in {declared_unit or 'no unit'}, but this "
            f"model exports it in {series.unit_pyomo or 'no unit'}."
        )
    try:
        return conversion_factor(series.unit_pyomo, declared_unit)
    except UnitError as exc:
        raise UnitError(f"{series.key!r}: {exc}") from None


def _stamps(series: ExportedSeries) -> list:
    if series.key == TIME:
        raise StorageError(f"A key can't be named {TIME!r}; it is the time column.")
    try:
        return [to_utc(when) for when in series.timestamps or []]
    except StorageError:
        raise StorageError(
            f"Timeseries {series.key!r} has no datetimes (its time points are "
            f"{series.timestamps[:3]!r}...). Pass datetime_index= to give each "
            "time point a datetime."
        ) from None
