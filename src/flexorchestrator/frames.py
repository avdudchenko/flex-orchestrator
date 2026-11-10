"""polars and pandas tables in and out.

The orchestrator works on plain columns: a list of UTC timestamps and one list
of values per column. This module turns a DataFrame of either kind into that,
and back.
"""

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

import pandas as pd
import polars as pl

from flexorchestrator.backend.base import align, to_utc
from flexorchestrator.errors import StorageError

FrameKind = Literal["polars", "pandas"]
FRAME_KINDS = ("polars", "pandas")
TIME = "time"
"""str: The name of the time column (polars) or index (pandas) on the way out."""


def frame_kind(df: Any) -> FrameKind:
    """Return ``"polars"`` or ``"pandas"`` for a DataFrame.

    Raises:
        TypeError: If ``df`` is neither.
    """
    if isinstance(df, pl.DataFrame):
        return "polars"
    if isinstance(df, pd.DataFrame):
        return "pandas"
    raise TypeError(f"Expected a polars or pandas DataFrame, not {type(df).__name__}.")


def check_frame(frame: Any) -> FrameKind:
    """Return ``frame`` if it names a frame kind.

    Raises:
        ValueError: If ``frame`` isn't ``"polars"`` or ``"pandas"``.
    """
    if frame not in FRAME_KINDS:
        raise ValueError(f"frame must be one of {FRAME_KINDS}, not {frame!r}.")
    return frame


def to_columns(
    df: Any, time_column: str | None = None
) -> tuple[list[datetime], dict[str, list[float | None]]]:
    """Split a DataFrame into UTC timestamps and numeric columns.

    Args:
        df: A polars or pandas DataFrame.
        time_column: The column holding the datetimes. For pandas the default is
            the index; for polars it is the only datetime column.

    Returns:
        ``(timestamps, {column: values})``, with NaN as ``None``.

    Raises:
        StorageError: If there is no datetime time column, the timestamps
            repeat, or a column isn't numeric.
    """
    if frame_kind(df) == "pandas":
        times, columns = _pandas_columns(df, time_column)
    else:
        times, columns = _polars_columns(df, time_column)
    timestamps = [to_utc(when) for when in times]
    if len(set(timestamps)) != len(timestamps):
        raise StorageError("The time column has repeated timestamps.")
    return timestamps, {
        name: [_number(name, value) for value in values]
        for name, values in columns.items()
    }


def from_columns(
    timestamps: Sequence[datetime],
    columns: Mapping[str, Sequence[float | None]],
    frame: FrameKind,
) -> Any:
    """Build a DataFrame from UTC timestamps and columns.

    polars gets a ``time`` column of UTC datetimes first; pandas gets a UTC
    ``DatetimeIndex`` named ``time``. Missing values are null (polars) or NaN
    (pandas).

    Args:
        timestamps: The UTC timestamps, one per row.
        columns: ``{column: values}``, a value per timestamp (``None`` if
            missing).
        frame: ``"polars"`` or ``"pandas"``.

    Raises:
        ValueError: If ``frame`` isn't ``"polars"`` or ``"pandas"``.
    """
    if check_frame(frame) == "polars":
        data = {TIME: pl.Series(TIME, list(timestamps), dtype=pl.Datetime("us", "UTC"))}
        data.update(
            {
                name: pl.Series(name, list(v), dtype=pl.Float64)
                for name, v in columns.items()
            }
        )
        return pl.DataFrame(data)
    index = pd.DatetimeIndex(list(timestamps), name=TIME)
    if index.tz is None:
        index = index.tz_localize("UTC")
    return pd.DataFrame(
        {name: pd.array(list(v), dtype="float64") for name, v in columns.items()},
        index=index,
    )


def _pandas_columns(df: Any, time_column: str | None) -> tuple[list, dict]:
    if time_column is None:
        if not isinstance(df.index, pd.DatetimeIndex):
            raise StorageError(
                "A pandas DataFrame needs a DatetimeIndex, or time_column= naming "
                "its datetime column."
            )
        times, values = df.index, df
    else:
        _need(time_column, df.columns)
        if pd.api.types.is_numeric_dtype(df[time_column]):
            # pandas would read numbers as nanoseconds since 1970.
            raise StorageError(f"Time column {time_column!r} isn't a datetime column.")
        times = pd.DatetimeIndex(df[time_column])
        values = df.drop(columns=[time_column])
    return list(times), {str(name): values[name].tolist() for name in values.columns}


def _polars_columns(df: Any, time_column: str | None) -> tuple[list, dict]:
    if time_column is None:
        found = [n for n, t in df.schema.items() if isinstance(t, pl.Datetime)]
        if len(found) != 1:
            raise StorageError(
                f"A polars DataFrame needs time_column= naming its datetime column "
                f"(found {len(found)} datetime columns)."
            )
        time_column = found[0]
    _need(time_column, df.columns)
    if not isinstance(df.schema[time_column], pl.Datetime):
        raise StorageError(f"Time column {time_column!r} isn't a datetime column.")
    return df[time_column].to_list(), {
        name: df[name].to_list() for name in df.columns if name != time_column
    }


def _need(name: str, columns: Any) -> None:
    if name not in list(columns):
        raise StorageError(f"There is no column {name!r}.")


def _number(column: str, value: Any) -> float | None:
    # pandas' nullable dtypes give pd.NA for a missing value.
    if value is None or type(value).__name__ == "NAType":
        return None
    if isinstance(value, bool | str):
        raise StorageError(f"Column {column!r} isn't numeric (it holds {value!r}).")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise StorageError(
            f"Column {column!r} isn't numeric (it holds {value!r})."
        ) from None
    if math.isnan(value):
        return None
    if math.isinf(value):
        raise StorageError(f"Column {column!r} holds an infinite value.")
    return value


def model_frame(data: Any, frame: FrameKind) -> Any:
    """Return a :class:`~flexorchestrator.export.ModelData`'s timeseries as one table.

    Every timeseries key is a column, aligned on the union of their timestamps
    (a subset of time such as ``startup`` is missing at the points it doesn't
    cover). Scalars aren't included.

    Args:
        data: The values, with datetime timestamps.
        frame: ``"polars"`` or ``"pandas"``.
    """
    by_key = {
        s.key: dict(zip([to_utc(t) for t in s.timestamps], s.values, strict=True))
        for s in data.timeseries()
    }
    timestamps, columns = align(by_key, list(by_key))
    return from_columns(timestamps, columns, frame)
