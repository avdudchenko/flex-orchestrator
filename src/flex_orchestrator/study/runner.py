"""The built-in ``execute_study`` command: run a :class:`StudyConfig` (PLAN.md M4).

1. The config is expanded into tasks (:func:`expand`): the sweeps' values are
   combined (``product`` or ``zip``) and each combination's tag is rendered
   from ``output_name``. Repeated or invalid tags fail here, before anything
   runs; ``dry_run`` stops here and returns the plan.
2. With ``on_existing: error``, every task's tag is checked; a stored one
   raises before anything runs.
3. ``fits`` run (``fit_surrogate``), so the sweeps can use their surrogates.
4. The tasks run: one after another in this process
   (:func:`~flex_orchestrator.study.task.run_task`), or with
   ``max_workers > 1`` on Ray (:func:`flex_orchestrator.parallel.run_tasks`),
   where workers build and solve and this process stores. Either way the
   stored results are the same.

The command returns a :class:`StudyRun`; the call, its parameters and its
summary are recorded in ``flexorch::commands`` like every command's.
"""

import itertools
import re
from typing import Any

from pydantic import BaseModel, Field

from flex_orchestrator.errors import SpecError, StudyConfigError
from flex_orchestrator.ext.commands import Progress, command
from flex_orchestrator.fitting.command import run_fits
from flex_orchestrator.fitting.models import FitSummary
from flex_orchestrator.hooks import HookSet
from flex_orchestrator.orchestrator import RESULT_KIND
from flex_orchestrator.specs import check_tag
from flex_orchestrator.study.config import (
    Case,
    CasesSweep,
    ConfigurationSweep,
    SpecListSweep,
    StudyConfig,
    SurrogateSweep,
    VariableSweep,
)
from flex_orchestrator.study.task import (
    SurrogateChoice,
    TaskResult,
    TaskSpec,
    run_task,
    study_hooks,
)

_FIELD = re.compile(r"\{([^{}]+)\}")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


class ExecuteParams(BaseModel):
    """The parameters of ``execute_study``.

    Attributes:
        config: The study config.
        dry_run: Return the plan without storing or solving anything.
        max_workers: Overrides ``config.execution.max_workers``.
        ray_address: Overrides ``config.execution.ray_address``.
    """

    config: StudyConfig
    dry_run: bool = False
    max_workers: int | None = Field(default=None, ge=1)
    ray_address: str | None = None


class StudyRun(BaseModel):
    """What ``execute_study`` did, or (``dry_run``) would do.

    Attributes:
        study: The study it ran in.
        dry_run: Whether this is only the plan.
        planned_fits: The tags ``fits`` stores (or would store).
        fits: What each ``fits`` entry stored, in order.
        tasks: The planned tasks.
        results: How each task ended, in order (empty for a dry run).
    """

    study: str
    dry_run: bool = False
    planned_fits: list[str] = Field(default_factory=list)
    fits: list[FitSummary] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(default_factory=list)
    results: list[TaskResult] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Return how many tasks ended each way, ``{status: count}``."""
        found: dict[str, int] = {}
        for result in self.results:
            found[result.status] = found.get(result.status, 0) + 1
        return found

    def result(self, tag: str) -> TaskResult:
        """Return the result of the task tagged ``tag``."""
        for result in self.results:
            if result.tag == tag:
                return result
        raise KeyError(tag)


@command(name="execute_study", long_running=True)
def execute_study(
    orch: Any,
    params: ExecuteParams,
    progress: Progress,
    build: Any = None,
    solve: Any = None,
) -> StudyRun:
    """Run a study config: its fits, then every task of its sweeps."""
    with orch._caching_specs():  # every task reads the same few specs
        return _execute(orch, params, progress, build, solve)


def _execute(
    orch: Any, params: ExecuteParams, progress: Progress, build: Any, solve: Any
) -> StudyRun:
    cfg = params.config
    execution = cfg.execution
    address = params.ray_address or execution.ray_address or orch.ray_address
    if address != execution.ray_address:
        execution = execution.model_copy(update={"ray_address": address})
    workers = params.max_workers or execution.max_workers
    # Resolved before anything runs, so a hook Ray can't import fails early.
    hooks = study_hooks(
        orch,
        cfg.hooks.model_dump(exclude_none=True),
        HookSet(build=build, solve=solve),
        portable=workers > 1,
    )
    if cfg.study is not None:
        orch.get_or_create_study(cfg.study)
    tasks = expand(cfg)
    planned_fits = [plan.tag for fit in cfg.fits for plan in fit.expand()]
    run = StudyRun(study=orch.study, planned_fits=planned_fits, tasks=tasks)
    if params.dry_run:
        run.dry_run = True
        return run
    if cfg.on_existing == "error":
        for task in tasks:
            orch._check_new(task.tag, RESULT_KIND)
    progress.update(done=0, total=len(cfg.fits) + len(tasks), message="fitting")
    for fit in cfg.fits:
        progress.check()
        run.fits.append(run_fits(orch, fit, Progress(), execution, workers))
        progress.advance(message="fitted " + ", ".join(run.fits[-1].tags))
    if workers > 1:
        from flex_orchestrator.parallel import run_tasks

        run.results = run_tasks(orch, tasks, hooks, execution, workers, progress)
    for task in tasks if workers == 1 else ():
        progress.check()
        result = run_task(orch, task, retries=execution.retries, hooks=hooks)
        run.results.append(result)
        orch.on_task_result(result)
        progress.advance(message=f"{task.tag}: {result.status}")
        if result.status in ("failed", "error") and execution.on_error == "stop":
            break
    orch.events.emit("job_finished", job=run)
    return run


EXECUTE_STUDY = execute_study


def expand(cfg: StudyConfig) -> list[TaskSpec]:
    """Return the config's tasks, in order.

    Raises:
        StudyConfigError: If there's no base spec, zipped sweeps differ in
            length, two sweeps share a field name or set the same thing,
            ``output_name`` names an unknown field, or a tag is invalid or
            repeated.
    """
    axes = [_values(sweep) for sweep in cfg.sweeps]
    if cfg.combine == "zip" and len({len(axis) for axis in axes}) > 1:
        lengths = [len(axis) for axis in axes]
        raise StudyConfigError(
            f"combine: zip needs sweeps of one length; they have {lengths}."
        )
    combos = (
        zip(*axes, strict=True) if cfg.combine == "zip" else itertools.product(*axes)
    )
    hooks = cfg.hooks.model_dump(exclude_none=True)
    tasks, tags = [], set()
    for index, combo in enumerate(combos):
        task = _Choices(cfg.base_model_spec)
        for sweep, value in combo:
            field = _field(sweep)
            if field in task.labels:
                raise StudyConfigError(
                    f"Two sweeps both render as {{{field}}}; give them names."
                )
            task.labels[field] = value.name if isinstance(value, Case) else value
            task.add(sweep, value)
        if task.base is None:
            raise StudyConfigError(
                "The config has no base_model_spec and no spec_list sweep."
            )
        tag = _render(cfg, task.base, index, task.labels)
        if tag in tags:
            raise StudyConfigError(
                f"Two tasks would be stored as {tag!r}; put every sweep's field "
                "in output_name, or format its values more precisely."
            )
        tags.add(tag)
        tasks.append(
            TaskSpec(
                index=index,
                tag=tag,
                base=task.base,
                configuration=task.configuration,
                surrogates=task.surrogates,
                overrides=task.variables,
                labels=task.labels,
                hooks=hooks,
                export=cfg.export,
                solve_options=cfg.solve_options,
                on_existing=cfg.on_existing,
            )
        )
    return tasks


class _Choices:
    """What one combination of sweep values sets, checked for clashes."""

    def __init__(self, base: str | None) -> None:
        self.base, self.spec_swept = base, False
        self.configuration: dict[str, Any] = {}
        self.variables: dict[str, Any] = {}
        self.surrogates: list[SurrogateChoice] = []
        self.labels: dict[str, Any] = {}

    def add(self, sweep: Any, value: Any) -> None:
        if isinstance(sweep, SurrogateSweep):
            self.surrogate(sweep.unit, value, sweep.relation)
        elif isinstance(sweep, VariableSweep):
            self.set(self.variables, "variable", sweep.path, value)
        elif isinstance(sweep, ConfigurationSweep):
            self.set(self.configuration, "configuration option", sweep.path, value)
        elif isinstance(sweep, SpecListSweep):
            self.spec(value)
        else:
            if value.spec is not None:
                self.spec(value.spec)
            for unit, surrogate in value.surrogates.items():
                self.surrogate(unit, surrogate, None)
            for path, option in value.configuration_options.items():
                self.set(self.configuration, "configuration option", path, option)
            for path, fixed in value.variables.items():
                self.set(self.variables, "variable", path, fixed)

    def set(self, into: dict, what: str, path: str, value: Any) -> None:
        if path in into:
            raise StudyConfigError(f"Two sweeps both set the {what} {path!r}.")
        into[path] = value

    def surrogate(self, unit: str, tag: str, relation: str | None) -> None:
        if any(s.unit == unit and s.relation == relation for s in self.surrogates):
            raise StudyConfigError(f"Two sweeps both pick the surrogate of {unit!r}.")
        self.surrogates.append(
            SurrogateChoice(unit=unit, relation=relation, surrogate=tag)
        )

    def spec(self, tag: str) -> None:
        if self.spec_swept:
            raise StudyConfigError("Two sweeps both pick the model spec.")
        self.base, self.spec_swept = tag, True


def _values(sweep: Any) -> list[tuple[Any, Any]]:
    if isinstance(sweep, SurrogateSweep):
        return [(sweep, tag) for tag in sweep.surrogates]
    if isinstance(sweep, VariableSweep | ConfigurationSweep):
        return [(sweep, value) for value in sweep.values]
    if isinstance(sweep, CasesSweep):
        return [(sweep, case) for case in sweep.cases]
    assert isinstance(sweep, SpecListSweep)
    return [(sweep, tag) for tag in sweep.specs]


def _field(sweep: Any) -> str:
    if sweep.name is not None:
        return sweep.name
    if isinstance(sweep, SurrogateSweep):
        return "surrogate"
    if isinstance(sweep, VariableSweep | ConfigurationSweep):
        return sweep.path
    if isinstance(sweep, CasesSweep):
        return "case"
    return "spec"


def _render(cfg: StudyConfig, base: str, index: int, labels: dict[str, Any]) -> str:
    fields = {"base": base, "index": index, **labels}
    if cfg.output_name is None:
        parts = [base] + [_text(v) for v in labels.values() if v != base]
        tag = _UNSAFE.sub("-", "_".join(parts))
    else:

        def value(match: re.Match) -> str:
            name, _, spec = match.group(1).partition(":")
            if name not in fields:
                raise StudyConfigError(
                    f"output_name {cfg.output_name!r} names {{{name}}}; the "
                    f"fields are {sorted(fields)}."
                )
            return _text(fields[name], spec)

        tag = _FIELD.sub(value, cfg.output_name)
    try:
        return check_tag(tag)
    except SpecError as exc:
        raise StudyConfigError(f"output_name renders an invalid tag: {exc}") from exc


def _text(value: Any, spec: str = "") -> str:
    """A value as tag text: exact (``str``) unless ``spec`` formats it.

    ``"e+"`` becomes ``"e"`` (``1e+20`` -> ``1e20``): ``+`` can't be in a tag.
    """
    if isinstance(value, dict) and set(value) == {"value", "units"}:
        return _text(value["value"], spec)
    try:
        text = format(value, spec) if spec else str(value)
    except (TypeError, ValueError) as exc:
        raise StudyConfigError(
            f"output_name can't format {value!r} as {{:{spec}}}: {exc}"
        ) from exc
    return text.replace("e+", "e")
