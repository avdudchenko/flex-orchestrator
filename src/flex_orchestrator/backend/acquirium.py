"""The Acquirium backend: the storage contract on Acquirium's study API.

This is the only module that talks to Acquirium (PLAN.md §3). It takes a
connected client, so how the client was started (a local server, a Postgres
one, or a remote tenant stack: PLAN.md D11) doesn't matter here.

Acquirium allows one active run per ``Study`` object, so each run gets its own
``Study`` handle, kept until the run ends. Declarations and reads go through
one cached handle per study. Units are declared as QUDT URIs, so Acquirium
doesn't look the unit text up again for every stream it registers.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import polars as pl
import requests

from flex_orchestrator.backend.base import (
    FileValue,
    Record,
    Role,
    Run,
    RunStatus,
    Variable,
    VariableKind,
    to_utc,
)
from flex_orchestrator.errors import StorageError

QUDT_UNIT = "http://qudt.org/vocab/unit/"
_QUDT_SCHEMA = "http://qudt.org/schema/qudt/"
_BRICK_REF = "https://brickschema.org/schema/Brick/ref#"
_ACQ = "urn:acquirium#"


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
        metadata = dict(metadata or {})
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
        return sorted(found, key=lambda run: run.started_at)

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
        _call(handle.client.delete_experiments, list(run_ids))

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

    # The general store: streams outside the experiment ledger (D19 selectors).

    def stream_rows(
        self, ref: str, start: Any = None, end: Any = None
    ) -> list[tuple[Any, float]]:
        """Return a stream's samples, oldest first, as UTC ``(time, value)`` rows.

        Args:
            ref: The stream's reference URI.
            start: Only samples at or after this datetime.
            end: Only samples at or before this datetime.
        """
        frame = _call(
            self.acquirium.client.timeseries_df,
            ref,
            start=None if start is None else to_utc(start).isoformat(),
            end=None if end is None else to_utc(end).isoformat(),
        )
        if frame.is_empty():
            return []
        return [
            (to_utc(when), value)
            for when, value in zip(
                frame["ts"].to_list(), frame["value"].to_list(), strict=True
            )
            if value is not None
        ]

    def sparql_select(self, query: str) -> dict[str, Any]:
        """Run a read-only SPARQL query; return ``{"columns", "rows"}``."""
        result = _call(self.acquirium.client.sparql_query, query)
        return {"columns": list(result["columns"]), "rows": list(result["rows"])}

    def stream_info(self, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return each stream's ``name`` (its source-local name), ``label`` and unit.

        The unit is the QUDT key on the stream, or on the point that refers to
        it; ``None`` if neither has one.
        """
        info = {ref: {"name": None, "label": None, "unit": None} for ref in refs}
        if not refs:
            return info
        values = " ".join(f"<{ref}>" for ref in refs)
        query = f"""PREFIX ref: <{_BRICK_REF}>
PREFIX qudt: <{_QUDT_SCHEMA}>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?ref ?name ?label ?unit WHERE {{
  VALUES ?ref {{ {values} }}
  OPTIONAL {{ ?ref <{_ACQ}refName> ?name }}
  OPTIONAL {{ ?point ref:hasExternalReference ?ref .
             OPTIONAL {{ ?point rdfs:label ?label }}
             OPTIONAL {{ ?point qudt:hasUnit ?pointUnit }} }}
  OPTIONAL {{ ?ref qudt:hasUnit ?refUnit }}
  BIND(COALESCE(?refUnit, ?pointUnit) AS ?unit)
}}"""
        for ref, name, label, unit in self.sparql_select(query)["rows"]:
            found = info[ref]
            found["name"] = found["name"] or name
            found["label"] = found["label"] or label
            if unit and not found["unit"]:
                found["unit"] = unit.rsplit("/", 1)[-1]
        return info

    def find_streams(self, text: str) -> list[dict[str, Any]]:
        """Find the streams whose point's label matches ``text``.

        A label equal to ``text`` (ignoring case) wins; failing that, every
        label containing all of its words. With no label match, ``text`` is
        resolved to a concept with Acquirium's ``resolve`` and that concept's
        streams are returned.

        Returns:
            ``[{"ref", "label", "point"}]``, sorted by label.
        """
        query = f"""PREFIX ref: <{_BRICK_REF}>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?point ?label ?ref WHERE {{
  ?point ref:hasExternalReference ?ref .
  ?point rdfs:label ?label .
}}"""
        rows = [
            {"point": point, "label": label, "ref": ref}
            for point, label, ref in self.sparql_select(query)["rows"]
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
                query = (
                    f"PREFIX ref: <{_BRICK_REF}>\nSELECT ?ref WHERE {{ <{concept}> "
                    "ref:hasExternalReference ?ref }"
                )
                matched = [
                    {"point": concept, "label": text, "ref": ref}
                    for (ref,) in self.sparql_select(query)["rows"]
                ]
        return sorted(matched, key=lambda r: (r["label"], r["ref"]))

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
    # looked up again for every stream it registers (PLAN.md D5).
    if unit is None or "://" in unit:
        return unit
    return f"{QUDT_UNIT}{unit}"


def _variable(declared: Any) -> Variable:
    metadata = dict(declared.metadata)
    unit = metadata.pop("unit", None)
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
