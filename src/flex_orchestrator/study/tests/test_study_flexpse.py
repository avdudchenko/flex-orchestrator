"""The M4 exit: fit-then-sweep as one StudyConfig, with flex-pse models.

A feed -> RO -> tank -> product plant priced by a tariff file. One config
fits two ``multilinear`` variants of the RO's power (two windows of a stored
dataset) and a ``constant_intensity`` one, swaps each into the RO and solves.
Running the fits again on the same data gives the same ``data_hash`` and
identical specs. Each test runs on the memory backend and on Acquirium.
"""

import copy
import shutil
from importlib.resources import files

import numpy as np
import pandas as pd
import pytest

from flex_orchestrator.errors import TagExistsError

pytest.importorskip("flexops")
pytestmark = [pytest.mark.component, pytest.mark.needs_highs]

TARIFF = files("flex_orchestrator.testing") / "data" / "tariff_tou_demo.csv"


def ro_spec():
    """feed -> RO -> tank -> product, a day hourly, priced by tariff.csv."""
    p = "plant"
    unit = {"kind": "unit"}
    return {
        "schema_version": "0.1.0",
        "name": "ro_day",
        "elements": [
            {
                "kind": "time",
                "start_date": "2025-07-08",
                "end_date": "2025-07-09",
                "time_step": "1 hr",
            },
            {
                "kind": "property_package",
                "name": "properties",
                "property_class": "SimpleAqueousFlow",
            },
            {"kind": "costing", "name": "costing", "tariff_source": "tariff.csv"},
            {"kind": "plant", "name": p},
            {
                **unit,
                "name": f"{p}.feed",
                "unit_model_class": "Feed",
                "construction_options": {
                    "max_withdrawal": {"value": 300, "units": "m^3/hr"}
                },
                "connections": [{"port": "outlet_a", "to": f"{p}.ro.inlet"}],
            },
            {
                **unit,
                "name": f"{p}.ro",
                "unit_model_class": "ReverseOsmosis",
                "construction_options": {
                    "recovery": 0.5,
                    "energy_intensity": {"value": 3.0, "units": "kWh/m^3"},
                },
                "connections": [
                    {"port": "outlet_a", "to": f"{p}.tank.inlet"},
                    {"port": "outlet_b", "to": f"{p}.brine.inlet_a"},
                ],
            },
            {
                **unit,
                "name": f"{p}.tank",
                "unit_model_class": "Tank",
                "costing_package": None,
                "construction_options": {
                    "max_volume": {"value": 1500.0, "units": "m^3"},
                    "initial_volume": {"value": 750.0, "units": "m^3"},
                },
                "connections": [{"port": "outlet", "to": f"{p}.product.inlet_a"}],
            },
            {
                **unit,
                "name": f"{p}.product",
                "unit_model_class": "Product",
                "construction_options": {
                    "min_demand": {"value": 90.0, "units": "m^3/hr"},
                    "max_demand": {"value": 90.0, "units": "m^3/hr"},
                },
            },
            {**unit, "name": f"{p}.brine", "unit_model_class": "Product"},
            {"kind": "objective", "name": "objective"},
        ],
    }


def operating_data():
    """Two weeks of RO data whose specific energy rises after the first week."""
    rng = np.random.default_rng(1)
    time = pd.date_range("2025-06-01", periods=14 * 24, freq="h", tz="UTC")
    permeate = rng.uniform(40, 140, len(time))
    sec = np.where(time < "2025-06-08", 2.8, 3.4)
    power = sec * permeate + rng.normal(0, 2, len(time))
    return pd.DataFrame({"permeate": permeate, "power_electrical": power}, time)


FIT = {
    "tag": "ro_{name}",
    "X": {"dataset": "ro_operation", "columns": ["permeate"]},
    "y": {"dataset": "ro_operation", "columns": ["power_electrical"]},
    "variants": [
        {
            "name": "week1",
            "regressor": "multilinear",
            "window": {"end": "2025-06-07T23:00:00Z"},
        },
        {
            "name": "week2",
            "regressor": "multilinear",
            "window": {"start": "2025-06-08T00:00:00Z"},
        },
        {"name": "average", "regressor": "constant_intensity"},
    ],
}
CONFIG = {
    "base_model_spec": "base",
    "fits": [FIT],
    "sweeps": [
        {
            "type": "surrogate",
            "unit": "plant.ro",
            "surrogates": ["ro_week1", "ro_week2", "ro_average"],
        }
    ],
    "export": {
        "include": [
            "plant.ro.permeate",
            "plant.ro.power_electrical",
            "costing.aggregate_operating_cost",
        ],
        "streams": ["plant.ro.power_electrical"],
    },
    "output_name": "{surrogate}",
}


@pytest.fixture(
    params=["memory", "acquirium"],
)
def study(request, tmp_path, study_name):
    from flex_orchestrator.backend import AcquiriumBackend, MemoryBackend
    from flex_orchestrator.orchestrator import Orchestrator

    if request.param == "memory":
        backend = MemoryBackend()
    else:
        backend = AcquiriumBackend(request.getfixturevalue("acquirium_local"))
    orch = Orchestrator(backend, environ={})
    orch.create_study(study_name)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    shutil.copy(TARIFF, inputs / "tariff.csv")
    orch.store_model_spec("base", ro_spec(), base_dir=inputs)
    orch.store_data(
        "ro_operation",
        operating_data(),
        units={"permeate": "m^3/hr", "power_electrical": "kW"},
    )
    return orch


def test_fit_then_sweep_in_one_config(study):
    run = study.execute_study(copy.deepcopy(CONFIG))
    assert run.counts() == {"succeeded": 3}
    fits = {f.tag: f for f in run.fits[0].fits}
    assert fits["ro_week1"].coefficients["permeate"] == pytest.approx(2.8, abs=0.05)
    assert fits["ro_week2"].coefficients["permeate"] == pytest.approx(3.4, abs=0.05)
    assert fits["ro_average"].coefficients["energy_intensity"] == pytest.approx(
        3.1, abs=0.1
    )
    costs = {
        r.tag: study.result(r.tag).scalars()["costing.aggregate_operating_cost"]
        for r in run.results
    }
    assert costs["ro_week1"] < costs["ro_average"] < costs["ro_week2"]
    derived = study.model_spec("ro_week2")
    assert derived.parent_tag == "base"
    [surrogate] = [e for e in derived.spec["elements"] if e["kind"] == "surrogate"]
    assert surrogate["unit"] == "plant.ro"
    assert surrogate["data"]["coefficients"] == fits["ro_week2"].coefficients
    # The tariff file is reused from the base spec, not read again.
    assert derived.assets == study.model_spec("base").assets
    # The constant intensity fixes the RO's energy_intensity.
    model = study.build("ro_average")
    assert model.plant.ro.energy_intensity.fixed
    assert model.plant.ro.energy_intensity.value == pytest.approx(
        fits["ro_average"].coefficients["energy_intensity"]
    )

    # The same data gives the same data_hash and identical specs.
    hashes = {tag: study.surrogate_spec(tag).spec_hash for tag in fits}
    rerun = study.fit_surrogate(**{**FIT, "on_existing": "replace"})
    assert {f.tag: f.data_hash for f in rerun.fits} == {
        t: f.data_hash for t, f in fits.items()
    }
    assert {tag: study.surrogate_spec(tag).spec_hash for tag in fits} == hashes

    # Resume: skip does nothing new; error raises before anything runs.
    stored = len(study.backend.runs(study._runs))
    skipped = study.execute_study({**CONFIG, "fits": [], "on_existing": "skip"})
    assert skipped.counts() == {"skipped": 3}
    with pytest.raises(TagExistsError):
        study.execute_study({**CONFIG, "fits": []})
    assert len(study.backend.runs(study._runs)) == stored


def test_a_configuration_sweep_edits_construction_options(study):
    run = study.execute_study(
        {
            "base_model_spec": "base",
            "sweeps": [
                {
                    "type": "configuration_option",
                    "name": "withdrawal",
                    "path": "plant.feed.max_withdrawal",
                    "values": [
                        {"value": 181, "units": "m^3/hr"},
                        {"value": 300, "units": "m^3/hr"},
                    ],
                },
                {
                    "type": "configuration_option",
                    "name": "tank",
                    "path": "plant.tank.max_volume.value",
                    "values": [1000.0],
                },
                {
                    "type": "variable",
                    "name": "recovery",
                    "path": "plant.ro.recovery",
                    "values": [0.5],
                },
            ],
            "export": {"include": ["costing.aggregate_operating_cost"]},
            "output_name": "w{withdrawal}",
        }
    )
    assert run.counts() == {"succeeded": 2}
    feed = {e.get("name"): e for e in study.model_spec("w181").spec["elements"]}
    assert feed["plant.feed"]["construction_options"]["max_withdrawal"] == {
        "value": 181,
        "units": "m^3/hr",
    }
    assert feed["plant.tank"]["construction_options"]["max_volume"] == {
        "value": 1000.0,
        "units": "m^3",
    }
    model = study.build("w181", **{"plant.ro.recovery": 0.5})
    assert all(
        p.value == pytest.approx(181) for p in model.plant.feed.withdrawal_max.values()
    )


def test_a_price_sweep_edits_the_costing_element(study):
    spec = ro_spec()
    costing = spec["elements"][2]
    del costing["tariff_source"]
    costing["energy_prices"] = {"electrical": {"value": 0.1, "units": "USD/kWh"}}
    study.store_model_spec("flat", spec)
    run = study.execute_study(
        {
            "base_model_spec": "flat",
            "sweeps": [
                {
                    "type": "configuration_option",
                    "name": "price",
                    "path": "costing.energy_prices.electrical",
                    "values": [
                        {"value": 0.1, "units": "USD/kWh"},
                        {"value": 0.2, "units": "USD/kWh"},
                    ],
                }
            ],
            "export": {"include": ["costing.aggregate_operating_cost"]},
            "output_name": "price_{price}",
            "solve_options": {"prefer": "ipopt"},
        }
    )
    assert run.counts() == {"succeeded": 2}
    low, high = (
        study.result(t).scalars()["costing.aggregate_operating_cost"]
        for t in ("price_0.1", "price_0.2")
    )
    assert high == pytest.approx(2 * low, rel=1e-4)
