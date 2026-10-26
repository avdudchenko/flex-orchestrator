"""``model_inputs`` entries: stored data a model variable is set from (PLAN.md D21).

An entry is a D19 fit selector (``dataset``, ``result``, ``ref_uris``,
``streams``, ``resolve`` or ``sparql``, with ``start``, ``end``, ``units`` and
``rename``) plus how its data is lined up with the model's time steps. After
``rename``, each column's name is the model path it sets.

Plain pydantic, so a study config carries it as data.
"""

import zoneinfo
from typing import Literal

import pandas as pd
from pydantic import Field, field_validator, model_validator

from flex_orchestrator.fitting.models import Selector, _Strict

Agg = Literal["mean", "sum", "min", "max", "median", "first", "last"]
Gaps = Literal["interpolate", "ffill", "zero", "error"]
Reduce = Literal["mean", "last", "first", "min", "max", "median"]


class InputResample(_Strict):
    """How samples become one value per time step.

    Attributes:
        agg: How the samples in one step combine.
    """

    agg: Agg = "mean"


class ModelInput(Selector):
    """One ``model_inputs`` entry: a selector, then its alignment.

    Attributes:
        timezone: The facility's time zone (an IANA name such as
            ``"America/Los_Angeles"``). The model runs on that site's wall
            clock, so stored UTC timestamps are converted to it before they're
            lined up with the model's (naive) time steps. ``UTC`` by default:
            data stored in UTC means a model in UTC.
        resample: How the samples in one time step combine; ``mean`` by
            default.
        gaps: A time step with no value is filled by ``interpolate`` (in
            time, the default), ``ffill`` (the last value carried forward) or
            ``zero``; ``error`` refuses any gap.
        limit: The longest run of missing steps ``interpolate`` or ``ffill``
            fills; a longer one fails.
        reduce: Set the variable to one number instead of a series: this
            statistic of the data over the model's horizon.
        offset: Read the data this far from the model's horizon, as a pandas
            offset (``"-365D"``: last year's data for this horizon).
    """

    timezone: str = "UTC"
    resample: InputResample | None = None
    gaps: Gaps = "interpolate"
    limit: int | None = Field(default=None, ge=1)
    reduce: Reduce | None = None
    offset: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("offset")
    @classmethod
    def _an_offset(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                pd.Timedelta(value)
            except ValueError as exc:
                raise ValueError(f"offset {value!r} isn't a duration") from exc
        return value

    @model_validator(mode="after")
    def _reduce_alone(self) -> "ModelInput":
        if self.reduce is not None:
            given = {"resample", "gaps", "limit"} & self.model_fields_set
            if given:
                raise ValueError(
                    f"reduce gives one number, so {sorted(given)} don't apply"
                )
        return self


class InputSet(_Strict):
    """One value of a ``model_inputs`` sweep: a labelled set of entries.

    Attributes:
        label: What ``output_name`` renders the set as.
        inputs: The entries a task with this value uses.
    """

    label: str
    inputs: list[ModelInput] = Field(min_length=1)
