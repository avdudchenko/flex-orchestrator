"""The ``flexorch-export/1`` file formats, as pydantic models.

Every file of a study export is one of these models as JSON, with
``"schema": "flexorch-export/1"``. Their JSON Schemas are published in
``docs/schemas/`` (:mod:`flexorchestrator.schemas`), so an export can be read
without the orchestrator.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flexorchestrator.assets import AssetRef

SCHEMA = "flexorch-export/1"
"""str: The schema version every export file carries."""


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["flexorch-export/1"] = Field(default=SCHEMA, alias="schema")

    def dump(self) -> dict[str, Any]:
        """Return the file's JSON, ``schema`` first."""
        return self.model_dump(mode="json", by_alias=True)


class StudyManifest(_File):
    """``study.json``: what the export holds, and how it was made.

    Attributes:
        study: The exported study.
        exported_at: When (UTC, ISO 8601).
        exported_by: Who (``FLEXORCH_USER`` or the login name).
        tenant: The tenant the study belongs to; ``None`` outside a tenant
            stack (Phase 1).
        versions: flex-orchestrator, flex-pse and Acquirium versions.
        counts: How many files each part has.
        options: The options the export was made with.
        files: Every other file of the export, relative to its folder.
    """

    study: str
    exported_at: str
    exported_by: str | None = None
    tenant: str | None = None
    versions: dict[str, str | None]
    counts: dict[str, int]
    options: dict[str, Any]
    files: list[str]


class SpecFile(_File):
    """``specs/models/<tag>.json`` or ``specs/surrogates/<tag>.json``.

    The spec's referenced files are in ``specs/`` under their stored names
    (``specs/assets/<sha256[:12]>-<name>``).

    Attributes:
        tag: The spec's tag.
        kind: ``"model_spec"`` or ``"surrogate_spec"``.
        spec_format: The spec's format.
        spec_hash: Its hash, files included.
        parent_tag: The spec it was derived from.
        created_at: When it was stored.
        created_by: Who stored it.
        run_id: Its run in ``<s>::specs``.
        versions: The versions that stored it.
        metadata: The run's metadata (``fit_run_id`` for a fitted surrogate).
        assets: ``{JSON pointer: AssetRef}`` for each referenced file.
        spec: The spec.
    """

    tag: str
    kind: Literal["model_spec", "surrogate_spec"]
    spec_format: str
    spec_hash: str
    parent_tag: str | None = None
    created_at: str | None = None
    created_by: str | None = None
    run_id: str
    versions: dict[str, str | None] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    assets: dict[str, AssetRef] = Field(default_factory=dict)
    spec: dict[str, Any]


class DatasetColumn(BaseModel):
    """One column of a dataset file.

    Attributes:
        unit: The unit as the user wrote it.
        qudt_unit: Its QUDT key, if one was found.
        values: One per ``time_index`` entry; ``null`` where missing.
    """

    model_config = ConfigDict(extra="forbid")

    unit: str | None = None
    qudt_unit: str | None = None
    values: list[float | None]


class DatasetFile(_File):
    """``data/<name>.json``: one dataset.

    Attributes:
        name: The dataset's name.
        run_id: Its run in ``<s>::data``.
        created_at: When it was stored.
        created_by: Who stored it.
        time_index: UTC timestamps, ISO 8601.
        columns: ``{column: DatasetColumn}``, in order.
        metadata: The run's metadata.
        manifest: The dataset's stored manifest.
    """

    name: str
    run_id: str
    created_at: str | None = None
    created_by: str | None = None
    time_index: list[str]
    columns: dict[str, DatasetColumn]
    metadata: dict[str, Any] = Field(default_factory=dict)
    manifest: dict[str, Any] = Field(default_factory=dict)


class TimeIndex(BaseModel):
    """A result's timestamps, stored once.

    Attributes:
        timestamps: UTC, ISO 8601; every timeseries lines up with them.
    """

    model_config = ConfigDict(extra="forbid")

    timestamps: list[str]


class ResultSeries(BaseModel):
    """One timeseries of a result file.

    Attributes:
        unit: The QUDT key.
        pyomo_unit: The Pyomo unit.
        unit_verified: Whether the QUDT key is confirmed.
        component: The Pyomo component.
        index: Its custom index (``[]`` for none), JSON values of their types.
        values: One per ``time_index`` timestamp; ``null`` where missing.
        rows: For a series covering only part of time (``startup``), the
            positions in ``time_index`` it covers; all of them if omitted.
    """

    model_config = ConfigDict(extra="forbid")

    unit: str | None = None
    pyomo_unit: str | None = None
    unit_verified: bool = False
    component: str
    index: list[Any] = Field(default_factory=list)
    values: list[float | None]
    rows: list[int] | None = None


class ResultScalar(BaseModel):
    """One scalar of a result file (see :class:`ResultSeries`)."""

    model_config = ConfigDict(extra="forbid")

    unit: str | None = None
    pyomo_unit: str | None = None
    unit_verified: bool = False
    component: str
    index: list[Any] = Field(default_factory=list)
    value: float | None


class ResultFile(_File):
    """``results/<tag>.json``: one stored result.

    Attributes:
        tag: The result's tag.
        run_id: Its run in ``<s>::runs``.
        status: ``"succeeded"`` (only stored results are exported).
        started_at: When its run started.
        model_spec_ref: ``{tag, spec_hash}`` of the spec it was built from.
        overrides: The build overrides.
        model_inputs: Where the model inputs among ``overrides`` came from.
        hooks: The hooks that built and solved it.
        solve_outcome: How the solve ended.
        metadata: The run's metadata.
        streams: The keys also stored as native variables.
        keys: Every key, in the order the model was walked.
        time_index: The timestamps.
        timeseries: ``{key: ResultSeries}``.
        scalars: ``{key: ResultScalar}``.
        stale: Keys with missing values.
    """

    tag: str
    run_id: str
    status: Literal["succeeded"] = "succeeded"
    started_at: str | None = None
    model_spec_ref: dict[str, str] | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    model_inputs: list[dict[str, Any]] = Field(default_factory=list)
    hooks: dict[str, str] = Field(default_factory=dict)
    solve_outcome: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    streams: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    time_index: TimeIndex
    timeseries: dict[str, ResultSeries]
    scalars: dict[str, ResultScalar]
    stale: list[str] = Field(default_factory=list)


class FitSeries(BaseModel):
    """A fit's ``actual`` / ``fitted`` / ``residual`` series."""

    model_config = ConfigDict(extra="forbid")

    time_index: list[str]
    actual: list[float | None]
    fitted: list[float | None]
    residual: list[float | None]


class FitFile(_File):
    """``fits/<tag>.json``: one fit run.

    Attributes:
        tag: The fit's tag (its surrogate spec's).
        run_id: Its run in ``<s>::fits``.
        started_at: When it ran.
        metadata: The run's metadata.
        fit_params: The fit as planned.
        training_data: What the data resolved to, and how it was prepared.
        fit_result: Coefficients, metrics and diagnostics.
        series: The fitted series, if stored.
    """

    tag: str
    run_id: str
    started_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    fit_params: dict[str, Any]
    training_data: dict[str, Any]
    fit_result: dict[str, Any]
    series: FitSeries | None = None


FILE_MODELS: dict[str, type[_File]] = {
    "study": StudyManifest,
    "spec": SpecFile,
    "dataset": DatasetFile,
    "result": ResultFile,
    "fit": FitFile,
}
"""dict: Schema file name stem -> model, for :func:`json_schemas`."""


def json_schemas() -> dict[str, dict[str, Any]]:
    """Return ``{name: JSON Schema}`` for every export file model."""
    schemas = {}
    for name, model in FILE_MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$id"] = f"https://flex-orchestrator/schemas/{SCHEMA}/{name}.json"
        schemas[name] = schema
    return schemas
