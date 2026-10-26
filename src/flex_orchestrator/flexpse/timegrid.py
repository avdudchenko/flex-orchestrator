"""The flex-pse ``time_grid`` hook: a spec's time points, before it's built (D21)."""

import datetime

import pandas as pd
import pyomo.environ as pyo
from flexops.core.build import parse_quantity
from pyomo.environ import units as pyunits

from flex_orchestrator.errors import SpecError


def time_grid(spec: dict) -> pd.DatetimeIndex | None:
    """Return the time points of a flex-pse spec's ``time`` element.

    The same points ``flexops`` builds the model's time block on: from
    ``start_date`` (inclusive) to ``end_date`` (exclusive) every ``time_step``,
    in naive local time.

    Args:
        spec: A ``FlowsheetSpec`` as JSON. Not modified.

    Returns:
        The points, with ``freq`` set; ``None`` if the spec has no ``time``
        element.

    Raises:
        SpecError: If the spec has two ``time`` elements, or the dates or step
            don't make a whole number of steps.

    Example:
        >>> time_grid(spec)[:2]  # doctest: +SKIP
        DatetimeIndex(['2025-07-08 00:00:00', '2025-07-08 01:00:00'], ...)
    """
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
