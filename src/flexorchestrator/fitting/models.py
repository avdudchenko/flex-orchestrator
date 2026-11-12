"""The parameters of ``fit_surrogate``, as JSON-friendly pydantic models.

The same parameters arrive from Python, the CLI, a study config's ``fits:``
section and a host app's HTTP API, so every field is plain JSON.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flexorchestrator.errors import FitError

SOURCES = ("dataset", "result", "ref_uris", "streams", "resolve", "sparql")
"""tuple[str, ...]: The keys that pick a selector's source; each has exactly one."""


class _Strict(BaseModel):
    """A config model that rejects unknown fields (a typo is an error)."""

    model_config = ConfigDict(extra="forbid")


class Window(_Strict):
    """A time window; either end may be open.

    Attributes:
        start: Rows at or after this time (naive means UTC).
        end: Rows at or before this time.
    """

    start: datetime | None = None
    end: datetime | None = None


class StreamName(_Strict):
    """A general-store stream by the names its driver registered it with.

    Attributes:
        source_id: The datasource, e.g. ``"historian"``.
        ref_name: The stream's name in that source, e.g. ``"FT_101"``.
    """

    source_id: str
    ref_name: str


class Selector(_Strict):
    """Where one or more columns of fit data come from.

    Exactly one source key is set:

    - ``dataset`` (with optional ``study`` and ``columns``): a dataset of this
      study, or of another study in the same store;
    - ``result`` (with ``keys``): exported values of a stored result;
    - ``ref_uris``: streams of the general store, by reference URI;
    - ``streams``: streams of the general store, by ``source_id`` and
      ``ref_name``;
    - ``resolve``: the one general-store stream whose label matches this text;
    - ``sparql``: a read-only SPARQL ``SELECT`` whose first column is stream
      references.

    Attributes:
        start: Only rows at or after this time.
        end: Only rows at or before this time.
        units: Units to convert to: one for every column, or ``{column: unit}``
            (after ``rename``). A Pyomo unit (``"m^3/hr"``) or a QUDT key
            (``"M3-PER-HR"``).
        rename: ``{column: new name}``, or ``{"*": name}`` for a selector with
            one column. The surrogate's variable names are the column names,
            so they must be the unit's variable names.
    """

    dataset: str | None = None
    study: str | None = None
    columns: list[str] | None = None
    result: str | None = None
    keys: list[str] | None = None
    ref_uris: list[str] | None = None
    streams: list[StreamName] | None = None
    resolve: str | None = None
    sparql: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    units: str | dict[str, str] | None = None
    rename: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_source(self) -> "Selector":
        given = [key for key in SOURCES if getattr(self, key) is not None]
        if len(given) != 1:
            raise ValueError(
                f"a selector needs exactly one of {list(SOURCES)}, got "
                f"{given or 'none'}"
            )
        if self.study is not None and self.dataset is None:
            raise ValueError("'study' only goes with 'dataset'")
        if self.columns is not None and self.dataset is None:
            raise ValueError("'columns' only goes with 'dataset'")
        if (self.keys is None) != (self.result is None):
            raise ValueError("'result' needs 'keys', and 'keys' only goes with it")
        return self

    @property
    def source(self) -> str:
        """Which source key is set."""
        return next(key for key in SOURCES if getattr(self, key) is not None)


Selectors = Selector | list[Selector]


class Resample(_Strict):
    """Resample to a regular index.

    Attributes:
        freq: A pandas offset alias, e.g. ``"1h"``.
        agg: How values in one bin combine.
    """

    freq: str
    agg: Literal["mean", "sum", "min", "max", "median", "first", "last"] = "mean"


class Prep(_Strict):
    """How the selected data is prepared before fitting.

    Attributes:
        align: ``inner`` keeps the timestamps every column has; ``outer`` keeps
            any column's, leaving gaps.
        resample: Resample to a regular index first.
        gaps: What to do with missing values: ``error`` (the default),
            ``drop`` the rows, ``interpolate`` (in time, up to ``limit``
            missing values in a row), or keep the ``longest_segment`` with
            none.
        limit: The longest run of missing values ``interpolate`` fills.
        max_rows: Refuse more rows than this (a guard against running out of
            memory).
    """

    align: Literal["inner", "outer"] = "inner"
    resample: Resample | None = None
    gaps: Literal["error", "drop", "interpolate", "longest_segment"] = "error"
    limit: int | None = Field(default=None, ge=1)
    max_rows: int = Field(default=1_000_000, ge=1)


class Validation(_Strict):
    """Hold out the end of the data to check the fit on.

    Attributes:
        holdout: How much to hold out from the end, as a pandas offset (``"7D"``);
            the fit uses the rows before it.
    """

    holdout: str


class Variant(_Strict):
    """One fit of a :class:`FitParams` with ``variants``; set fields override the base.

    Attributes:
        name: Rendered into the tag template as ``{name}``.

    The other fields are those of :class:`FitParams`; ``None`` keeps the base's.
    """

    name: str
    regressor: str | None = None
    params: dict[str, Any] | None = None
    spec_options: dict[str, Any] | None = None
    X: Selectors | None = None
    y: Selectors | None = None
    window: Window | None = None
    prep: Prep | None = None
    validation: Validation | None = None


class FitParams(_Strict):
    """The parameters of ``fit_surrogate``.

    Attributes:
        tag: The surrogate spec's tag; a template with ``{name}`` when there
            are variants (``"biogas_{name}"``).
        regressor: A flex-pse ``SurrogateType`` (``"multilinear"``,
            ``"constant_intensity"``) or an import string of a class that
            follows flex-pse's ``Regressor`` protocol.
        params: The regressor's constructor arguments.
        spec_options: Passed to its ``to_surrogate_spec``.
        X: The inputs: one selector or a list, merged column-wise.
        y: The output: one selector (or a list) giving one column.
        window: Only data in this window.
        prep: How the data is prepared (see :class:`Prep`).
        validation: Hold-out metrics (see :class:`Validation`).
        variants: Several fits in one call, each overriding these fields.
        on_existing: What to do when a tag is stored: ``error``, ``skip`` it,
            or ``replace`` it.
        store_series: Also store ``actual`` / ``fitted`` / ``residual``
            timeseries with the fit, for plotting.
        max_workers: Variants fitted at once; more than 1 fits them on Ray.
            In a study, the study's ``execution`` decides instead.
        ray_address: The Ray cluster for ``max_workers > 1``; local Ray if
            ``None``.
    """

    tag: str
    regressor: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    spec_options: dict[str, Any] = Field(default_factory=dict)
    X: Selectors
    y: Selectors
    window: Window | None = None
    prep: Prep = Field(default_factory=Prep)
    validation: Validation | None = None
    variants: list[Variant] | None = None
    on_existing: Literal["error", "skip", "replace"] = "error"
    store_series: bool = False
    max_workers: int = Field(default=1, ge=1)
    ray_address: str | None = None

    def expand(self) -> list["FitPlan"]:
        """Return one :class:`FitPlan` per variant (one if there are none).

        Raises:
            FitError: If a variant has no regressor, or two render the same tag.
        """
        variants = self.variants or [None]
        plans, tags = [], set()
        for variant in variants:
            fields = self.model_dump(
                exclude={"variants", "on_existing", "max_workers", "ray_address"}
            )
            name = None
            if variant is not None:
                name = variant.name
                overrides = variant.model_dump(exclude={"name"}, exclude_none=True)
                fields.update(overrides)
            tag = _render(self.tag, name)
            if tag in tags:
                raise FitError(
                    f"Two fits would be stored as {tag!r}; put '{{name}}' in the "
                    "tag when there are variants."
                )
            tags.add(tag)
            if fields.get("regressor") is None:
                raise FitError(
                    f"Fit {tag!r} has no regressor; set one for it or the base."
                )
            plans.append(FitPlan.model_validate({**fields, "tag": tag, "name": name}))
        return plans


def _render(template: str, name: str | None) -> str:
    if "{name}" not in template:
        return template
    if name is None:
        raise FitError(f"Tag {template!r} has '{{name}}' but there are no variants.")
    return template.replace("{name}", name)


class FitPlan(_Strict):
    """One fit, with its variant applied: what a fit run records as its parameters.

    Attributes:
        tag: The surrogate spec's tag, rendered.
        name: The variant's name, if it came from one.

    The other fields are those of :class:`FitParams`, with the variant's set.
    """

    tag: str
    name: str | None = None
    regressor: str
    params: dict[str, Any]
    spec_options: dict[str, Any]
    X: Selectors
    y: Selectors
    window: Window | None
    prep: Prep
    validation: Validation | None
    store_series: bool


class FitRecord(BaseModel):
    """What one fit stored.

    Attributes:
        tag: The surrogate spec's tag.
        status: ``"stored"``, or ``"skipped"`` (``on_existing: skip``).
        run_id: The fit run in ``<s>::fits``.
        spec_hash: The stored surrogate spec's hash.
        regressor: The regressor used.
        coefficients: The fitted coefficients.
        metrics: The fit's ``r2`` and ``rmse`` (and more, per regressor).
        holdout: Metrics on the held-out rows, if validated.
        n_samples: Rows the fit used.
        data_hash: sha256 of the prepared data.
    """

    tag: str
    status: Literal["stored", "skipped"]
    run_id: str | None = None
    spec_hash: str | None = None
    regressor: str | None = None
    coefficients: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    holdout: dict[str, float] | None = None
    n_samples: int | None = None
    data_hash: str | None = None


class FitSummary(BaseModel):
    """What ``fit_surrogate`` returns: one record per fit, in order.

    Attributes:
        fits: The records.
    """

    fits: list[FitRecord]

    @property
    def tags(self) -> list[str]:
        """The tags of the fits stored (not skipped)."""
        return [f.tag for f in self.fits if f.status == "stored"]

    def __getitem__(self, tag: str) -> FitRecord:
        """Return the record of ``tag``."""
        for record in self.fits:
            if record.tag == tag:
                return record
        raise KeyError(tag)
