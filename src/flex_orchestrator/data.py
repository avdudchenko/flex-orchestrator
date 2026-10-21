"""Datasets: tables of measured data stored with a study (PLAN.md D6).

A dataset ``name`` is one run of ``<s>::data`` carrying ``{flexorch_kind:
"data", flexorch_tag: name}``:

- each column is a timeseries input labelled ``<name>/<column>``, so two
  datasets can both have a ``flow`` column, declared with its QUDT unit (when
  one is found) and the metadata ``dataset``, ``column`` and ``unit_text`` (the
  unit as the user wrote it);
- the ``manifest`` JSON lists the columns in order, their units, the row count
  and the time range.

Units are found the way the exporter finds them (pint's parts, verified with
the backend's resolver), and failing that by the resolver's own text lookup,
which understands spellings like ``m3/day``. A unit the resolver can't place is
stored as text only, with a warning; without a resolver (the memory backend)
every unit is stored as text only.
"""

import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flex_orchestrator.backend.base import StorageBackend, to_rows
from flex_orchestrator.errors import StorageError, UnitError
from flex_orchestrator.export.units import UnitMapper, local_name

DATA_KIND = "data"
MANIFEST = "manifest"
"""str: The label of a dataset's JSON manifest."""


@dataclass(frozen=True)
class StoredData:
    """What :meth:`Orchestrator.store_data` stored.

    Attributes:
        name: The dataset's name.
        run_id: The backend run.
        columns: The column names, in order.
        rows: The number of rows.
        units: ``{column: QUDT key or None}``.
    """

    name: str
    run_id: str
    columns: list[str]
    rows: int
    units: dict[str, str | None]


def column_label(name: str, column: str) -> str:
    """Return the variable label of a dataset column."""
    return f"{name}/{column}"


def qudt_unit(text: str | None, mapper: UnitMapper) -> str | None:
    """Return the verified QUDT key for a unit as a user wrote it, or ``None``.

    Without a resolver no key can be verified, so the answer is ``None``.
    """
    if text is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            mapping = mapper.map(text)
        except UnitError:
            mapping = None
    if mapping is not None and mapping.verified:
        return mapping.qudt
    if mapper.resolver is None:
        return None
    try:
        found = mapper.resolver(text)
    except Exception:
        return None
    return local_name(found) if found else None


def store(
    backend: StorageBackend,
    study: str,
    name: str,
    timestamps: list,
    columns: Mapping[str, list[float | None]],
    units: Mapping[str, str | None],
    mapper: UnitMapper,
    metadata: Mapping[str, Any],
) -> StoredData:
    """Declare and record one dataset as a run of ``study``.

    Everything is checked and declared before the run starts.

    Raises:
        StorageError: If the table is empty, ``units`` names a column the table
            doesn't have, or a column is already declared with other units.
    """
    if not timestamps or not columns:
        raise StorageError(f"Dataset {name!r} has no rows or no columns.")
    unknown = sorted(set(units) - set(columns))
    if unknown:
        raise StorageError(f"units= names columns the table doesn't have: {unknown}.")
    keys = {column: qudt_unit(units.get(column), mapper) for column in columns}
    for column, key in keys.items():
        # Without a resolver no unit can be verified, so there is nothing to warn about.
        if key is None and units.get(column) is not None and mapper.resolver:
            warnings.warn(
                f"No QUDT unit for {units[column]!r} (column {column!r}); storing "
                "it with its unit text only.",
                stacklevel=3,
            )
        backend.declare(
            study,
            column_label(name, column),
            role="input",
            kind="timeseries",
            unit=key,
            metadata={
                "dataset": name,
                "column": column,
                "unit_text": units.get(column),
            },
        )
    backend.declare(study, MANIFEST, role="input", kind="json")
    manifest = {
        "name": name,
        "columns": list(columns),
        "units": {column: units.get(column) for column in columns},
        "qudt_units": keys,
        "rows": len(timestamps),
        "start": min(timestamps).isoformat(),
        "end": max(timestamps).isoformat(),
    }
    run_id = backend.start_run(study, metadata)
    try:
        backend.record(run_id, MANIFEST, manifest)
        for column, values in columns.items():
            backend.record(
                run_id, column_label(name, column), to_rows(timestamps, values)
            )
    except BaseException as exc:
        backend.fail_run(run_id, exc)
        raise
    backend.finish_run(run_id)
    return StoredData(name, run_id, list(columns), len(timestamps), keys)


def read(
    backend: StorageBackend,
    study: str,
    run_id: str,
    name: str,
    columns: list[str] | None,
) -> tuple[list, dict[str, list[float | None]]]:
    """Read a dataset's columns back, aligned on their timestamps.

    Raises:
        StorageError: If ``columns`` names a column the dataset doesn't have.
    """
    [manifest] = backend.records(study, run_id, labels=[MANIFEST])
    stored = manifest.value["columns"]
    wanted = stored if columns is None else list(columns)
    missing = [c for c in wanted if c not in stored]
    if missing:
        raise StorageError(
            f"Dataset {name!r} has no column {missing[0]!r}; it has {stored}."
        )
    labels = {column_label(name, c): c for c in wanted}
    by_column = {
        labels[r.label]: dict(r.rows or [])
        for r in backend.records(study, run_id, labels=list(labels))
    }
    timestamps = sorted({when for rows in by_column.values() for when in rows})
    return timestamps, {
        c: [by_column.get(c, {}).get(when) for when in timestamps] for c in wanted
    }
