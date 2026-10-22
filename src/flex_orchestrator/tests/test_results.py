"""The model snapshot and native-stream writer (PLAN.md D3 step 4, D5)."""

from datetime import UTC, datetime, timedelta

import pytest

from flex_orchestrator.errors import StorageError, UnitError
from flex_orchestrator.export import ExportedSeries, ModelData, export_model_data
from flex_orchestrator.flexpse import time_sets
from flex_orchestrator.orchestrator import Orchestrator
from flex_orchestrator.results import (
    SNAPSHOT_SCHEMA,
    convert,
    read_snapshot,
    snapshot,
)

pytestmark = pytest.mark.unit

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def series(key, values, timestamps=None, unit="kW", kind="timeseries"):
    if kind == "timeseries" and timestamps is None:
        timestamps = [T0 + timedelta(hours=i) for i in range(len(values))]
    return ExportedSeries(key, kind, key, [], values, timestamps, unit, None)


def test_a_subset_of_time_keeps_its_rows(logic_model):
    data = export_model_data(
        logic_model, time_sets=time_sets(logic_model), include=["p.ro*"]
    )
    table, manifest = snapshot(data)
    entries = {e["key"]: e for e in manifest["series"]}
    n = len(logic_model.time_block.time_index)
    assert entries["p.ro[0].startup"]["rows"] == list(range(1, n))
    assert "rows" not in entries["p.ro[0].status"]
    assert table[0, "p.ro[0].startup"] is None  # no value at the first point
    back = read_snapshot(table, manifest)
    assert back["p.ro[0].startup"].timestamps == back["p.ro[0].status"].timestamps[1:]
    assert back["p.ro[0].startup"].values == data["p.ro[0].startup"].values


def test_an_unknown_snapshot_schema_is_refused():
    with pytest.raises(StorageError, match="flexorch-model/9"):
        read_snapshot(None, {"schema": "flexorch-model/9", "series": []})


def test_a_key_named_time_is_refused():
    with pytest.raises(StorageError, match="can't be named 'time'"):
        snapshot(ModelData([series("time", [1.0])]))


def test_scalars_only_have_no_table():
    data = ModelData([series("x", [2.0], kind="scalar")])
    table, manifest = snapshot(data)
    assert table is None
    assert manifest == {
        "schema": SNAPSHOT_SCHEMA,
        "series": [
            {
                "key": "x",
                "kind": "scalar",
                "component": "x",
                "index": [],
                "pyomo_unit": "kW",
                "qudt_unit": None,
                "unit_verified": False,
                "value": 2.0,
            }
        ],
    }
    assert read_snapshot(table, manifest)["x"].value == 2.0


def test_convert_leaves_same_units_and_needs_a_unit():
    data = ModelData([series("a", [1.0]), series("b", [1.0], unit=None)])
    assert convert(data, {"a": "kW"})["a"] is data["a"]
    with pytest.raises(UnitError, match="no unit to convert from"):
        convert(data, {"b": "kW"})


def test_a_native_stream_reuses_its_declaration(memory_backend):
    orch = Orchestrator(memory_backend)
    orch.create_study("s")
    data = ModelData([series("p", [1.0, 2.0])])
    orch.store_model_data("a", data, streams=["p"])
    stored = orch.store_model_data("b", data, streams=["p"])
    [native] = [
        r for r in memory_backend.records("s::runs", stored.run_id) if r.label == "p"
    ]
    assert [v for _, v in native.rows] == [1.0, 2.0]
