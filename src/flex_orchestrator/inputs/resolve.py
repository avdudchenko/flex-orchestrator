"""Turn ``model_inputs`` entries into build overrides (PLAN.md D21).

Runs in the main process, which holds the store; the values it returns are
plain data a Ray worker builds with. Each entry's selector is loaded with the
fit selectors (:func:`flex_orchestrator.fitting.selectors.load`), then:

1. its UTC timestamps go to the entry's ``timezone`` and lose it, since the
   model's time steps are the facility's naive wall clock;
2. the data is shifted by ``offset`` onto the model's horizon;
3. it is resampled onto the model's time steps and its gaps filled;
4. or, with ``reduce``, the horizon's data becomes one number.
"""

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from flex_orchestrator.errors import FitError, ModelInputError
from flex_orchestrator.fitting import selectors
from flex_orchestrator.fitting.models import Selector, Window
from flex_orchestrator.inputs.models import InputResample, ModelInput

# Data is read this far past each end of the horizon, so a gap at either end
# can be interpolated from the samples beyond it.
_MARGIN = pd.Timedelta(days=1)
_ALIGNMENT = ("timezone", "resample", "gaps", "limit", "reduce", "offset")


@dataclass
class Resolved:
    """What a list of entries set.

    Attributes:
        values: ``{model path: value}``, for the build hook: a list with one
            number per time point (or one number, for ``reduce``), as
            ``{"value", "units"}`` when the data has a unit.
        provenance: Per entry: the entry as sent, what its selector resolved
            to, the window read, how many steps were filled, and the values'
            hash.
    """

    values: dict[str, Any] = field(default_factory=dict)
    provenance: list[dict[str, Any]] = field(default_factory=list)


def resolve(
    orch: Any,
    inputs: Sequence[ModelInput],
    grid: pd.DatetimeIndex | None,
    swept: Sequence[ModelInput] = (),
    cache: dict | None = None,
) -> Resolved:
    """Resolve a task's entries against its model's time steps.

    Args:
        orch: The orchestrator, with the task's study selected.
        inputs: The study's (or one build's) entries.
        grid: The model's time steps (the ``time_grid`` hook); ``None`` for a
            model without a time axis, which takes only ``reduce`` entries.
        swept: Entries from a sweep or case; one replaces an ``inputs`` entry
            that sets the same path.
        cache: Selector data read so far, reused for an entry that reads the
            same data (a study passes one for all its tasks).

    Raises:
        ModelInputError: If an entry's data can't be read or lined up, or two
            entries of one list set the same path.
    """
    base, swept_found = _layer(orch, inputs, grid, cache), _layer(
        orch, swept, grid, cache
    )
    values = {**base.values, **swept_found.values}
    kept = []
    for record in base.provenance:
        paths = [p for p in record["paths"] if p not in swept_found.values]
        if paths:
            kept.append({**record, "paths": paths})
    return Resolved(values, kept + swept_found.provenance)


def _layer(
    orch: Any,
    inputs: Sequence[ModelInput],
    grid: pd.DatetimeIndex | None,
    cache: dict | None,
) -> Resolved:
    found = Resolved()
    for entry in inputs:
        values, record = _entry(orch, entry, grid, cache)
        twice = sorted(set(values) & set(found.values))
        if twice:
            raise ModelInputError(
                f"Two model_inputs entries both set {twice}; each path takes one."
            )
        found.values.update(values)
        found.provenance.append(record)
    return found


def _entry(
    orch: Any, entry: ModelInput, grid: pd.DatetimeIndex | None, cache: dict | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    selector = Selector(**entry.model_dump(exclude=set(_ALIGNMENT)))
    shift = pd.Timedelta(entry.offset) if entry.offset else pd.Timedelta(0)
    if grid is None:
        if entry.reduce is None:
            raise ModelInputError(
                f"Model input {_describe(entry)} gives a series, but the model has "
                "no time axis (its time_grid hook returned None); set reduce."
            )
        start = end = None
        window = None
    else:
        step = _step(grid)
        start, end = grid[0], grid[-1] + step
        margin = pd.Timedelta(0) if entry.reduce else _MARGIN
        window = Window(
            start=_utc(start + shift - margin, entry.timezone),
            end=_utc(end + shift + margin, entry.timezone),
        )
    data = _load(orch, selector, window, cache)
    frame = data.frame.copy()
    frame.index = frame.index.tz_convert(entry.timezone).tz_localize(None) - shift
    values, filled = {}, {}
    for column in frame.columns:
        series = frame[column]
        if entry.reduce is not None:
            value = _reduce(series, entry, start, end)
        else:
            value, filled[column] = _series(series, entry, grid)
        unit = data.units.get(column)
        if unit:
            # Pyomo prints m**3; flex-pse's parser splits on '*', so write m^3.
            value = {"value": value, "units": unit.replace("**", "^")}
        values[column] = value
    record = {
        "input": entry.model_dump(mode="json", exclude_defaults=True),
        "resolved": _plain(data.provenance),
        "paths": list(values),
        "window": (
            None
            if start is None
            else {"start": str(start + shift), "end": str(end + shift)}
        ),
        "timezone": entry.timezone,
        "filled": filled,
        "data_hash": _hash(values),
    }
    return values, record


def _load(
    orch: Any, selector: Selector, window: Window | None, cache: dict | None
) -> Any:
    key = (
        orch.study,
        json.dumps(selector.model_dump(mode="json"), sort_keys=True),
        window and (window.start, window.end),
    )
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    try:
        found = selectors.load(orch, selector, window)
    except FitError as exc:
        raise ModelInputError(f"Model input: {exc}") from exc
    if cache is not None:
        cache[key] = copy.deepcopy(found)
    return found


def _series(
    series: pd.Series, entry: ModelInput, grid: pd.DatetimeIndex
) -> tuple[list[float], int]:
    step = _step(grid)
    agg = (entry.resample or InputResample()).agg
    bins = series.resample(step, origin=grid[0], closed="left", label="left")
    binned = bins.sum(min_count=1) if agg == "sum" else getattr(bins, agg)()
    # The bins share the grid's origin and step, so the union stays regular.
    extended = binned.reindex(binned.index.union(grid))
    missing = int(extended.reindex(grid).isna().sum())
    if entry.gaps == "interpolate":
        extended = extended.interpolate(
            method="time", limit=entry.limit, limit_area="inside"
        )
    elif entry.gaps == "ffill":
        extended = extended.ffill(limit=entry.limit)
    elif entry.gaps == "zero":
        extended = extended.fillna(0.0)
    aligned = extended.reindex(grid)
    left = aligned.index[aligned.isna()]
    if len(left):
        shown = ", ".join(str(t) for t in left[:3]) + (", ..." if len(left) > 3 else "")
        raise ModelInputError(
            f"Model input {_describe(entry)} ({series.name!r}) has no value at "
            f"{len(left)} of the model's {len(grid)} time steps ({shown}) after "
            f"gaps: {entry.gaps}"
            + (f" with limit {entry.limit}" if entry.limit else "")
            + ". Read a wider window, or fill the gaps another way."
        )
    return [float(v) for v in aligned], missing


def _reduce(series: pd.Series, entry: ModelInput, start: Any, end: Any) -> float:
    if start is not None:
        series = series[(series.index >= start) & (series.index < end)]
    series = series.dropna()
    if series.empty:
        raise ModelInputError(
            f"Model input {_describe(entry)} ({series.name!r}) has no values over "
            "the model's horizon to reduce."
        )
    if entry.reduce == "first":
        return float(series.iloc[0])
    if entry.reduce == "last":
        return float(series.iloc[-1])
    return float(getattr(series, entry.reduce)())


def _step(grid: pd.DatetimeIndex) -> pd.Timedelta:
    if grid.freq is not None:
        return pd.Timedelta(grid.freq)
    if len(grid) < 2:
        raise ModelInputError("The model's time grid has one point and no freq.")
    return grid[1] - grid[0]


def _utc(local: pd.Timestamp, zone: str) -> datetime:
    """A naive local time as UTC; a time DST skips or repeats takes the later."""
    stamp = pd.Timestamp(local).tz_localize(
        zone, ambiguous=False, nonexistent="shift_forward"
    )
    return stamp.tz_convert("UTC").to_pydatetime()


def _hash(values: dict[str, Any]) -> str:
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _describe(entry: ModelInput) -> str:
    return f"{{{entry.source!r}: {getattr(entry, entry.source)!r}}}"
