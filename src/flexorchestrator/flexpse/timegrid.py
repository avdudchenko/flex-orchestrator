"""The flex-pse ``time_grid`` hook: a spec's or a built model's time points."""

import datetime
from collections.abc import Mapping
from typing import Any

import pandas as pd
import pyomo.environ as pyo
from flexops.core.build import parse_quantity
from pyomo.environ import units as pyunits

from flexorchestrator.errors import SpecError


def time_grid(spec_or_model: Any) -> pd.DatetimeIndex | None:
    """Return the time points of a flex-pse spec's ``time`` element, or a model's.

    For a spec, the points ``flexops`` builds the model's time block on: from
    ``start_date`` (inclusive) to ``end_date`` (exclusive) every ``time_step``,
    in naive local time. For a built model, its time block's.

    Args:
        spec_or_model: A ``FlowsheetSpec`` as JSON, or a built model. Not
            modified.

    Returns:
        The points; ``None`` if there's no time axis.

    Raises:
        SpecError: If there are two time axes, or the dates or step don't make
            a whole number of steps.

    Example:
        >>> time_grid(spec)[:2]  # doctest: +SKIP
        DatetimeIndex(['2025-07-08 00:00:00', '2025-07-08 01:00:00'], ...)
    """
    if not isinstance(spec_or_model, Mapping):
        return _of_model(spec_or_model)
    spec = spec_or_model
    found = [e for e in spec.get("elements", []) if e.get("kind") == "time"]
    if not found:
        return None
    if len(found) > 1:
        raise SpecError("The spec has more than one time element.")
    [element] = found
    try:
        start = datetime.datetime.fromisoformat(element["start_date"])
        end = datetime.datetime.fromisoformat(element["end_date"])
        seconds = pyo.value(
            pyunits.convert(parse_quantity(element["time_step"]), pyunits.s)
        )
    except Exception as exc:
        raise SpecError(f"Can't read the spec's time element: {exc}") from exc
    if start.tzinfo is not None or end.tzinfo is not None:
        raise SpecError("The time element's dates must be naive local time.")
    step = pd.Timedelta(seconds=seconds)
    n, remainder = divmod(pd.Timestamp(end) - pd.Timestamp(start), step)
    if remainder != pd.Timedelta(0) or n <= 0:
        raise SpecError(
            f"The time element's horizon {start} -> {end} isn't a whole number of "
            f"{element['time_step']} steps."
        )
    return pd.date_range(start, periods=n, freq=step)


def _of_model(model: Any) -> pd.DatetimeIndex | None:
    blocks = [
        b
        for b in model.component_data_objects(pyo.Block, descend_into=True)
        if isinstance(getattr(type(b), "datetime_index", None), property)
    ]
    if not blocks:
        return None
    if len(blocks) > 1:
        raise SpecError(
            f"The model has {len(blocks)} time blocks; apply model inputs to one "
            "with a single time axis."
        )
    return pd.DatetimeIndex(blocks[0].datetime_index)
