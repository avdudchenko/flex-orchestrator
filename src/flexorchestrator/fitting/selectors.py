"""Load fit data from what a :class:`~flexorchestrator.fitting.Selector` names.

Each selector becomes a pandas DataFrame with a UTC index, the Pyomo unit of
each column, and a provenance record of what it resolved to: the dataset run,
the result run, or the stream references.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from flexorchestrator.backend.base import to_utc
from flexorchestrator.data import DATA_KIND, MANIFEST
from flexorchestrator.errors import (
    FitError,
    StorageError,
    TagNotFoundError,
    UnitError,
)
from flexorchestrator.export.units import conversion_factor, pyomo_unit_of
from flexorchestrator.fitting.models import Selector, Window
from flexorchestrator.orchestrator import LEDGERS

# SPARQL Update keywords; a selector query may only read.
_UPDATE = re.compile(
    r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|ADD|MOVE|COPY)\b", re.IGNORECASE
)
_STREAM_METHODS = (
    "stream_rows",
    "sparql_select",
    "stream_info",
    "find_streams",
    "stream_ref",
)


@dataclass
class Selected:
    """The data one selector gave.

    Attributes:
        frame: Float columns on a UTC ``DatetimeIndex``.
        units: ``{column: Pyomo unit or None}``.
        provenance: What the selector resolved to.
    """

    frame: pd.DataFrame
    units: dict[str, str | None]
    provenance: dict[str, Any] = field(default_factory=dict)


def load(orch: Any, selector: Selector, window: Window | None = None) -> Selected:
    """Load one selector's data from the orchestrator's store.

    Args:
        orch: The orchestrator; its selected study is the default for
            ``dataset`` and the only one for ``result``.
        selector: What to load.
        window: The fit's window, intersected with the selector's own.

    Raises:
        FitError: If the source doesn't exist or matches nothing, a unit
            doesn't convert, or a ``rename`` doesn't fit the columns.
    """
    start, end = _bounds(selector, window)
    try:
        if selector.source == "dataset":
            found = _dataset(orch, selector, start, end)
        elif selector.source == "result":
            found = _result(orch, selector, start, end)
        else:
            found = _streams(orch, selector, start, end)
    except (TagNotFoundError, StorageError) as exc:
        raise FitError(f"Selector {_describe(selector)}: {exc}") from exc
    found = _rename(found, selector)
    found = _convert(found, selector)
    if found.frame.empty:
        raise FitError(
            f"Selector {_describe(selector)} matched no rows"
            + (f" between {start} and {end}" if start or end else "")
            + "."
        )
    found.provenance.update(
        {
            "rows": len(found.frame),
            "columns": list(found.frame.columns),
            "units": dict(found.units),
        }
    )
    return found


def _bounds(selector: Selector, window: Window | None) -> tuple[Any, Any]:
    starts = [w for w in (selector.start, window and window.start) if w is not None]
    ends = [w for w in (selector.end, window and window.end) if w is not None]
    start = max((to_utc(w) for w in starts), default=None)
    end = min((to_utc(w) for w in ends), default=None)
    return start, end


def _dataset(orch: Any, selector: Selector, start: Any, end: Any) -> Selected:
    study = selector.study or orch.study
    name = selector.dataset
    frame = orch._read_data(study, name, selector.columns, start, end, "pandas")
    run = orch._latest(name, DATA_KIND, study)
    ledger = orch._ledger(study, LEDGERS[DATA_KIND])
    [manifest] = orch.backend.records(ledger, run.run_id, labels=[MANIFEST])
    texts = manifest.value.get("units", {})
    keys = manifest.value.get("qudt_units", {})
    units = {
        column: pyomo_unit_of(texts.get(column)) or pyomo_unit_of(keys.get(column))
        for column in frame.columns
    }
    return Selected(
        _floats(frame),
        units,
        {
            "source": "dataset",
            "study": study,
            "dataset": name,
            "run_id": run.run_id,
        },
    )


def _result(orch: Any, selector: Selector, start: Any, end: Any) -> Selected:
    handle = orch.result(selector.result)
    table = handle.to_frame("pandas")
    missing = [key for key in selector.keys if key not in table.columns]
    if missing:
        raise FitError(
            f"Result {selector.result!r} has no timeseries {missing}; it has "
            f"{list(table.columns)}."
        )
    frame = table[list(selector.keys)]
    if start is not None:
        frame = frame[frame.index >= start]
    if end is not None:
        frame = frame[frame.index <= end]
    units = handle.units
    return Selected(
        _floats(frame),
        {key: units.get(key) for key in selector.keys},
        {"source": "result", "result": selector.result, "run_id": handle.run_id},
    )


def _streams(orch: Any, selector: Selector, start: Any, end: Any) -> Selected:
    backend = orch.backend
    if not all(hasattr(backend, method) for method in _STREAM_METHODS):
        raise FitError(
            f"Selector {_describe(selector)} reads the general store's streams, "
            f"which {type(backend).__name__} doesn't have; use the Acquirium "
            "backend."
        )
    provenance: dict[str, Any] = {"source": selector.source}
    if selector.source == "ref_uris":
        refs = list(dict.fromkeys(selector.ref_uris))
    elif selector.source == "streams":
        named = {
            backend.stream_ref(s.source_id, s.ref_name): s for s in selector.streams
        }
        refs = list(named)
    elif selector.source == "resolve":
        found = backend.find_streams(selector.resolve)
        if len(found) != 1:
            labels = [f"{f['label']} ({f['ref']})" for f in found]
            raise FitError(
                f"resolve {selector.resolve!r} matched "
                + (f"{len(found)} streams: {labels}" if found else "no stream")
                + "; it must match exactly one. Use ref_uris to name streams."
            )
        refs = [found[0]["ref"]]
        provenance.update({"text": selector.resolve, "label": found[0]["label"]})
    else:
        query = selector.sparql
        if _UPDATE.search(query) or not re.search(r"\bSELECT\b", query, re.I):
            raise FitError(
                "A sparql selector must be a read-only SELECT query; updates are "
                "refused."
            )
        rows = backend.sparql_select(query)["rows"]
        refs = list(dict.fromkeys(row[0] for row in rows if row and row[0]))
        provenance["query"] = query
        if not refs:
            raise FitError("The sparql selector's query returned no stream refs.")
    info = backend.stream_info(refs)
    if selector.source == "streams":
        for ref, stream in named.items():
            if info[ref]["name"] is None:
                raise FitError(
                    f"There's no stream {stream.ref_name!r} from source "
                    f"{stream.source_id!r}."
                )
    columns, units = {}, {}
    for ref in refs:
        name = info[ref]["name"] or ref.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if name in columns:
            raise FitError(
                f"Two streams are both named {name!r}; rename them by ref URI."
            )
        rows = backend.stream_rows(ref, start, end)
        columns[name] = pd.Series(
            [value for _, value in rows],
            index=pd.DatetimeIndex([when for when, _ in rows], tz="UTC"),
            dtype="float64",
        )
        unit = info[ref]["unit"]
        units[name] = pyomo_unit_of(unit) if unit else None
        provenance.setdefault("refs", {})[name] = ref
    frame = pd.DataFrame(columns)
    frame.index.name = "time"
    return Selected(frame.sort_index(), units, provenance)


def _rename(found: Selected, selector: Selector) -> Selected:
    mapping = dict(selector.rename)
    if not mapping:
        return found
    columns = list(found.frame.columns)
    if "*" in mapping:
        if len(mapping) != 1 or len(columns) != 1:
            raise FitError(
                f"rename {{'*': ...}} needs a selector with one column; "
                f"{_describe(selector)} has {columns}."
            )
        mapping = {columns[0]: mapping["*"]}
    unknown = [c for c in mapping if c not in columns]
    if unknown:
        raise FitError(f"rename names columns the selector doesn't have: {unknown}.")
    found.frame = found.frame.rename(columns=mapping)
    found.units = {mapping.get(c, c): u for c, u in found.units.items()}
    if "refs" in found.provenance:
        found.provenance["refs"] = {
            mapping.get(c, c): r for c, r in found.provenance["refs"].items()
        }
    return found


def _convert(found: Selected, selector: Selector) -> Selected:
    if selector.units is None:
        return found
    targets: Mapping[str, str] = (
        {c: selector.units for c in found.frame.columns}
        if isinstance(selector.units, str)
        else selector.units
    )
    unknown = [c for c in targets if c not in found.frame.columns]
    if unknown:
        raise FitError(f"units= names columns the selector doesn't have: {unknown}.")
    for column, target in targets.items():
        to = pyomo_unit_of(target)
        have = found.units.get(column)
        if to is None:
            raise FitError(f"Can't read unit {target!r} for column {column!r}.")
        if have is None:
            raise FitError(
                f"Column {column!r} has no stored unit, so it can't be converted "
                f"to {target!r}."
            )
        try:
            factor = conversion_factor(have, to)
        except UnitError as exc:
            raise FitError(f"Column {column!r}: {exc}") from exc
        found.frame[column] = found.frame[column] * factor
        found.units[column] = to
    return found


def _floats(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.astype("float64")
    frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC")
    frame.index.name = "time"
    return frame


def _describe(selector: Selector) -> str:
    return f"{{{selector.source!r}: {getattr(selector, selector.source)!r}}}"
