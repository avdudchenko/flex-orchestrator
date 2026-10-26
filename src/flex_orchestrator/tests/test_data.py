"""The data API (PLAN.md D6) on the memory backend."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import polars as pl
import pytest

from flex_orchestrator.data import MANIFEST, qudt_unit
from flex_orchestrator.errors import (
    AmbiguousColumnError,
    StorageError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
)
from flex_orchestrator.export import UnitMapper
from flex_orchestrator.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

T0 = datetime(2025, 1, 1)
STAMPS = [T0 + timedelta(minutes=15 * i) for i in range(4)]
UTC_STAMPS = [t.replace(tzinfo=UTC) for t in STAMPS]


@pytest.fixture
def memory_orch(memory_backend):
    orch = Orchestrator(memory_backend)
    orch.create_study("example_a")
    return orch


def biogas():
    return pd.DataFrame(
        {"biogas_volume": [1.0, 2.0, None, 4.0], "flow_in_feed": [10.0, 11, 12, 13]},
        index=pd.DatetimeIndex(STAMPS),
    )


def test_store_and_read_back(memory_orch):
    stored = memory_orch.store_data(
        "biogas_data", biogas(), units={"biogas_volume": "m3/day"}
    )
    assert (stored.columns, stored.rows) == (["biogas_volume", "flow_in_feed"], 4)
    df = memory_orch.get_data("biogas_data")
    assert isinstance(df, pl.DataFrame)
    assert df.columns == ["time", "biogas_volume", "flow_in_feed"]
    assert df["time"].to_list() == UTC_STAMPS
    assert df["biogas_volume"].to_list() == [1.0, 2.0, None, 4.0]


def test_layout_in_the_data_ledger(memory_orch):
    memory_orch.store_data("biogas_data", biogas(), units={"biogas_volume": "m3/day"})
    ledger = "example_a::data"
    [run] = memory_orch.backend.runs(ledger)
    assert run.metadata["flexorch_kind"] == "data"
    assert run.metadata["flexorch_tag"] == "biogas_data"
    column = memory_orch.backend.variable(ledger, "biogas_data/biogas_volume")
    assert (column.role, column.kind) == ("input", "timeseries")
    assert column.metadata == {
        "dataset": "biogas_data",
        "column": "biogas_volume",
        "unit_text": "m3/day",
    }
    [manifest] = memory_orch.backend.records(ledger, run.run_id, labels=[MANIFEST])
    assert manifest.value["columns"] == ["biogas_volume", "flow_in_feed"]
    assert manifest.value["units"] == {"biogas_volume": "m3/day", "flow_in_feed": None}
    assert (manifest.value["rows"], manifest.value["start"]) == (
        4,
        "2025-01-01T00:00:00+00:00",
    )


def test_columns_window_and_pandas(memory_orch):
    memory_orch.store_data("biogas_data", biogas())
    df = memory_orch.get_data(
        "biogas_data",
        columns=["flow_in_feed"],
        start=STAMPS[1],
        end=STAMPS[2],
        frame="pandas",
    )
    assert list(df.columns) == ["flow_in_feed"]
    assert df["flow_in_feed"].tolist() == [11.0, 12.0]
    assert str(df.index.tz) == "UTC"


def test_orchestrator_default_frame(memory_backend):
    orch = Orchestrator(memory_backend, frame="pandas")
    orch.create_study("s")
    orch.store_data("d", biogas())
    assert isinstance(orch.get_data("d"), pd.DataFrame)
    with pytest.raises(ValueError, match="frame must be"):
        Orchestrator(memory_backend, frame="arrow")


def test_polars_input(memory_orch):
    df = pl.DataFrame({"when": STAMPS, "x": [1.0, 2.0, 3.0, 4.0]})
    memory_orch.store_data("p", df, units={"x": "kW"})
    assert memory_orch.get_data("p")["x"].to_list() == [1.0, 2.0, 3.0, 4.0]


def test_find_a_column_across_datasets(memory_orch):
    memory_orch.store_data("biogas_data", biogas())
    memory_orch.store_data(
        "weather", pd.DataFrame({"temp": [1.0, 2, 3, 4]}, index=STAMPS)
    )
    df = memory_orch.get_data(column="temp")
    assert df.columns == ["time", "temp"]
    with pytest.raises(TagNotFoundError, match="No dataset .* has a column 'nope'"):
        memory_orch.get_data(column="nope")


def test_an_ambiguous_column_names_its_datasets(memory_orch):
    memory_orch.store_data("a", biogas())
    memory_orch.store_data("b", biogas())
    with pytest.raises(AmbiguousColumnError, match=r"\['a', 'b'\]"):
        memory_orch.get_data(column="biogas_volume")
    # A missing value is no sample, so that row isn't in this column.
    assert memory_orch.get_data("b", columns=["biogas_volume"]).height == 3


def test_name_or_column(memory_orch):
    with pytest.raises(TypeError, match="dataset name"):
        memory_orch.get_data()
    with pytest.raises(TypeError, match="dataset name"):
        memory_orch.get_data("a", column="b")


def test_a_dataset_is_stored_once(memory_orch):
    memory_orch.store_data("biogas_data", biogas())
    with pytest.raises(TagExistsError, match="data 'biogas_data'"):
        memory_orch.store_data("biogas_data", biogas())
    # Datasets and results are separate kinds: the same tag is fine.
    assert memory_orch._check_new("biogas_data", "result") is None


def test_missing_dataset_and_column(memory_orch):
    with pytest.raises(TagNotFoundError, match="no stored data 'nope'"):
        memory_orch.get_data("nope")
    memory_orch.store_data("biogas_data", biogas())
    with pytest.raises(StorageError, match="no column 'nope'"):
        memory_orch.get_data("biogas_data", columns=["nope"])


@pytest.mark.parametrize(
    ("df", "units", "match"),
    [
        (biogas(), {"nope": "kW"}, "doesn't have"),
        (biogas().iloc[:0], None, "no rows"),
        (pd.DataFrame(index=pd.DatetimeIndex(STAMPS)), None, "no columns"),
    ],
)
def test_tables_that_cant_be_stored_leave_no_run(memory_orch, df, units, match):
    with pytest.raises(StorageError, match=match):
        memory_orch.store_data("bad", df, units=units)
    assert memory_orch.backend.runs("example_a::data") == []


def test_a_write_that_fails_fails_the_run(memory_orch, monkeypatch):
    def broken(run_id, label, value):
        raise StorageError("disk full")

    monkeypatch.setattr(memory_orch.backend, "record", broken)
    with pytest.raises(StorageError, match="disk full"):
        memory_orch.store_data("biogas_data", biogas())
    [run] = memory_orch.backend.runs("example_a::data")
    assert run.status == "failed"
    with pytest.raises(TagNotFoundError):
        memory_orch.get_data(column="biogas_volume")


def test_data_needs_a_study(memory_backend):
    with pytest.raises(StudyNotFoundError):
        Orchestrator(memory_backend).store_data("d", biogas())


# Units


def fake_resolver(identifier):
    known = {
        "M3-PER-DAY": 1 / 86400,
        "m3/day": 1 / 86400,
        "KiloW": 1000.0,
    }
    if identifier not in known:
        raise ValueError(identifier)
    key = "M3-PER-DAY" if identifier == "m3/day" else identifier
    return {"uri": f"http://qudt.org/vocab/unit/{key}", "multiplier": known[identifier]}


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("kW", "KiloW"),  # pint's parts, verified
        ("m3/day", "M3-PER-DAY"),  # pint can't parse it; the resolver's text lookup
        ("furlong/fortnight", None),
        (None, None),
    ],
)
def test_units_with_a_resolver(text, key):
    assert qudt_unit(text, UnitMapper(resolver=fake_resolver)) == key


def test_units_without_a_resolver_are_never_declared():
    assert qudt_unit("kW", UnitMapper()) is None


def test_strict_mapping_falls_back_to_the_resolver():
    assert qudt_unit("m3/day", UnitMapper(resolver=fake_resolver, strict=True)) == (
        "M3-PER-DAY"
    )


def test_resolver_answering_nothing():
    assert qudt_unit("m3/day", UnitMapper(resolver=lambda identifier: None)) is None


def test_a_unit_with_no_key_warns(memory_backend):
    orch = Orchestrator(memory_backend, unit_mapper=UnitMapper(resolver=fake_resolver))
    orch.create_study("s")
    with pytest.warns(UserWarning, match="No QUDT unit for 'furlong'"):
        stored = orch.store_data("d", biogas(), units={"biogas_volume": "furlong"})
    assert stored.units == {"biogas_volume": None, "flow_in_feed": None}
    stored = orch.store_data("e", biogas(), units={"biogas_volume": "kW"})
    assert stored.units["biogas_volume"] == "KiloW"
