"""``StudyConfig``: a whole study as data (PLAN.md §4, M4).

A config names a base model spec, optional ``fits`` (run first, so sweeps can
use the surrogates they store), ``sweeps`` (combined as a product or zipped),
and how each task's result is named, exported and stored. A sweep varies one
thing:

- ``surrogate``: the surrogate a unit uses;
- ``variable``: a model variable, fixed to each value;
- ``configuration_option``: an option the model is built with, such as a
  unit's construction option or a price;
- ``spec_list``: the model spec itself;
- ``cases``: named, hand-picked combinations of the above.

For example::

    base_model_spec: base
    fits:
      - tag: "ro_{name}"
        X: {dataset: ro_operation, columns: [permeate]}
        y: {dataset: ro_operation, columns: [power_electrical]}
        variants:
          - {name: average, regressor: constant_intensity}
          - {name: linear, regressor: multilinear}
    sweeps:
      - type: surrogate
        unit: plant.ro
        surrogates: [ro_average, ro_linear]
    export: {include: ["plant.ro.*", "costing.aggregate_operating_cost"]}
    output_name: "{surrogate}"

Hooks are import strings or registered names, so a config is plain data that
a Ray worker or the HTTP API can carry. Its JSON Schema is
``StudyConfig.model_json_schema()`` (published in ``docs/schemas/``).
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from flex_orchestrator.errors import StudyConfigError
from flex_orchestrator.fitting.models import FitParams


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SurrogateSweep(_Strict):
    """Each task uses one of ``surrogates`` for ``unit``.

    Attributes:
        name: The field ``output_name`` renders the surrogate's tag as;
            ``surrogate`` by default.
        unit: The unit's path, e.g. ``"plant.ro"``.
        relation: The relation the surrogate replaces; the hook's default if
            omitted.
        surrogates: Surrogate spec tags, stored or fitted by ``fits``.
    """

    type: Literal["surrogate"]
    name: str | None = None
    unit: str
    relation: str | None = None
    surrogates: list[str] = Field(min_length=1)


class VariableSweep(_Strict):
    """Each task fixes the model variable at ``path`` to one of ``values``.

    The values are passed to the build hook as overrides; for flex-pse each
    becomes a ``fix`` element. Use it for variables (``"plant.ro.recovery"``);
    an option the unit is built with is a :class:`ConfigurationSweep`.

    Attributes:
        name: The field ``output_name`` renders the value as; ``path`` by
            default.
        path: The variable's path from the model root, e.g.
            ``"plant.ro.recovery"`` or ``"plant.tank.level[*]"``.
        values: The values: numbers in the variable's units,
            ``{"value", "units"}``, or one number per time point.
    """

    type: Literal["variable"]
    name: str | None = None
    path: str
    values: list[Any] = Field(min_length=1)


class ConfigurationSweep(_Strict):
    """Each task sets the configuration option at ``path`` to one of ``values``.

    The option is set in a copy of the base spec (the ``configure`` hook), which
    is stored under the task's tag with the base as parent. For flex-pse,
    ``path`` is any element's name followed by the option: a unit's
    construction option (``"plant.feed.max_withdrawal"``) or another
    element's field (``"costing.energy_prices.electrical"``). A dict-valued
    option is set whole by a dict value, or one key at a time by a longer
    path (``"plant.feed.max_withdrawal.value"``).

    Attributes:
        name: The field ``output_name`` renders the value as; ``path`` by
            default.
        path: The option's path.
        values: The values; any JSON value, dicts included.
    """

    type: Literal["configuration_option"]
    name: str | None = None
    path: str
    values: list[Any] = Field(min_length=1)


class SpecListSweep(_Strict):
    """Each task builds from one of ``specs`` instead of ``base_model_spec``.

    Attributes:
        name: The field ``output_name`` renders the tag as; ``spec`` by
            default.
        specs: Model spec tags.
    """

    type: Literal["spec_list"]
    name: str | None = None
    specs: list[str] = Field(min_length=1)


class Case(_Strict):
    """One hand-picked combination in a :class:`CasesSweep`.

    Attributes:
        name: The case's name, which ``output_name`` renders as ``{case}``.
        spec: A model spec to build from instead of ``base_model_spec``.
        surrogates: ``{unit: surrogate tag}``.
        configuration_options: ``{option path: value}``, as a
            :class:`ConfigurationSweep` sets them.
        variables: ``{variable path: value}``, as a :class:`VariableSweep`
            fixes them.
    """

    name: str
    spec: str | None = None
    surrogates: dict[str, str] = Field(default_factory=dict)
    configuration_options: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)


class CasesSweep(_Strict):
    """Each task runs one of ``cases``: chosen combinations, not every one.

    A cases sweep is one sweep like any other, so it can be combined with the
    rest (a ``product`` with a price sweep runs every case at every price).

    Attributes:
        name: The field ``output_name`` renders the case's name as; ``case``
            by default.
        cases: The cases; their names must differ.
    """

    type: Literal["cases"]
    name: str | None = None
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_names(self) -> "CasesSweep":
        names = [case.name for case in self.cases]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"case names must differ; repeated: {repeated}")
        return self


Sweep = Annotated[
    SurrogateSweep | VariableSweep | ConfigurationSweep | SpecListSweep | CasesSweep,
    Field(discriminator="type"),
]


class HooksConfig(_Strict):
    """Hooks for the study's tasks: import strings or registered names (D1).

    Unset slots use the orchestrator's hooks; hooks passed to
    ``execute_study`` win over these.
    """

    build: str | None = None
    solve: str | None = None
    apply_surrogate: str | None = None
    swap_surrogate: str | None = None
    time_sets: str | None = None
    configure: str | None = None


class ExportConfig(_Strict):
    """What each task's result stores (D3); nothing set means the default selection.

    Attributes:
        keys: Exact paths to export.
        include: Glob patterns to export.
        exclude: Glob patterns to leave out.
        streams: Values to also store natively.
        strict_units: Raise on a unit with no QUDT key.
    """

    keys: list[str] | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    streams: list[str] | None = None
    strict_units: bool = False


class Execution(_Strict):
    """How tasks run.

    Attributes:
        max_workers: Tasks run at once; more than 1 needs Ray (M5).
        task_timeout_s: A task's time limit. Enforced by Ray (M5); in-process
            it is recorded but not enforced.
        retries: How many times a task whose build or solve raised is tried
            again.
        on_error: Go on with the other tasks after one fails (``continue``) or
            stop (``stop``).
    """

    max_workers: int = Field(default=1, ge=1)
    task_timeout_s: float | None = Field(default=None, gt=0)
    retries: int = Field(default=0, ge=0)
    on_error: Literal["continue", "stop"] = "continue"


class StudyConfig(_Strict):
    """A study as data; see :mod:`flex_orchestrator.study.config`.

    Attributes:
        study: The study to run in, created if needed; the selected one if
            omitted.
        base_model_spec: The model spec every task builds from, unless a
            ``spec_list`` sweep names others.
        fits: ``fit_surrogate`` parameters, run before the sweeps.
        hooks: See :class:`HooksConfig`.
        sweeps: See :class:`SurrogateSweep`, :class:`VariableSweep`,
            :class:`ConfigurationSweep`, :class:`SpecListSweep` and
            :class:`CasesSweep`.
        combine: ``product`` (every combination) or ``zip`` (the sweeps in
            step; they must be the same length).
        solve_options: Passed to the solve hook.
        export: See :class:`ExportConfig`.
        output_name: Each task's tag, e.g.
            ``"ro_{surrogate}_{plant.ro.recovery}"``: fields are the sweeps'
            names, plus ``base`` and ``index``. By default the base tag and
            each sweep's value, joined by ``_``. A value is written exactly
            (``str``; ``0.1 + 0.2`` gives ``0.30000000000000004``) unless the
            field has a format spec, as in Python: ``{recovery:.3g}``,
            ``{price:.2f}``. The tag is only a name: the values themselves are
            stored in full with the result. Values that format alike make
            tags that repeat, which is refused before anything runs.
        on_existing: When a task's tag is stored: ``error`` (checked before
            anything runs), ``skip`` it (resume), or ``replace`` it.
        execution: See :class:`Execution`.
    """

    study: str | None = None
    base_model_spec: str | None = None
    fits: list[FitParams] = Field(default_factory=list)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    sweeps: list[Sweep] = Field(default_factory=list)
    combine: Literal["product", "zip"] = "product"
    solve_options: dict[str, Any] = Field(default_factory=dict)
    export: ExportConfig = Field(default_factory=ExportConfig)
    output_name: str | None = None
    on_existing: Literal["error", "skip", "replace"] = "error"
    execution: Execution = Field(default_factory=Execution)


def load_config(config: Any) -> StudyConfig:
    """Return ``config`` as a :class:`StudyConfig`.

    Args:
        config: A ``StudyConfig``, a mapping, or the path of a YAML (``.yaml``,
            ``.yml``) or JSON file.

    Raises:
        StudyConfigError: If the file can't be read or the config doesn't
            validate; the message lists each problem.
    """
    if isinstance(config, StudyConfig):
        return config
    if isinstance(config, str | os.PathLike):
        config = _read(Path(config))
    if not isinstance(config, Mapping):
        raise StudyConfigError(
            f"A study config is a StudyConfig, a mapping or a file path, not "
            f"{type(config).__name__}."
        )
    try:
        return StudyConfig.model_validate(dict(config))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(config)'}: {e['msg']}"
            for e in exc.errors()
        )
        raise StudyConfigError(f"Invalid study config: {problems}.") from exc


def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StudyConfigError(f"Can't read study config {path}: {exc}") from exc
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            return yaml.safe_load(text)
        return json.loads(text)
    except Exception as exc:
        raise StudyConfigError(f"Can't parse study config {path}: {exc}") from exc
