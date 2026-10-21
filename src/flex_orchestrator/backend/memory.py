"""An in-memory backend for unit tests (PLAN.md §7).

It follows the :class:`~flex_orchestrator.backend.base.StorageBackend`
contract the way Acquirium does, so the contract tests pass on both.
"""

import copy
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from flex_orchestrator.backend.base import (
    Record,
    Role,
    Run,
    RunStatus,
    Variable,
    VariableKind,
    to_utc,
)
from flex_orchestrator.errors import StorageError


@dataclass
class _Run:
    study: str
    run: Run
    records: dict[str, Record] = field(default_factory=dict)


class MemoryBackend:
    """Studies, variables and runs held in dicts.

    Attributes:
        resolve_unit: Always ``None``: there is no unit vocabulary, so the
            exporter's QUDT keys stay unverified and aren't declared.

    Example:
        >>> backend = MemoryBackend()
        >>> backend.define_study("s")
        >>> backend.has_study("s")
        True
    """

    resolve_unit = None

    def __init__(self) -> None:
        """Create an empty store."""
        self._variables: dict[str, dict[str, Variable]] = {}
        self._runs: dict[str, _Run] = {}

    def define_study(self, name: str) -> None:
        """Create the study if it doesn't exist."""
        self._variables.setdefault(name, {})

    def has_study(self, name: str) -> bool:
        """Return whether the study exists."""
        return name in self._variables

    def variable(self, study: str, label: str) -> Variable | None:
        """Return the declaration of ``label``, or ``None``."""
        return self._study(study).get(label)

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
            StorageError: If ``label`` is declared differently, or a JSON
                variable is given a unit.
        """
        if kind == "json" and unit is not None:
            raise StorageError(f"JSON variable {label!r} can't have a unit.")
        # Like Acquirium, None-valued metadata isn't kept.
        kept = {k: v for k, v in (metadata or {}).items() if v is not None}
        wanted = Variable(label, role, kind, unit, copy.deepcopy(kept))
        variables = self._study(study)
        found = variables.setdefault(label, wanted)
        if found != wanted:
            raise StorageError(
                f"Variable {label!r} in study {study!r} was already declared "
                "differently."
            )
        return found

    def start_run(self, study: str, metadata: Mapping[str, Any]) -> str:
        """Start a run and return its id."""
        self._study(study)
        run_id = str(uuid.uuid4())
        run = Run(run_id, "running", dict(metadata), started_at=datetime.now(UTC))
        self._runs[run_id] = _Run(study, run)
        return run_id

    def record(self, run_id: str, label: str, value: Any) -> None:
        """Record a value in a running run.

        Raises:
            StorageError: If the run isn't running, ``label`` isn't declared, or
                the value isn't JSON (NaN included).
        """
        entry = self._running(run_id)
        variable = self.variable(entry.study, label)
        if variable is None:
            raise StorageError(f"Variable {label!r} isn't declared.")
        if variable.kind == "timeseries":
            rows = sorted(((to_utc(when), v) for when, v in value), key=lambda r: r[0])
            self._check_json([v for _, v in rows])
            entry.records[label] = Record(label, "timeseries", rows=rows)
        else:
            self._check_json(value)
            entry.records[label] = Record(label, variable.kind, copy.deepcopy(value))

    def finish_run(self, run_id: str) -> None:
        """Mark a running run ``succeeded``."""
        self._end(run_id, "succeeded", None)

    def fail_run(self, run_id: str, error: BaseException) -> None:
        """Mark a running run ``failed``."""
        self._end(
            run_id, "failed", {"type": type(error).__name__, "message": str(error)}
        )

    def runs(
        self,
        study: str,
        *,
        status: RunStatus | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[Run]:
        """Return the study's runs, oldest first."""
        self._study(study)
        return [
            entry.run
            for entry in self._runs.values()
            if entry.study == study
            and (status is None or entry.run.status == status)
            and all(entry.run.metadata.get(k) == v for k, v in (metadata or {}).items())
        ]

    def variables(self, study: str) -> list[Variable]:
        """Return every variable declared in the study."""
        return list(self._study(study).values())

    def records(
        self, study: str, run_id: str, labels: Sequence[str] | None = None
    ) -> list[Record]:
        """Return what a run recorded, in the order it was recorded."""
        entry = self._runs.get(run_id)
        if entry is None or entry.study != study:
            raise StorageError(f"Study {study!r} has no run {run_id!r}.")
        return [
            r for r in entry.records.values() if labels is None or r.label in labels
        ]

    def _study(self, name: str) -> dict[str, Variable]:
        try:
            return self._variables[name]
        except KeyError:
            raise StorageError(f"There is no study {name!r}.") from None

    def _running(self, run_id: str) -> _Run:
        entry = self._runs.get(run_id)
        if entry is None or entry.run.status != "running":
            raise StorageError(f"Run {run_id!r} isn't running.")
        return entry

    def _end(self, run_id: str, status: RunStatus, error: Any) -> None:
        entry = self._running(run_id)
        run = entry.run
        entry.run = Run(run.run_id, status, run.metadata, run.started_at, error)

    @staticmethod
    def _check_json(value: Any) -> None:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StorageError(f"Can't store {value!r}: {exc}.") from None
