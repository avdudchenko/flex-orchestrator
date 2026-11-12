"""polars and pandas tables in and out."""

from datetime import UTC, datetime, timedelta, timezone

import pandas as pd
import polars as pl
import pytest

from flexorchestrator.errors import StorageError
from flexorchestrator.frames import frame_kind, from_columns, to_columns

pytestmark = pytest.mark.unit

T0 = datetime(2025, 1, 1)
STAMPS = [T0 + timedelta(minutes=15 * i) for i in range(3)]
UTC_STAMPS = [t.replace(tzinfo=UTC) for t in STAMPS]


def test_frame_kind():
    assert frame_kind(pl.DataFrame({"a": [1]})) == "polars"
    assert frame_kind(pd.DataFrame({"a": [1]})) == "pandas"
    with pytest.raises(TypeError, match="polars or pandas"):
        frame_kind({"a": [1]})


def test_pandas_index_is_time():
    df = pd.DataFrame(
        {
            "flow": [1.0, float("nan"), 3],
            "level": pd.array([1, None, 2], dtype="Int64"),
        },
        index=pd.DatetimeIndex(STAMPS),
    )
    timestamps, columns = to_columns(df)
    assert timestamps == UTC_STAMPS
    assert columns == {"flow": [1.0, None, 3.0], "level": [1.0, None, 2.0]}


def test_pandas_time_column_and_timezones():
    plus2 = timezone(timedelta(hours=2))
    df = pd.DataFrame({"t": [t.replace(tzinfo=plus2) for t in STAMPS], "x": [1, 2, 3]})
    timestamps, columns = to_columns(df, time_column="t")
    assert timestamps == [t - timedelta(hours=2) for t in UTC_STAMPS]
    assert columns == {"x": [1.0, 2.0, 3.0]}


def test_polars_finds_its_one_datetime_column():
    df = pl.DataFrame({"x": [1.0, None, 3.0], "when": STAMPS})
    assert to_columns(df) == (UTC_STAMPS, {"x": [1.0, None, 3.0]})
    assert to_columns(df, time_column="when")[0] == UTC_STAMPS


@pytest.mark.parametrize(
    ("df", "time_column", "match"),
    [
        (pd.DataFrame({"x": [1]}), None, "DatetimeIndex"),
        (pl.DataFrame({"x": [1]}), None, "found 0 datetime columns"),
        (pl.DataFrame({"a": STAMPS, "b": STAMPS}), None, "found 2"),
        (pl.DataFrame({"a": STAMPS, "x": [1, 2, 3]}), "x", "isn't a datetime"),
        (pl.DataFrame({"a": STAMPS}), "nope", "no column 'nope'"),
        (pd.DataFrame({"t": [1, 2, 3], "x": [1.0, 2.0, 3.0]}), "t", "isn't a datetime"),
        (pd.DataFrame({"x": ["a", "b", "c"]}, index=STAMPS), None, "isn't numeric"),
        (pd.DataFrame({"x": [True, False, True]}, index=STAMPS), None, "isn't numeric"),
        (pd.DataFrame({"x": [1.0, float("inf"), 2.0]}, index=STAMPS), None, "infinite"),
        (pd.DataFrame({"x": [1, 2]}, index=[T0, T0]), None, "repeated"),
        (pd.DataFrame({"x": [object(), 1, 2]}, index=STAMPS), None, "isn't numeric"),
    ],
)
def test_bad_tables(df, time_column, match):
    with pytest.raises(StorageError, match=match):
        to_columns(df, time_column)


def test_from_columns_polars():
    df = from_columns(UTC_STAMPS, {"x": [1.0, None, 3.0]}, "polars")
    assert df.columns == ["time", "x"]
    assert df.schema["time"] == pl.Datetime("us", "UTC")
    assert df["x"].to_list() == [1.0, None, 3.0]


def test_from_columns_pandas():
    df = from_columns(UTC_STAMPS, {"x": [1.0, None, 3.0]}, "pandas")
    assert df.index.name == "time" and str(df.index.tz) == "UTC"
    assert list(df.index) == [pd.Timestamp(t) for t in UTC_STAMPS]
    assert df["x"].isna().tolist() == [False, True, False]
    assert from_columns([], {"x": []}, "pandas").empty


def test_from_columns_rejects_other_kinds():
    with pytest.raises(ValueError, match="frame must be"):
        from_columns([], {}, "arrow")


def test_round_trip_through_columns():
    df = pl.DataFrame({"time": UTC_STAMPS, "x": [1.0, 2.0, 3.0]})
    assert from_columns(*to_columns(df), "polars").equals(df)


def test_a_pandas_time_column_of_strings_is_still_parsed():
    df = pd.DataFrame({"t": ["2025-07-08 00:00", "2025-07-08 01:00"], "x": [1, 2]})
    assert len(to_columns(df, time_column="t")[0]) == 2
