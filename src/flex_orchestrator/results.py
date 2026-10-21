"""Write exported model values to a backend, and read them back (PLAN.md D3 step 4).

Each :class:`~flex_orchestrator.export.ExportedSeries` becomes one output
variable of the run, labelled by its key:

- a timeseries is declared ``timeseries`` and recorded as ``(timestamp, value)``
  rows;
- a scalar is declared ``scalar`` and recorded as its value.

The declaration carries the QUDT unit (only when verified, since Acquirium
rejects a unit it can't resolve) and the metadata ``pyomo_unit``,
``component`` and ``index`` (a JSON string, so a list survives Acquirium's
metadata flattening). A label keeps the unit it was first declared with: a
later run that exports it in another unit of the same dimensions is converted
to the declared unit, and a run whose units don't convert raises.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from flex_orchestrator.backend.base import Record, StorageBackend, Variable, to_rows
from flex_orchestrator.errors import StorageError, UnitError
from flex_orchestrator.export.model_data import ExportedSeries, ModelData
from flex_orchestrator.export.units import conversion_factor


@dataclass(frozen=True)
class _Write:
    series: ExportedSeries
    declared: Variable | None
    value: Any


def plan_writes(backend: StorageBackend, study: str, data: ModelData) -> list[_Write]:
    """Check every exported value against the study and prepare its write.

    Nothing is written, so a run is never started for values that can't be
    stored.

    Args:
        backend: The store.
        study: The study the run will belong to.
        data: The exported values.

    Returns:
        One write per value, in walk order, with values converted to the
        declared units.

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
            value: Any = _rows(series, values)
        else:
            value = values[0]
        writes.append(_Write(series, declared, value))
    return writes


def declare(backend: StorageBackend, study: str, writes: Sequence[_Write]) -> None:
    """Declare every value that isn't declared yet."""
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
    """Record the prepared values in a running run."""
    for write in writes:
        backend.record(run_id, write.series.key, write.value)


def read_model_data(backend: StorageBackend, study: str, run_id: str) -> ModelData:
    """Read a run's model values back as :class:`ModelData`.

    Values come back in their declared units, with UTC timestamps. JSON
    outputs (the solve outcome) are left out.

    Args:
        backend: The store.
        study: The study.
        run_id: The run.

    Returns:
        The stored values, in the order they were recorded.
    """
    series = []
    for found in backend.records(study, run_id):
        if found.kind == "json":
            continue
        series.append(_series(found, backend.variable(study, found.label)))
    return ModelData(series)


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


def _rows(series: ExportedSeries, values: list[float | None]) -> list:
    try:
        return to_rows(series.timestamps or [], values)
    except StorageError:
        raise StorageError(
            f"Timeseries {series.key!r} has no datetimes (its time points are "
            f"{series.timestamps[:3]!r}...). Pass datetime_index= to give each "
            "time point a datetime."
        ) from None


def _series(found: Record, variable: Variable | None) -> ExportedSeries:
    metadata = variable.metadata if variable is not None else {}
    index = json.loads(metadata.get("index", "[]"))
    if found.kind == "timeseries":
        rows = found.rows or []
        timestamps: list | None = [when for when, _ in rows]
        values = [value for _, value in rows]
    else:
        timestamps, values = None, [found.value]
    unit = variable.unit if variable is not None else None
    return ExportedSeries(
        key=found.label,
        kind=found.kind,
        component=metadata.get("component", found.label),
        index=index,
        values=values,
        timestamps=timestamps,
        unit_pyomo=metadata.get("pyomo_unit"),
        unit_qudt=unit,
        unit_verified=unit is not None,
        raw_index=tuple(index),
    )
