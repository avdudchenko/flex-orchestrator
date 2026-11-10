"""The Acquirium backend: the storage contract on Acquirium's study API.

This is the only module that talks to Acquirium. It takes a
connected client, so how the client was started (a local server, a Postgres
one, or a remote server) doesn't matter here.

Acquirium allows one active run per ``Study`` object, so each run gets its own
``Study`` handle, kept until the run ends. Declarations and reads go through
one cached handle per study. Units are declared as QUDT URIs, so Acquirium
doesn't look the unit text up again for every stream it registers.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import polars as pl
import requests

from flexorchestrator.backend.base import (
    FileValue,
    Record,
    Role,
    Row,
    Run,
    RunStatus,
    Variable,
    VariableKind,
    qudt_key,
    to_rows,
    to_utc,
)
from flexorchestrator.errors import StorageError

QUDT_UNIT = "http://qudt.org/vocab/unit/"
_EPOCH = datetime.min.replace(tzinfo=UTC)


class AcquiriumBackend:
    """Stores studies, variables and runs in Acquirium.

    Args:
        acquirium: A connected Acquirium client, e.g. from
            ``acquirium.init(data_dir=...)``.

    Attributes:
        acquirium: The client.
        resolve_unit: Acquirium's QUDT lookup, so the exporter verifies every
            unit key against the store's own vocabulary.

    Example:
        >>> import acquirium as aq  # doctest: +SKIP
        >>> backend = AcquiriumBackend(aq.init(data_dir=".flexorch"))  # doctest: +SKIP
    """

    def __init__(self, acquirium: Any) -> None:
        """Wrap a connected Acquirium client."""
        self.acquirium = acquirium
        self.resolve_unit: Callable[[str], Mapping[str, Any]] = (
            acquirium.client.resolve_unit
        )
        self._studies: dict[str, Any] = {}
        self._active: dict[str, Any] = {}

    def define_study(self, name: str) -> None:
        """Create the study if it doesn't exist."""
        self._studies[name] = _call(self.acquirium.study.define, name)

    def has_study(self, name: str) -> bool:
        """Return whether the study exists."""
        if name in self._studies:
            return True
        try:
            self._studies[name] = self.acquirium.study.get(name)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return False
            raise StorageError(f"Acquirium: {exc}") from exc
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            raise StorageError(f"Acquirium: {exc}") from exc
        return True

    def studies(self) -> list[str]:
        """Return the names of every study, sorted."""
        items = _call(self.acquirium.client.list_experiment_studies)
        return sorted(item["name"] for item in items)

    def variable(self, study: str, label: str) -> Variable | None:
        """Return the declaration of ``label``, or ``None``."""
        found = self._study(study).variables.get(label)
        return None if found is None else _variable(found)

    def declare(
        self,
        study: str,
        label: str,
        *,
        role: Role,
        kind: VariableKind,
        unit: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Variable:
        """Declare a variable, or return it if declared the same way.

        Raises:
            StorageError: If ``label`` is declared differently, or Acquirium
                can't resolve ``unit``.
        """
        builder = getattr(self._study(study), role)(label)
        metadata = {k: v for k, v in (metadata or {}).items() if v is not None}
        if kind in ("json", "table", "file"):
            if unit is not None:
                raise StorageError(f"A {kind} variable ({label!r}) can't have a unit.")
            declared = _call(getattr(builder, kind), **metadata)
        else:
            declared = _call(getattr(builder, kind), unit=_unit_uri(unit), **metadata)
        return _variable(declared)

    def start_run(self, study: str, metadata: Mapping[str, Any]) -> str:
        """Start a run and return its id."""
        handle = _call(self.acquirium.study.get, study)
        run = _call(handle.start, dict(metadata))
        self._active[run.run_id] = run
        return run.run_id

    def record(self, run_id: str, label: str, value: Any) -> None:
        """Record a value in a running run.

        Raises:
            StorageError: If the run isn't running here, ``label`` isn't
                declared, or Acquirium rejects the value.
        """
        run = self._running(run_id)
        variable = run.study.variables.get(label)
        if variable is None:
            raise StorageError(f"Variable {label!r} isn't declared.")
        if variable.kind == "table" and not isinstance(value, pl.DataFrame):
            raise StorageError(f"Table {label!r} records a polars DataFrame.")
        if variable.kind == "file":
            if not isinstance(value, FileValue):
                raise StorageError(f"File {label!r} records a FileValue.")
            # Bytes go straight to the server; no local file is involved.
            _call(
                run.study.client.attach_experiment_bytes,
                run_id,
                variable.variable_id,
                value.data,
                filename=value.name,
            )
            return
        _call(variable.record, value)

    def finish_run(self, run_id: str) -> None:
        """Mark a running run ``succeeded``."""
        _call(self._running(run_id).finish)
        del self._active[run_id]

    def fail_run(self, run_id: str, error: BaseException) -> None:
        """Mark a running run ``failed``."""
        _call(self._running(run_id).fail, error)
        del self._active[run_id]

    def runs(
        self,
        study: str,
        *,
        status: RunStatus | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[Run]:
        """Return the study's runs, oldest first."""
        # metadata= is an exact-match filter in SQL; **attrs would need the graph.
        selected = self._study(study).experiments.where(
            status=status, metadata=dict(metadata) if metadata else None
        )
        found = [
            Run(r.run_id, r.status, dict(r.metadata), r.started_at, r.error)
            for r in _call(selected.all)
        ]
        # A run without a start time (not expected from Acquirium) sorts last.
        return sorted(
            found, key=lambda run: (run.started_at is None, run.started_at or _EPOCH)
        )

    def variables(self, study: str) -> list[Variable]:
        """Return every variable declared in the study."""
        # A fresh handle loads every declaration, including ones made elsewhere.
        handle = _call(self.acquirium.study.get, study)
        self._studies[study] = handle
        return [_variable(v) for v in handle.variables.all()]

    def delete_runs(self, study: str, run_ids: Sequence[str]) -> None:
        """Delete finished runs and everything they recorded."""
        if not run_ids:
            return
        handle = self._study(study)
        found = {r.run_id for r in _call(handle.experiments.all)}
        missing = [run_id for run_id in run_ids if run_id not in found]
        if missing:
            raise StorageError(f"Study {study!r} has no run {missing[0]!r}.")
        _call(handle.client.delete_experiments, list(dict.fromkeys(run_ids)))

    def delete_study(self, name: str) -> None:
        """Delete a study, its declarations and every run."""
        _call(self._study(name).delete)
        del self._studies[name]

    def records(
        self, study: str, run_id: str, labels: Sequence[str] | None = None
    ) -> list[Record]:
        """Return what a run recorded, in the order it was recorded."""
        handle = self._study(study)
        try:
            run = handle.experiments.get(run_id)
        except (KeyError, requests.HTTPError) as exc:
            raise StorageError(f"Study {study!r} has no run {run_id!r}.") from exc
        found = []
        for observation in _call(run.observations().all):
            variable = observation.variable
            if labels is not None and variable.label not in labels:
                continue
            if variable.kind == "timeseries":
                frame = _call(handle.client.timeseries_df, observation.ref_uri)
                rows = [
                    (to_utc(when), value)
                    for when, value in zip(
                        frame["ts"].to_list(), frame["value"].to_list(), strict=True
                    )
                ]
                found.append(Record(variable.label, "timeseries", rows=rows))
            elif variable.kind == "table":
                frame = _call(observation.dataframe)
                found.append(Record(variable.label, "table", frame))
            elif variable.kind == "file":
                data = _call(observation.read_bytes)
                found.append(
                    Record(
                        variable.label, "file", FileValue(observation.filename, data)
                    )
                )
            else:
                found.append(Record(variable.label, variable.kind, observation.value))
        return found

    # The general store: streams outside the experiment ledger, for the fit selectors.

    def stream_rows(self, ref: str, start: Any = None, end: Any = None) -> list[Row]:
        """Return a stream's samples, oldest first, as UTC ``(time, value)`` rows.

        Read through Acquirium's query API, so the values are in the unit
        :meth:`stream_info` reports (Acquirium converts a stream stored in
        another unit than its point's).

        Args:
            ref: The stream's reference URI.
            start: Only samples at or after this datetime.
            end: Only samples at or before this datetime.
        """
        frame = _call(
            self.acquirium.query().measurement(ref=ref).dataframe,
            "narrow",
            start=None if start is None else to_utc(start),
            end=None if end is None else to_utc(end),
        )
        if frame.is_empty():
            return []
        return to_rows(frame["time"].to_list(), frame["value_numeric"].to_list())

    def sparql_select(self, query: str) -> dict[str, Any]:
        """Run a read-only SPARQL query; return ``{"columns", "rows"}``.

        Only for the ``sparql`` selector, whose query the user writes; the
        other stream lookups go through Acquirium's query API.
        """
        result = _call(self.acquirium.client.sparql_query, query)
        return {"columns": list(result["columns"]), "rows": list(result["rows"])}

    def stream_info(self, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return each stream's ``name`` (its source-local name), ``label`` and unit.

        The label is its point's; the unit is a QUDT key, the one
        :meth:`stream_rows` gives values in, or ``None`` if it has none. A
        stream Acquirium doesn't have gets no ``name``.
        """
        info = {ref: {"name": None, "label": None, "unit": None} for ref in refs}
        if not refs:
            return info
        for row in self._measurements(ref=list(info)):
            found = info[row["ref_uri"]]
            found["name"] = found["name"] or row["ref_name"]
            found["label"] = found["label"] or row["point_label"]
            if row["unit"] and not found["unit"]:
                found["unit"] = qudt_key(row["unit"])
        return info

    def stream_ref(self, source_id: str, ref_name: str) -> str:
        """Return the reference URI of ``source_id``'s stream ``ref_name``.

        Acquirium derives it from the two names, so this doesn't check that the
        stream exists; :meth:`stream_info` gives a missing stream no ``name``.
        """
        return str(self.acquirium.reference_uri(source_id, ref_name))

    def find_streams(self, text: str) -> list[dict[str, Any]]:
        """Find the streams whose point's label matches ``text``.

        A label equal to ``text`` (ignoring case) wins; failing that, every
        label containing all of its words. With no label match, ``text`` is
        resolved to a point with Acquirium's ``resolve`` and that point's
        streams are returned.

        Returns:
            ``[{"ref", "label", "point"}]``, sorted by label.
        """
        rows = [
            {
                "point": row["point_uri"],
                "label": row["point_label"],
                "ref": row["ref_uri"],
            }
            for row in self._measurements()
            if row["point_label"]
        ]
        wanted = text.strip().lower()
        exact = [r for r in rows if r["label"].strip().lower() == wanted]
        words = wanted.split()
        matched = exact or [
            r for r in rows if all(w in r["label"].lower() for w in words)
        ]
        if not matched:
            concept = _call(self.acquirium.client.resolve, text)
            if concept is not None:
                matched = [
                    {"point": concept, "label": text, "ref": row["ref_uri"]}
                    for row in self._measurements(uri=concept)
                ]
        return sorted(matched, key=lambda r: (r["label"], r["ref"]))

    def _measurements(self, **match: Any) -> list[dict[str, Any]]:
        """Acquirium's measurement points matching ``match``, one row per stream.

        Each row has ``ref_uri``, ``ref_name``, ``point_uri``, ``point_label``
        and ``unit`` (a URI). Reading this doesn't fetch stream statistics.
        """
        data = _call(self.acquirium.query().measurement(**match).data)
        return _call(data.metadata, include_ref_uris=True).to_dicts()

    def _study(self, name: str) -> Any:
        if name not in self._studies:
            self._studies[name] = _call(self.acquirium.study.get, name)
        return self._studies[name]

    def _running(self, run_id: str) -> Any:
        try:
            return self._active[run_id]
        except KeyError:
            raise StorageError(
                f"Run {run_id!r} isn't running in this session."
            ) from None


def _unit_uri(unit: str | None) -> str | None:
    # A QUDT URI passes through Acquirium's unit resolution; a bare key would be
    # looked up again for every stream it registers.
    if unit is None or "://" in unit:
        return unit
    return f"{QUDT_UNIT}{unit}"


def _variable(declared: Any) -> Variable:
    metadata = dict(declared.metadata)
    unit = declared.unit
    if unit is not None and unit.startswith(QUDT_UNIT):
        unit = unit[len(QUDT_UNIT) :]
    metadata.pop("observed", None)
    return Variable(declared.label, declared.role, declared.kind, unit, metadata)


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call into Acquirium, turning its HTTP and value errors into StorageError."""
    try:
        return fn(*args, **kwargs)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise StorageError(f"Acquirium: {detail}") from exc
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        raise StorageError(f"Acquirium: {exc}") from exc
