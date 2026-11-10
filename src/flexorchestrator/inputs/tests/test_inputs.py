"""Model inputs: stored data lined up with a model's time steps.

The resolver alone, on memory and files, with the time steps given directly;
``test_inputs_study.py`` runs them through studies and builds.
"""

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from flexorchestrator.errors import ModelInputError
from flexorchestrator.fitting import selectors
from flexorchestrator.inputs import ModelInput
from flexorchestrator.inputs.resolve import resolve

HOURS = pd.date_range("2025-07-07", periods=72, freq="h", tz="UTC")
GRID = pd.date_range("2025-07-08", periods=8, freq="15min")  # 00:00 to 01:45


@pytest.fixture
def stored(orch):
    """``ops``: hourly demand 0, 1, 2, ... (m^3/hr) and a unitless count."""
    frame = pd.DataFrame(
        {"demand": np.arange(72.0), "count": np.arange(72.0) * 10}, HOURS
    )
    orch.store_data("ops", frame, units={"demand": "m^3/hr"})
    return orch


def entry(**fields):
    return ModelInput(dataset="ops", columns=["demand"], **fields)


def test_hourly_data_fills_every_step(stored):
    found = resolve(stored, [entry(rename={"demand": "plant.d"})], GRID)
    value = found.values["plant.d"]
    assert value["units"] == "m^3/hr"
    # 24 at 00:00 and 25 at 01:00 (UTC); the quarter hours between are filled.
    assert value["value"] == [24.0, 24.25, 24.5, 24.75, 25.0, 25.25, 25.5, 25.75]
    [record] = found.provenance
    assert record["paths"] == ["plant.d"]
    assert record["filled"] == {"plant.d": 6}
    assert record["resolved"]["dataset"] == "ops" and record["resolved"]["run_id"]
    assert record["window"] == {
        "start": "2025-07-08 00:00:00",
        "end": "2025-07-08 02:00:00",
    }
    assert record["data_hash"].startswith("sha256:")


def test_a_column_without_a_unit_is_a_plain_list(stored):
    found = resolve(
        stored, [ModelInput(dataset="ops", columns=["count"], gaps="ffill")], GRID
    )
    assert found.values["count"] == [240.0] * 4 + [250.0] * 4


def test_the_facility_time_zone(stored):
    # 00:00 in Los Angeles (PDT) is 07:00 UTC, when the demand was 31.
    found = resolve(stored, [entry(timezone="America/Los_Angeles")], GRID)
    assert found.values["demand"]["value"][0] == 31.0


def test_daylight_saving_days(orch):
    stamps = pd.date_range("2025-03-08", "2025-11-04", freq="h", tz="UTC")
    orch.store_data(
        "dst", pd.DataFrame({"v": np.arange(len(stamps), dtype=float)}, stamps)
    )
    zone = "America/Los_Angeles"
    spring = pd.date_range("2025-03-09", periods=24, freq="h")
    found = resolve(orch, [ModelInput(dataset="dst", timezone=zone)], spring)
    values = found.values["v"]
    # 02:00 doesn't exist that day: interpolated between 01:00 and 03:00.
    assert values[2] == (values[1] + values[3]) / 2
    assert found.provenance[0]["filled"] == {"v": 1}
    autumn = pd.date_range("2025-11-02", periods=24, freq="h")
    found = resolve(orch, [ModelInput(dataset="dst", timezone=zone)], autumn)
    values = found.values["v"]
    # 01:00 happens twice; its two samples average into one step.
    assert values[1] == values[0] + 1.5
    assert values[2] == values[0] + 3


def test_resample_aggregates_onto_a_coarser_step(stored):
    grid = pd.date_range("2025-07-08", periods=2, freq="4h")
    mean = resolve(stored, [entry()], grid).values["demand"]["value"]
    total = resolve(stored, [entry(resample={"agg": "sum"})], grid)
    assert mean == [25.5, 29.5]
    assert total.values["demand"]["value"] == [102.0, 118.0]


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        ("interpolate", [24.0, 24.5, 25.0]),
        ("ffill", [24.0, 24.0, 25.0]),
        ("zero", [24.0, 0.0, 25.0]),
    ],
)
def test_gap_rules(stored, gaps, expected):
    grid = pd.date_range("2025-07-08", periods=3, freq="30min")
    found = resolve(stored, [entry(gaps=gaps)], grid)
    assert found.values["demand"]["value"] == expected


def test_gaps_that_cant_be_filled(stored):
    with pytest.raises(ModelInputError, match="no value at 6 of the model's 8"):
        resolve(stored, [entry(gaps="error")], GRID)
    with pytest.raises(ModelInputError, match="with limit 2"):
        resolve(stored, [entry(limit=2)], GRID)
    after = pd.date_range("2025-07-09T23:00", periods=4, freq="30min")
    with pytest.raises(ModelInputError, match="no value at 3 of the model's 4"):
        resolve(stored, [entry()], after)  # 23:00 is the last sample


def test_reduce_gives_one_number(stored):
    last = resolve(stored, [entry(reduce="last")], GRID).values["demand"]
    mean = resolve(stored, [entry(reduce="mean")], GRID).values["demand"]
    assert last == {"value": 25.0, "units": "m^3/hr"}
    assert mean["value"] == 24.5
    everything = resolve(stored, [entry(reduce="max")], None)
    assert everything.values["demand"]["value"] == 71.0
    assert everything.provenance[0]["window"] is None


def test_a_model_without_time_steps_takes_only_reduced_inputs(stored):
    with pytest.raises(ModelInputError, match="no time axis"):
        resolve(stored, [entry()], None)


def test_offset_reads_another_window(stored):
    found = resolve(stored, [entry(offset="-1D", reduce="first")], GRID)
    assert found.values["demand"]["value"] == 0.0
    assert found.provenance[0]["window"]["start"] == "2025-07-07 00:00:00"


def test_units_convert(stored):
    found = resolve(stored, [entry(units="m^3/s", reduce="first")], GRID)
    assert found.values["demand"]["value"] == pytest.approx(24 / 3600)
    assert found.values["demand"]["units"] == "m^3/s"


def test_paths_set_once_per_list_and_swept_entries_replace(stored):
    two = [entry(rename={"demand": "p"}), entry(rename={"demand": "p"}, reduce="last")]
    with pytest.raises(ModelInputError, match=r"both set \['p'\]"):
        resolve(stored, two, GRID)
    base = [
        ModelInput(dataset="ops", columns=["demand", "count"], rename={"demand": "p"})
    ]
    found = resolve(
        stored, base, GRID, swept=[entry(rename={"demand": "p"}, reduce="last")]
    )
    assert found.values["p"]["value"] == 25.0
    assert [r["paths"] for r in found.provenance] == [["count"], ["p"]]


def test_a_selector_that_matches_nothing(stored):
    with pytest.raises(
        ModelInputError, match="Model input: .*no dataset|Model input: "
    ):
        resolve(stored, [ModelInput(dataset="nope")], GRID)
    later = pd.date_range("2030-01-01", periods=2, freq="h")
    with pytest.raises(ModelInputError, match="matched no rows"):
        resolve(stored, [entry()], later)


def test_each_distinct_selection_is_read_once(stored, monkeypatch):
    calls = []
    original = selectors.load
    monkeypatch.setattr(
        selectors, "load", lambda *a, **k: calls.append(a[1]) or original(*a, **k)
    )
    cache = {}
    for _ in range(3):
        resolve(stored, [entry(), entry(rename={"demand": "x"})], GRID, cache=cache)
    assert len(calls) == 2  # the rename is part of the selection


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"timezone": "Mars/Olympus"}, "unknown time zone"),
        ({"offset": "a while"}, "isn't a duration"),
        ({"reduce": "last", "gaps": "zero"}, "don't apply"),
        ({"limit": 0}, "greater than or equal to 1"),
        ({"gaps": "guess"}, "Input should be"),
    ],
)
@pytest.mark.unit
def test_invalid_entries(fields, message):
    with pytest.raises(ValidationError, match=message):
        entry(**fields)
