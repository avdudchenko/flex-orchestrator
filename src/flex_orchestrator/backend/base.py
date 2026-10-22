"""The storage contract every backend implements (PLAN.md D10).

It mirrors Acquirium's study API, so the memory backend behaves like
Acquirium and the same contract tests run against both:

- a **study** holds variable declarations and runs; defining one is
  idempotent, as in Acquirium;
- a **variable** is declared once per study with a role, kind, unit and
  metadata, and keeps them: a later declaration of the same label with another
  shape is an error;
- a **run** starts with its metadata (which can't change afterwards), records
  values for declared variables, and ends ``succeeded`` or ``failed``;
- finished runs, and whole studies, can be **deleted** (never a running one);
- a **table** variable records a whole polars DataFrame (Acquirium stores it
  as Parquet), and a timeseries sample always has a value: a missing value is
  no sample;
- a **file** variable records a :class:`FileValue`, bytes with a file name
  (an Acquirium artifact, stored once per content hash).

Timestamps are timezone-aware UTC datetimes on the way out; naive datetimes
going in are taken as UTC.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from flex_orchestrator.errors import StorageError

Role = Literal["input", "output"]
VariableKind = Literal["timeseries", "scalar", "json", "table", "file"]
RunStatus = Literal["running", "succeeded", "failed"]
Row = tuple[datetime, float]
"""One timeseries sample: ``(timestamp, value)``."""


@dataclass(frozen=True)
class Variable:
    """A declared variable.

    Attributes:
        label: The variable's label, unique within its study.
        role: ``"input"`` or ``"output"``.
        kind: ``"timeseries"``, ``"scalar"``, ``"json"``, ``"table"`` or
            ``"file"``.
        unit: The QUDT unit key, or ``None``.
        metadata: Everything else declared with it.
    """

    label: str
    role: Role
    kind: VariableKind
    unit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileValue:
    """The value of a ``file`` variable.

    Attributes:
        name: The file name (no directory).
        data: The file's bytes.
    """

    name: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Run:
    """One run of a study.

    Attributes:
        run_id: The backend's id for the run.
        status: ``"running"``, ``"succeeded"`` or ``"failed"``.
        metadata: The metadata the run started with.
        started_at: When it started (UTC).
        error: ``{"type", "message"}`` for a failed run.
    """

    run_id: str
    status: RunStatus
    metadata: dict[str, Any]
    started_at: datetime | None = None
    error: Any = None


@dataclass(frozen=True)
class Record:
    """One value a run recorded.

    Attributes:
        label: The variable's label.
        kind: The variable's kind.
        value: The value of a scalar or JSON variable, the polars DataFrame
            of a table, or the :class:`FileValue` of a file.
        rows: The samples of a timeseries, in time order.
    """

    label: str
    kind: VariableKind
    value: Any = None
    rows: list[Row] | None = None


class StorageBackend(Protocol):
    """What the orchestrator needs from a store.

    Attributes:
        resolve_unit: Looks a unit up in QUDT (see
            :data:`flex_orchestrator.export.units.Resolver`), or ``None`` if the
            backend has no unit vocabulary.
    """

    resolve_unit: Callable[[str], Mapping[str, Any] | None] | None

    def define_study(self, name: str) -> None:
        """Create the study if it doesn't exist (idempotent)."""

    def has_study(self, name: str) -> bool:
        """Return whether the study exists."""

    def studies(self) -> list[str]:
        """Return the names of every study, sorted."""

    def variable(self, study: str, label: str) -> Variable | None:
        """Return the declaration of ``label``, or ``None``."""

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
            StorageError: If ``label`` is declared with another role, kind,
                unit or metadata, or the backend rejects the unit.
        """

    def start_run(self, study: str, metadata: Mapping[str, Any]) -> str:
        """Start a run and return its id."""

    def record(self, run_id: str, label: str, value: Any) -> None:
        """Record a value in a running run.

        ``value`` is a list of :data:`Row` for a timeseries (every row with a
        number), a number or ``None`` for a scalar, any JSON value for a JSON
        variable, a polars DataFrame for a table, and a :class:`FileValue` for
        a file.

        Raises:
            StorageError: If the run isn't running, ``label`` isn't declared, or
                the value doesn't fit the variable's kind.
        """

    def finish_run(self, run_id: str) -> None:
        """Mark a running run ``succeeded``."""

    def fail_run(self, run_id: str, error: BaseException) -> None:
        """Mark a running run ``failed``, recording ``error``."""

    def runs(
        self,
        study: str,
        *,
        status: RunStatus | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[Run]:
        """Return the study's runs, oldest first.

        Args:
            study: The study.
            status: Only runs with this status.
            metadata: Only runs whose metadata has these exact values.
        """

    def variables(self, study: str) -> list[Variable]:
        """Return every variable declared in the study."""

    def delete_runs(self, study: str, run_ids: Sequence[str]) -> None:
        """Delete finished runs and everything they recorded.

        Raises:
            StorageError: If a run doesn't exist in the study or is still
                running; then nothing is deleted.
        """

    def delete_study(self, name: str) -> None:
        """Delete a study, its declarations and every run.

        Raises:
            StorageError: If the study doesn't exist or has a running run.
        """

    def records(
        self, study: str, run_id: str, labels: Sequence[str] | None = None
    ) -> list[Record]:
        """Return what a run recorded, in the order it was recorded.

        Args:
            study: The study.
            run_id: The run.
            labels: Only these labels; a backend then reads only their data.
        """


def to_utc(when: Any) -> datetime:
    """Return ``when`` as a timezone-aware UTC datetime.

    Args:
        when: A ``datetime`` (or a pandas ``Timestamp``); naive means UTC.

    Returns:
        The UTC datetime.

    Raises:
        StorageError: If ``when`` isn't a datetime.

    Example:
        >>> to_utc(datetime(2025, 7, 8)).isoformat()
        '2025-07-08T00:00:00+00:00'
    """
    if hasattr(when, "to_pydatetime"):
        when = when.to_pydatetime()
    if not isinstance(when, datetime):
        raise StorageError(
            f"Timeseries timestamps must be datetimes, not {type(when).__name__} "
            f"({when!r})."
        )
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def to_rows(timestamps: Sequence[Any], values: Sequence[float | None]) -> list[Row]:
    """Pair timestamps with values as UTC :data:`Row` samples.

    A missing value (``None``) is left out: a timeseries has no sample there.

    Raises:
        StorageError: If a timestamp isn't a datetime.
    """
    return [
        (to_utc(when), value)
        for when, value in zip(timestamps, values, strict=True)
        if value is not None
    ]
