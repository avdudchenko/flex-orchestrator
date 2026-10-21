"""The orchestrator: studies and model results on a storage backend (PLAN.md D4).

Model results are stored **explicitly**: every call names the model and the
tag, and there is no "current model" (D4). A study ``s`` is laid out as
backend studies (D10): ``s`` itself, and ``s::runs``, where each stored result
is one run carrying ``{flexorch_kind: "result", flexorch_tag: tag}``, the
exported model values as outputs, and the ``solve_outcome`` JSON.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Any, Literal

from flex_orchestrator import results
from flex_orchestrator.backend.base import StorageBackend
from flex_orchestrator.errors import (
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
    ) -> None:
        """Set up the orchestrator; see the class docstring for the arguments."""
        self.backend = backend
        self.hooks = DEFAULT_HOOKS.merged(hooks) if hooks else DEFAULT_HOOKS
        self.registry = registry or HookRegistry()
        self.unit_mapper = unit_mapper or UnitMapper(
            resolver=backend.resolve_unit, strict=strict_units
        )
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
        self.backend.define_study(self._ledger(name, "runs"))
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
        self.backend.define_study(self._ledger(name, "runs"))
        self._study = name
        return name

    def get_or_create_study(self, name: str) -> str:
        """Select the study, creating it if it doesn't exist."""
        if self.backend.has_study(name):
            return self.select_study(name)
        return self.create_study(name)

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
        if outcome is not None and not isinstance(outcome, SolveOutcome):
            raise TypeError(
                f"outcome must be a SolveOutcome, not {type(outcome).__name__}."
            )
        writes = []
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
        found = self.backend.runs(
            self._runs, status="succeeded", metadata=_tag_metadata(tag)
        )
        if not found:
            raise TagNotFoundError(
                f"Study {self.study!r} has no stored result {tag!r}."
            )
        return results.read_model_data(self.backend, self._runs, found[-1].run_id)

    # Internals

    @property
    def _runs(self) -> str:
        return self._ledger(self.study, "runs")

    @staticmethod
    def _ledger(study: str, name: str) -> str:
        return f"{study}::{name}"

    def _check_new(self, tag: str) -> None:
        check_tag(tag)
        for run in self.backend.runs(self._runs, metadata=_tag_metadata(tag)):
            if run.status in ("running", "succeeded"):
                raise TagExistsError(
                    f"Study {self.study!r} already has a {run.status} result "
                    f"{tag!r} (run {run.run_id})."
                )

    def _metadata(self, tag: str) -> dict[str, str]:
        return {**_tag_metadata(tag), "flexorch_version": version("flex-orchestrator")}

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


def _tag_metadata(tag: str) -> dict[str, str]:
    return {"flexorch_kind": RESULT_KIND, "flexorch_tag": tag}
