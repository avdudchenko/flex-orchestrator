"""The orchestrator: studies, datasets and model results on a storage backend.

Model results are stored **explicitly**: every call names the model and the
tag, and there is no "current model" (PLAN.md D4). A study ``s`` is laid out as
backend studies (D10): ``s`` itself; ``s::runs``, where each stored result is
one run carrying ``{flexorch_kind: "result", flexorch_tag: tag}``, the exported
model values as outputs, and the ``solve_outcome`` JSON; and ``s::data``, where
each dataset is one run (D6, :mod:`flex_orchestrator.data`).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Any, Literal

from flex_orchestrator import data as datasets
from flex_orchestrator import frames, results
from flex_orchestrator.backend.base import StorageBackend, to_utc
from flex_orchestrator.data import DATA_KIND, StoredData
from flex_orchestrator.errors import (
    AmbiguousColumnError,
    HookError,
    StudyExistsError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
)
from flex_orchestrator.export import ModelData, UnitMapper, export_model_data
from flex_orchestrator.hooks import (
    DEFAULT_HOOKS,
    HookRegistry,
    HookSet,
    SolveOutcome,
    run_solve,
)
from flex_orchestrator.specs import check_tag

RESULT_KIND = "result"
LEDGERS = {RESULT_KIND: "runs", DATA_KIND: "data"}
"""dict: Item kind -> the ledger study (``<s>::<ledger>``) that holds it (D10)."""
SOLVE_OUTCOME = "solve_outcome"
"""str: The label of the JSON output that holds a result's solve outcome."""


class SolveNotOptimal(Exception):  # noqa: N818 (recorded as the run's error type)
    """Recorded as the error of a result run whose solve didn't succeed."""


@dataclass(frozen=True)
class StoredResult:
    """What :meth:`Orchestrator.store_model_results` stored.

    Attributes:
        tag: The result's tag.
        run_id: The backend run.
        status: ``"succeeded"``, or ``"failed"`` when the solve wasn't ``ok``
            (then only the solve outcome is stored, and the tag can be reused).
        outcome: The solve outcome, if one was given.
        keys: The exported keys that were stored.
    """

    tag: str
    run_id: str
    status: Literal["succeeded", "failed"]
    outcome: SolveOutcome | None = None
    keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the result was stored with its model values."""
        return self.status == "succeeded"


class Orchestrator:
    """Studies, model results and hooks on one storage backend.

    Args:
        backend: The store, e.g. :class:`~flex_orchestrator.backend.MemoryBackend`
            or :class:`~flex_orchestrator.backend.AcquiriumBackend`.
        study: A study to select; it must exist.
        hooks: Hooks that replace the flex-pse defaults, slot by slot.
        registry: Where registered hook names resolve.
        unit_mapper: Maps Pyomo units to QUDT. By default it verifies keys
            with the backend's ``resolve_unit``.
        strict_units: With the default mapper, raise on a unit with no QUDT
            key instead of storing it without one.
        frame: What reads return by default, ``"polars"`` or ``"pandas"`` (D7).

    Example:
        >>> from flex_orchestrator.backend import MemoryBackend
        >>> orch = Orchestrator(MemoryBackend())
        >>> orch.create_study("example_a")
        'example_a'
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        study: str | None = None,
        hooks: HookSet | None = None,
        registry: HookRegistry | None = None,
        unit_mapper: UnitMapper | None = None,
        strict_units: bool = False,
        frame: frames.FrameKind = "polars",
    ) -> None:
        """Set up the orchestrator; see the class docstring for the arguments."""
        self.backend = backend
        self.hooks = DEFAULT_HOOKS.merged(hooks) if hooks else DEFAULT_HOOKS
        self.registry = registry or HookRegistry()
        self.unit_mapper = unit_mapper or UnitMapper(
            resolver=backend.resolve_unit, strict=strict_units
        )
        if frame not in frames.FRAME_KINDS:
            raise ValueError(
                f"frame must be one of {frames.FRAME_KINDS}, not {frame!r}."
            )
        self.frame = frame
        self._study: str | None = None
        if study is not None:
            self.select_study(study)

    # Studies (D9)

    @property
    def study(self) -> str:
        """The selected study.

        Raises:
            StudyNotFoundError: If no study is selected.
        """
        if self._study is None:
            raise StudyNotFoundError(
                "No study is selected; call create_study() or select_study() first."
            )
        return self._study

    def create_study(self, name: str) -> str:
        """Create a study and select it.

        Raises:
            StudyExistsError: If the study exists. Use
                :meth:`get_or_create_study` when either is fine.
        """
        check_tag(name)
        if self.backend.has_study(name):
            raise StudyExistsError(
                f"Study {name!r} exists; use select_study({name!r}) to work in it."
            )
        self.backend.define_study(name)
        self._define_ledgers(name)
        self._study = name
        return name

    def select_study(self, name: str) -> str:
        """Select an existing study.

        Raises:
            StudyNotFoundError: If it doesn't exist.
        """
        if not self.backend.has_study(name):
            raise StudyNotFoundError(
                f"There is no study {name!r}; use create_study({name!r}) to make it."
            )
        self._define_ledgers(name)
        self._study = name
        return name

    def get_or_create_study(self, name: str) -> str:
        """Select the study, creating it if it doesn't exist."""
        if self.backend.has_study(name):
            return self.select_study(name)
        return self.create_study(name)

    # Datasets (D6)

    def store_data(
        self,
        name: str,
        df: Any,
        units: dict[str, str | None] | None = None,
        *,
        time_column: str | None = None,
    ) -> StoredData:
        """Store a table of measured data as the dataset ``name``.

        Each column becomes a timeseries with its unit, so it can be queried,
        converted and used to fit surrogates (D19). Timestamps are stored in
        UTC; naive ones are taken as UTC.

        Args:
            name: The dataset's name, unique among the study's datasets.
            df: A polars or pandas DataFrame of numeric columns.
            units: ``{column: unit}``, as QUDT keys (``"M3-PER-DAY"``) or as
                written (``"m3/day"``, ``"gal/min"``). Columns left out have no
                unit.
            time_column: The datetime column. For pandas the default is the
                index; for polars, the only datetime column.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If the dataset exists.
            StorageError: If the table can't be stored (no datetimes, repeated
                timestamps, a non-numeric column, an empty table).

        Example:
            >>> units = {"biogas_volume": "m3/day"}
            >>> orch.store_data("biogas_data", df, units=units)  # doctest: +SKIP
        """
        self._check_new(name, DATA_KIND)
        timestamps, columns = frames.to_columns(df, time_column)
        return datasets.store(
            self.backend,
            self._ledger(self.study, LEDGERS[DATA_KIND]),
            name,
            timestamps,
            columns,
            dict(units or {}),
            self.unit_mapper,
            self._metadata(name, DATA_KIND),
        )

    def get_data(
        self,
        name: str | None = None,
        columns: list[str] | None = None,
        start: Any = None,
        end: Any = None,
        *,
        column: str | None = None,
        frame: frames.FrameKind | None = None,
    ) -> Any:
        """Read a dataset, or one column found across the study's datasets.

        Args:
            name: The dataset.
            columns: Only these columns, in this order.
            start: Only rows at or after this datetime (naive means UTC).
            end: Only rows at or before this datetime.
            column: Instead of ``name``: a column name, looked up across the
                study's datasets.
            frame: ``"polars"`` or ``"pandas"``; the orchestrator's default if
                omitted.

        Returns:
            A polars DataFrame with a ``time`` column first, or a pandas
            DataFrame with a UTC ``DatetimeIndex``.

        Raises:
            TagNotFoundError: If the dataset, or a dataset with ``column``,
                doesn't exist.
            AmbiguousColumnError: If ``column`` is in several datasets.
        """
        if (name is None) == (column is None):
            raise TypeError("Give the dataset name, or column= to look one up.")
        if column is not None:
            name, columns = self._dataset_with(column), [column]
        ledger = self._ledger(self.study, LEDGERS[DATA_KIND])
        run = self._latest(name, DATA_KIND)
        timestamps, values = datasets.read(
            self.backend, ledger, run.run_id, name, columns
        )
        keep = [
            i
            for i, when in enumerate(timestamps)
            if (start is None or when >= to_utc(start))
            and (end is None or when <= to_utc(end))
        ]
        return frames.from_columns(
            [timestamps[i] for i in keep],
            {c: [v[i] for i in keep] for c, v in values.items()},
            frame or self.frame,
        )

    # Model results (D4)

    def store_model_results(
        self,
        tag: str,
        model: Any,
        outcome: SolveOutcome | None = None,
        keys: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        *,
        time_sets: Sequence[Any] | None = None,
        datetime_index: Sequence[Any] | None = None,
    ) -> StoredResult:
        """Export a model's values and store them under ``tag``.

        Values are exported with
        :func:`~flex_orchestrator.export.export_model_data` and checked against
        the study's declarations before the run starts, so a value that can't be
        stored leaves no run behind. If ``outcome`` isn't ``ok``, only the
        outcome is stored, in a ``failed`` run, so the tag can be used again.

        Args:
            tag: The result's tag, unique among the study's results.
            model: The model, passed explicitly (there is no current model).
            outcome: How the solve ended; stored as ``solve_outcome``.
            keys: Exact paths to export (see ``export_model_data``).
            include: Glob patterns to export.
            exclude: Glob patterns to leave out.
            time_sets: The model's time Sets. By default the ``time_sets`` hook
                gives them (for flex-pse models, including the partial-horizon
                Sets of startup/shutdown).
            datetime_index: One datetime per time point, for models whose time
                block doesn't have them.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded.
            ExportError, StorageError, UnitError: If the values can't be
                exported or stored.
        """
        self._check_new(tag)
        _check_outcome(outcome)
        data = None
        if outcome is None or outcome.ok:
            data = export_model_data(
                model,
                keys=keys,
                include=include,
                exclude=exclude,
                time_sets=(
                    time_sets if time_sets is not None else self._time_sets(model)
                ),
                datetime_index=datetime_index,
                unit_mapper=self.unit_mapper,
            )
        return self._store(tag, data, outcome)

    def store_model_data(
        self, tag: str, data: ModelData, outcome: SolveOutcome | None = None
    ) -> StoredResult:
        """Store values that are already exported under ``tag``.

        :meth:`store_model_results` is this after
        :func:`~flex_orchestrator.export.export_model_data`. Use it directly for
        values exported another way, e.g. from a model that isn't Pyomo.

        Args:
            tag: The result's tag, unique among the study's results.
            data: The values.
            outcome: How the solve ended. If it isn't ``ok``, ``data`` is
                ignored and only the outcome is stored, in a ``failed`` run.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded.
            StorageError, UnitError: If the values can't be stored.
        """
        self._check_new(tag)
        _check_outcome(outcome)
        return self._store(tag, data, outcome)

    def solve_and_store(
        self,
        tag: str,
        model: Any,
        keys: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        *,
        solve: Any = None,
        time_sets: Sequence[Any] | None = None,
        datetime_index: Sequence[Any] | None = None,
        **options: Any,
    ) -> StoredResult:
        """Solve a model and store its results under ``tag``.

        The tag is checked before solving, so a solve is never wasted on a tag
        that can't be stored. A solve that raises or isn't optimal is stored as
        a ``failed`` result holding only its outcome.

        Args:
            tag: The result's tag.
            model: The built model.
            keys: Exact paths to export.
            include: Glob patterns to export.
            exclude: Glob patterns to leave out.
            solve: The solve hook (a callable, import string or registered
                name); the orchestrator's ``solve`` hook by default.
            time_sets: See :meth:`store_model_results`.
            datetime_index: See :meth:`store_model_results`.
            **options: Passed to the solve hook.

        Returns:
            What was stored, with the outcome.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded.
        """
        self._check_new(tag)
        hook = self.registry.resolve_ref(solve or self.hooks.solve, "solve")
        outcome = run_solve(hook, model, **options)
        return self.store_model_results(
            tag,
            model,
            outcome,
            keys,
            include,
            exclude,
            time_sets=time_sets,
            datetime_index=datetime_index,
        )

    def model_results(self, tag: str) -> ModelData:
        """Read a stored result's model values back.

        Values come back in the units their labels were first declared with,
        with UTC timestamps.

        Raises:
            TagNotFoundError: If no succeeded result has this tag.
        """
        run = self._latest(tag, RESULT_KIND)
        return results.read_model_data(self.backend, self._runs, run.run_id)

    # Internals

    def _store(
        self, tag: str, data: ModelData | None, outcome: SolveOutcome | None
    ) -> StoredResult:
        writes = []
        if data is not None and (outcome is None or outcome.ok):
            writes = results.plan_writes(self.backend, self._runs, data)
            results.declare(self.backend, self._runs, writes)
        if outcome is not None:
            self.backend.declare(self._runs, SOLVE_OUTCOME, role="output", kind="json")
        run_id = self.backend.start_run(self._runs, self._metadata(tag))
        try:
            results.record(self.backend, run_id, writes)
            if outcome is not None:
                self.backend.record(
                    run_id, SOLVE_OUTCOME, outcome.model_dump(mode="json")
                )
        except BaseException as exc:
            self.backend.fail_run(run_id, exc)
            raise
        if outcome is not None and not outcome.ok:
            self.backend.fail_run(
                run_id,
                SolveNotOptimal(
                    f"solve {outcome.status} ({outcome.termination}): {outcome.message}"
                ),
            )
            return StoredResult(tag, run_id, "failed", outcome)
        self.backend.finish_run(run_id)
        return StoredResult(
            tag, run_id, "succeeded", outcome, [w.series.key for w in writes]
        )

    @property
    def _runs(self) -> str:
        return self._ledger(self.study, LEDGERS[RESULT_KIND])

    @staticmethod
    def _ledger(study: str, name: str) -> str:
        return f"{study}::{name}"

    def _define_ledgers(self, study: str) -> None:
        for ledger in LEDGERS.values():
            self.backend.define_study(self._ledger(study, ledger))

    def _check_new(self, tag: str, kind: str = RESULT_KIND) -> None:
        check_tag(tag)
        ledger = self._ledger(self.study, LEDGERS[kind])
        for run in self.backend.runs(ledger, metadata=_tag_metadata(tag, kind)):
            if run.status in ("running", "succeeded"):
                raise TagExistsError(
                    f"Study {self.study!r} already has a {run.status} {kind} "
                    f"{tag!r} (run {run.run_id})."
                )

    def _latest(self, tag: str, kind: str) -> Any:
        ledger = self._ledger(self.study, LEDGERS[kind])
        found = self.backend.runs(
            ledger, status="succeeded", metadata=_tag_metadata(tag, kind)
        )
        if not found:
            raise TagNotFoundError(
                f"Study {self.study!r} has no stored {kind} {tag!r}."
            )
        return found[-1]

    def _dataset_with(self, column: str) -> str:
        ledger = self._ledger(self.study, LEDGERS[DATA_KIND])
        stored = {
            run.metadata["flexorch_tag"]
            for run in self.backend.runs(
                ledger, status="succeeded", metadata={"flexorch_kind": DATA_KIND}
            )
        }
        found = sorted(
            {
                v.metadata.get("dataset")
                for v in self.backend.variables(ledger)
                if v.metadata.get("column") == column
            }
            & stored
        )
        if not found:
            raise TagNotFoundError(
                f"No dataset in study {self.study!r} has a column {column!r}."
            )
        if len(found) > 1:
            raise AmbiguousColumnError(
                f"Column {column!r} is in datasets {found}; name the dataset, e.g. "
                f"get_data({found[0]!r}, columns=[{column!r}])."
            )
        return found[0]

    def _metadata(self, tag: str, kind: str = RESULT_KIND) -> dict[str, str]:
        return {
            **_tag_metadata(tag, kind),
            "flexorch_version": version("flex-orchestrator"),
        }

    def _time_sets(self, model: Any) -> list[Any] | None:
        ref = self.hooks.time_sets
        if ref is None:
            return None
        try:
            hook = self.registry.resolve_ref(ref, "time_sets")
        except HookError:
            if ref == DEFAULT_HOOKS.time_sets:
                # flex-pse isn't installed. The exporter still finds a
                # TimeBlock by duck typing; only flex-pse's partial-horizon
                # Sets need the hook.
                return None
            raise
        return hook(model) or None


def _check_outcome(outcome: Any) -> None:
    if outcome is not None and not isinstance(outcome, SolveOutcome):
        raise TypeError(
            f"outcome must be a SolveOutcome, not {type(outcome).__name__}."
        )


def _tag_metadata(tag: str, kind: str = RESULT_KIND) -> dict[str, str]:
    return {"flexorch_kind": kind, "flexorch_tag": tag}
