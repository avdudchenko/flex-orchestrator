"""One task of a study: build a model from a stored spec, solve it, store it.

A :class:`TaskSpec` is plain data (hooks as import strings or registered
names), so it can be written to a dry-run plan. Running one has three steps,
split so that a Ray worker (D13) needs only plain data and never the store:

1. :func:`prepare` (main process) checks the tag, stores the derived spec if
   the task has one, and reads the spec and its files into a :class:`Work`.
2. :func:`execute` (in this process or on a worker) builds, solves and
   exports, and returns a :class:`Done`: the outcome and the values.
3. :func:`finish` (main process) maps the units and stores the result.

:func:`run_task` runs all three here, with retries; ``execute_study`` runs
them on Ray when ``max_workers > 1``. Both store the same results, and both
retry only :func:`execute`: a raising :func:`prepare` or :func:`finish` is a
storage problem, and the task ends as an ``error`` result at once.

A task with configuration options or surrogates stores a derived model spec
under its own tag, with the base as parent: the base spec with each option
set by the ``configure`` hook, then each surrogate applied by the
``apply_surrogate`` hook. A derived spec that is already stored and identical
is reused, so a resumed study doesn't trip over its own specs.
"""

import copy
import dataclasses
import tempfile
import time
import traceback
import warnings
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flex_orchestrator.assets import materialize_spec
from flex_orchestrator.errors import HookError, TagExistsError
from flex_orchestrator.export import ModelData, UnitMapper, export_model_data
from flex_orchestrator.hooks import (
    DEFAULT_HOOKS,
    HookRegistry,
    HookSet,
    SolveOutcome,
    hook_ref,
    run_solve,
)
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
        seconds: Wall times of its last attempt: ``build`` (with writing the
            spec's files), ``solve``, ``export`` and ``store``.
        rss_mb: The memory of the process that built and solved it, after
            exporting, in MB.
    """

    index: int
    tag: str
    status: Literal["succeeded", "failed", "error", "skipped"]
    spec_tag: str | None = None
    run_id: str | None = None
    outcome: SolveOutcome | None = None
    error: str | None = None
    attempts: int = 0
    seconds: dict[str, float] = Field(default_factory=dict)
    rss_mb: float | None = None

    @property
    def ok(self) -> bool:
        """Whether the task stored a result (or one was already stored)."""
        return self.status in ("succeeded", "skipped")


@dataclass
class SpecBundle:
    """A stored model spec with its files: what a build needs, as plain data."""

    tag: str
    spec_hash: str
    spec: dict[str, Any]
    assets: dict[str, Any]
    files: dict[str, bytes]


@dataclass
class Work:
    """What :func:`execute` needs: plain data that pickles, no credentials.

    Attributes:
        task: The task.
        hooks: The ``build``, ``solve`` and ``time_sets`` hooks: callables in
            this process, import strings on Ray.
        selection: The export selection, ``{"keys", "include", "exclude"}``.
        unit_overrides: The orchestrator's ``{pyomo unit: QUDT key}`` overrides.
        base_units: The orchestrator's base-unit symbols.
        bundle: The spec to build; on Ray it's sent separately, once per spec.
    """

    task: TaskSpec
    hooks: HookSet
    selection: dict[str, Any]
    unit_overrides: dict[str, str]
    base_units: dict[str, str]
    bundle: SpecBundle | None = None


@dataclass
class Done:
    """What :func:`execute` returns.

    Attributes:
        outcome: How the solve ended; ``None`` if building raised.
        data: The exported values, for an ``ok`` solve. Their QUDT keys are
            unverified: :func:`finish` maps the units again in the main process.
        error: The error, if building or exporting raised (with the
            traceback), a hook couldn't be resolved, or the Ray worker died.
        seconds: ``{"build", "solve", "export"}`` wall times.
        rss_mb: The process's memory after exporting, in MB, if ``psutil``
            is installed (Ray brings it).
    """

    outcome: SolveOutcome | None = None
    data: ModelData | None = None
    error: str | None = None
    seconds: dict[str, float] = field(default_factory=dict)
    rss_mb: float | None = None


def study_hooks(
    orch: Any, task_hooks: dict[str, str], runtime: HookSet | None, portable: bool
) -> HookSet:
    """The hooks a study's tasks use: runtime over config over the orchestrator's.

    Registered names are resolved here, with the orchestrator's registry:
    :func:`execute` resolves hooks without one, in this process or on a worker.

    Args:
        orch: The orchestrator.
        task_hooks: The config's hooks.
        runtime: Hooks given to ``execute_study`` (callables allowed).
        portable: Return import strings, for Ray workers.

    Returns:
        Every set slot as an import string or a callable (import strings
        only with ``portable``).

    Raises:
        HookError: If a registered name isn't registered (or its set has no
            hook of that kind), or, with ``portable``, a hook can't be imported
            by name (a lambda, a closure, or a function defined in
            ``__main__``).
    """
    hooks = orch.hooks.merged(HookSet(**task_hooks)).merged(runtime or HookSet())
    refs = {}
    for kind, ref in hooks.slots().items():
        # Import strings stay strings: they're imported where they run.
        if ref is None or (isinstance(ref, str) and ":" in ref):
            refs[kind] = ref
            continue
        refs[kind] = orch.registry.resolve_ref(ref, kind)
        if not portable:
            continue
        # Not HookSet.to_refs(): this message names the registered name.
        refs[kind] = hook_ref(refs[kind])
        if refs[kind] is None:
            raise HookError(
                f"The {kind} hook {ref!r} can't run on a Ray worker: workers "
                "import hooks by name. Define it at module level in an "
                "importable module (not the script you run), or pass its import "
                "string ('package.module:function')."
            )
    return HookSet(**refs)


def prepare(orch: Any, task: TaskSpec, hooks: HookSet) -> Work:
    """Get a task ready to execute: store its derived spec and read the spec.

    Runs in the main process, which holds the store.

    Args:
        orch: The orchestrator, with the task's study selected.
        task: The task.
        hooks: The study's hooks (:func:`study_hooks`).

    Returns:
        The task's :class:`Work`, holding the spec and its files.

    Raises:
        TagExistsError: If the result's tag (or a different derived spec) is
            stored and ``on_existing`` doesn't allow it.
    """
    replace = task.on_existing == "replace"
    orch._check_new(task.tag, RESULT_KIND, replace)
    spec_tag = _spec(orch, task, hooks, replace)
    record, files = orch._read_spec(MODEL_SPEC, spec_tag)
    selection = task.export.model_dump(include={"keys", "include", "exclude"})
    if all(v is None for v in selection.values()):
        selection = {**selection, **orch.default_export_selection()}
    mapper = orch.unit_mapper
    return Work(
        task=task,
        hooks=HookSet(build=hooks.build, solve=hooks.solve, time_sets=hooks.time_sets),
        selection=selection,
        unit_overrides=dict(mapper.overrides),
        base_units=dict(mapper.base_units),
        bundle=SpecBundle(
            spec_tag, record.spec_hash, record.spec, dict(record.assets), files
        ),
    )


def execute(work: Work, bundle: SpecBundle | None = None) -> Done:
    """Build, solve and export one task; never touches the store.

    This is what a Ray worker runs (D13): the model never leaves the process,
    and only plain data comes back. Exceptions from building or exporting are
    returned as ``error``; a solve hook that raises gives an ``error`` outcome.

    Args:
        work: From :func:`prepare`.
        bundle: The spec, if not in ``work`` (Ray sends it once per spec).
    """
    bundle = bundle or work.bundle
    registry = HookRegistry()
    done = Done()
    clock = time.perf_counter()
    try:
        build = registry.resolve_ref(work.hooks.build, "build")
        with tempfile.TemporaryDirectory(prefix="flexorch-spec-") as directory:
            spec = materialize_spec(bundle.spec, bundle.assets, bundle.files, directory)
            model = build(spec, **work.task.overrides)
        done.seconds["build"] = time.perf_counter() - clock
        solve = registry.resolve_ref(work.hooks.solve, "solve")
        done.outcome = run_solve(solve, model, **work.task.solve_options)
        done.seconds["solve"] = done.outcome.wall_time or 0.0
        if done.outcome.ok:
            clock = time.perf_counter()
            mapper = UnitMapper(
                overrides=work.unit_overrides, base_units=work.base_units
            )
            with warnings.catch_warnings():
                # The main process maps the units again, and warns there.
                warnings.simplefilter("ignore", UserWarning)
                done.data = export_model_data(
                    model,
                    **work.selection,
                    time_sets=_time_sets(registry, work.hooks.time_sets, model),
                    unit_mapper=mapper,
                )
            done.seconds["export"] = time.perf_counter() - clock
    except Exception as exc:
        done.error = error_text(exc)
    done.rss_mb = _rss_mb()
    return done


def finish(orch: Any, work: Work, done: Done, attempts: int) -> TaskResult:
    """Store what :func:`execute` returned; runs in the main process.

    A task whose build raised stores nothing. Otherwise the outcome is stored
    (a failed run for a solve that isn't optimal), with the values for an
    ``ok`` one, their units mapped by the orchestrator's mapper.

    Args:
        orch: The orchestrator.
        work: The task's :class:`Work`, from :func:`prepare`.
        done: What :func:`execute` returned.
        attempts: How many times the task was executed.

    Returns:
        The task's result: ``succeeded``, ``failed`` or ``error``.
    """
    task = work.task
    common = {
        "index": task.index,
        "tag": task.tag,
        "attempts": attempts,
        "seconds": done.seconds,
        "rss_mb": done.rss_mb,
    }
    spec_tag = task.spec_tag
    if done.error is not None:
        return TaskResult(**common, status="error", spec_tag=spec_tag, error=done.error)
    data = done.data
    clock = time.perf_counter()
    with _strict_units(orch, task.export.strict_units), orch._using_hooks(work.hooks):
        if data is not None:
            data = _remap_units(orch.unit_mapper, data)
        stored = orch.store_model_data(
            task.tag,
            data,
            done.outcome,
            streams=task.export.streams,
            replace=task.on_existing == "replace",
            spec=spec_tag,
            overrides=task.overrides,
        )
    done.seconds["store"] = time.perf_counter() - clock
    crashed = stored.outcome is not None and stored.outcome.status == "error"
    status = "succeeded" if stored.ok else "error" if crashed else "failed"
    return TaskResult(
        **common,
        status=status,
        spec_tag=spec_tag,
        run_id=stored.run_id,
        outcome=stored.outcome,
        error=None if stored.ok else stored.outcome.message,
    )


def retryable(done: Done) -> bool:
    """Whether a task that ended so is tried again (if it has retries left)."""
    return done.error is not None or (
        done.outcome is not None and done.outcome.status == "error"
    )


def run_task(
    orch: Any,
    task: TaskSpec,
    runtime: HookSet | None = None,
    retries: int = 0,
    hooks: HookSet | None = None,
) -> TaskResult:
    """Run one task in this process: :func:`prepare`, :func:`execute`, :func:`finish`.

    Args:
        orch: The orchestrator, with the task's study selected.
        task: The task.
        runtime: Hooks (callables allowed) that win over ``task.hooks``.
        retries: Executes it again this many times when building or exporting
            raises, or the solve hook raised (an ``error`` outcome). Reading or
            storing its spec, and storing its result, aren't retried.
        hooks: The resolved hooks (:func:`study_hooks`), instead of ``runtime``.

    Returns:
        The result; an exception becomes an ``error`` result, so one task
        doesn't end the study.

    Raises:
        TagExistsError: If the tag (or a different derived spec) is stored and
            ``on_existing`` doesn't allow it; that's a config problem, not a
            task failure.
    """
    if task.on_existing == "skip" and stored(orch, task.tag):
        return skipped(task)
    attempts = 1
    try:
        hooks = hooks or study_hooks(orch, task.hooks, runtime, portable=False)
        work = prepare(orch, task, hooks)
        done = execute(work)
        while retryable(done) and attempts <= retries:
            done, attempts = execute(work), attempts + 1
        return finish(orch, work, done, attempts)
    except TagExistsError:
        raise
    except Exception as exc:
        return errored(task, error_text(exc), attempts)


def skipped(task: TaskSpec) -> TaskResult:
    """The result of a task skipped because its tag is stored."""
    return TaskResult(
        index=task.index, tag=task.tag, status="skipped", spec_tag=task.spec_tag
    )


def report(
    orch: Any, progress: Any, execution: Any, results: list, result: TaskResult
) -> bool:
    """Add a finished task's result to ``results`` and tell everyone.

    Calls ``orch.on_task_result`` and advances ``progress``.

    Returns:
        Whether the study stops here (the task failed, with ``on_error: stop``).
    """
    results.append(result)
    orch.on_task_result(result)
    progress.advance(message=f"{result.tag}: {result.status}")
    return result.status in ("failed", "error") and execution.on_error == "stop"


def errored(task: TaskSpec, error: str, attempts: int) -> TaskResult:
    """The ``error`` result of a task that raised outside :func:`execute`."""
    return TaskResult(
        index=task.index,
        tag=task.tag,
        status="error",
        spec_tag=task.spec_tag,
        error=error,
        attempts=attempts,
    )


def error_text(exc: BaseException) -> str:
    """An exception as a task's ``error``: its type, message and traceback."""
    return f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def stored(orch: Any, tag: str, kind: str = RESULT_KIND) -> bool:
    """Whether ``tag`` has a stored record of ``kind`` (a succeeded run)."""
    try:
        orch._latest(tag, kind)
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


def _time_sets(registry: HookRegistry, ref: Any, model: Any) -> list[Any] | None:
    if ref is None:
        return None
    try:
        hook = registry.resolve_ref(ref, "time_sets")
    except HookError:
        if ref == DEFAULT_HOOKS.time_sets:
            return None  # flex-pse isn't installed; the exporter duck-types time
        raise
    return hook(model) or None


def _rss_mb() -> float | None:
    try:
        import psutil
    except ImportError:  # pragma: no cover - Ray installs psutil
        return None
    return psutil.Process().memory_info().rss / 2**20


def _remap_units(mapper: UnitMapper, data: ModelData) -> ModelData:
    """Map every series' units with ``mapper`` (the worker's couldn't verify)."""
    series = []
    for s in data:
        mapping = mapper.map(s.unit_pyomo)
        series.append(
            dataclasses.replace(
                s, unit_qudt=mapping.qudt, unit_verified=mapping.verified
            )
        )
    return ModelData(series)


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
