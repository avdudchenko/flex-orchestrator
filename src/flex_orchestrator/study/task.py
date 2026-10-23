"""One task of a study: build a model from a stored spec, solve it, store it.

A :class:`TaskSpec` is plain data (hooks as import strings or registered
names), so it can be sent to a Ray worker (M5) or written to a dry-run plan.
:func:`run_task` runs it in this process and returns a :class:`TaskResult`.

A task with configuration options or surrogates first stores a derived model
spec under its own tag, with the base as parent: the base spec with each
option set by the ``configure`` hook, then each surrogate applied by the
``apply_surrogate`` hook. A derived spec that is already stored and identical
is reused, so a resumed study doesn't trip over its own specs.
"""

import copy
import traceback
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flex_orchestrator.errors import TagExistsError
from flex_orchestrator.export import UnitMapper
from flex_orchestrator.hooks import HookSet, SolveOutcome
from flex_orchestrator.orchestrator import MODEL_SPEC, RESULT_KIND
from flex_orchestrator.specs import normalise_spec
from flex_orchestrator.study.config import ExportConfig


class SurrogateChoice(BaseModel):
    """One surrogate a task applies.

    Attributes:
        unit: The unit's path.
        relation: The relation it replaces; the hook's default if ``None``.
        surrogate: The surrogate spec's tag.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    unit: str
    relation: str | None = None
    surrogate: str


class TaskSpec(BaseModel):
    """One planned build-solve-store.

    Attributes:
        index: Its position in the study, from 0.
        tag: The result's tag (and the derived spec's, with surrogates).
        base: The model spec it builds from (or derives from).
        configuration: ``{option path: value}``, set in ``base`` first.
        surrogates: Surrogates applied to ``base`` (after the configuration).
        overrides: Passed to the build hook: the variables to fix.
        labels: The values ``output_name`` was rendered with.
        hooks: Hook import strings or registered names by slot; unset slots
            use the orchestrator's.
        export: What the result stores.
        solve_options: Passed to the solve hook.
        on_existing: ``error``, ``skip`` or ``replace``.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    tag: str
    base: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    surrogates: list[SurrogateChoice] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    hooks: dict[str, str] = Field(default_factory=dict)
    export: ExportConfig = Field(default_factory=ExportConfig)
    solve_options: dict[str, Any] = Field(default_factory=dict)
    on_existing: Literal["error", "skip", "replace"] = "error"

    @property
    def spec_tag(self) -> str:
        """The model spec it builds: its own tag when it derives one."""
        return self.tag if self.derives else self.base

    @property
    def derives(self) -> bool:
        """Whether it stores a derived spec (it sets options or surrogates)."""
        return bool(self.configuration or self.surrogates)


class TaskResult(BaseModel):
    """How one task ended.

    Attributes:
        index: The task's position.
        tag: The result's tag.
        status: ``succeeded``, ``failed`` (the solve ended without an optimal
            solution; stored as a failed result), ``error`` (building or
            storing raised, or the solve hook did; a raising solve is stored
            as a failed result too) or ``skipped`` (already stored, with
            ``on_existing: skip``).
        spec_tag: The model spec it was built from.
        run_id: The result run, if one was stored.
        outcome: The solve outcome.
        error: The error, for ``error``.
        attempts: How many times it was tried.
    """

    index: int
    tag: str
    status: Literal["succeeded", "failed", "error", "skipped"]
    spec_tag: str | None = None
    run_id: str | None = None
    outcome: SolveOutcome | None = None
    error: str | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        """Whether the task stored a result (or one was already stored)."""
        return self.status in ("succeeded", "skipped")


def run_task(
    orch: Any, task: TaskSpec, runtime: HookSet | None = None, retries: int = 0
) -> TaskResult:
    """Run one task in this process.

    Args:
        orch: The orchestrator, with the task's study selected.
        task: The task.
        runtime: Hooks (callables allowed) that win over ``task.hooks``.
        retries: Tries again this many times when building, solving or storing
            raises, or the solve hook raised (an ``error`` outcome).

    Returns:
        The result; an exception becomes an ``error`` result, so one task
        doesn't end the study.

    Raises:
        TagExistsError: If the tag (or a different derived spec) is stored and
            ``on_existing`` doesn't allow it; that's a config problem, not a
            task failure.
    """
    hooks = HookSet(**task.hooks).merged(runtime or HookSet())
    if task.on_existing == "skip" and _stored(orch, task.tag):
        return TaskResult(
            index=task.index, tag=task.tag, status="skipped", spec_tag=task.spec_tag
        )
    replace = task.on_existing == "replace"
    attempts, error = 0, None
    while attempts <= retries:
        attempts += 1
        try:
            spec_tag = _spec(orch, task, hooks, replace)
            model = orch.build(spec_tag, build=hooks.build, **task.overrides)
            with _strict_units(orch, task.export.strict_units):
                stored = orch.solve_and_store(
                    task.tag,
                    model,
                    keys=task.export.keys,
                    include=task.export.include,
                    exclude=task.export.exclude,
                    streams=task.export.streams,
                    replace=replace,
                    solve=hooks.solve,
                    **task.solve_options,
                )
        except TagExistsError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            continue
        crashed = stored.outcome is not None and stored.outcome.status == "error"
        if crashed and attempts <= retries:
            continue
        status = "succeeded" if stored.ok else "error" if crashed else "failed"
        return TaskResult(
            index=task.index,
            tag=task.tag,
            status=status,
            spec_tag=spec_tag,
            run_id=stored.run_id,
            outcome=stored.outcome,
            error=None if stored.ok else stored.outcome.message,
            attempts=attempts,
        )
    return TaskResult(
        index=task.index,
        tag=task.tag,
        status="error",
        spec_tag=task.spec_tag,
        error=error,
        attempts=attempts,
    )


def _stored(orch: Any, tag: str) -> bool:
    try:
        orch._latest(tag, RESULT_KIND)
    except LookupError:
        return False
    return True


def _spec(orch: Any, task: TaskSpec, hooks: HookSet, replace: bool) -> str:
    """Return the spec tag to build, storing the derived spec first if needed."""
    if not task.derives:
        return task.base
    spec = copy.deepcopy(orch.model_spec(task.base).spec)
    if task.configuration:
        configure = _hook(orch, hooks, "configure")
        for path, value in task.configuration.items():
            spec = configure(spec, path, value)
    if task.surrogates:
        apply = _hook(orch, hooks, "apply_surrogate")
        for choice in task.surrogates:
            surrogate = orch.surrogate_spec(choice.surrogate).spec
            spec = apply(spec, choice.unit, surrogate, choice.relation)
    try:
        orch.store_model_spec(task.tag, spec, parent=task.base, replace=replace)
    except TagExistsError:
        stored = orch.model_spec(task.tag)
        data, _ = normalise_spec(MODEL_SPEC, spec)
        if stored.parent_tag != task.base or stored.spec != data:
            raise TagExistsError(
                f"Model spec {task.tag!r} is stored and differs from the one this "
                "task derives; use on_existing: replace, or another output_name."
            ) from None
    return task.tag


def _hook(orch: Any, hooks: HookSet, kind: str) -> Any:
    return orch.registry.resolve_ref(
        getattr(hooks, kind) or getattr(orch.hooks, kind), kind
    )


class _strict_units:
    """Make the orchestrator's unit mapper strict for one store."""

    def __init__(self, orch: Any, strict: bool) -> None:
        self.orch, self.strict, self.saved = orch, strict, None

    def __enter__(self) -> None:
        if self.strict and not self.orch.unit_mapper.strict:
            self.saved = self.orch.unit_mapper
            self.orch.unit_mapper = UnitMapper(
                overrides=self.saved.overrides,
                resolver=self.saved.resolver,
                strict=True,
                base_units=self.saved.base_units,
            )

    def __exit__(self, *exc: object) -> None:
        if self.saved is not None:
            self.orch.unit_mapper = self.saved
