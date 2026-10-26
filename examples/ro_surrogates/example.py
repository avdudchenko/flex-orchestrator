"""Compare a fixed RO energy intensity with a fitted linear surrogate.

A feed -> RO -> tank -> product plant must deliver a constant 90 m^3/hr while
buying electricity on a time-of-use tariff (an EECO tariff file). The tank
lets the RO shift production to off-peak hours. The RO's specific energy
depends on its permeate flow, so how the model represents that changes the
schedule it picks. The script:

1. stores a month of synthetic RO operating data, generated from a parabolic
   specific-energy curve;
2. builds the model in Python, emits its spec with flex-pse and stores it;
3. fits the data's average energy intensity and a linear power surrogate;
4. solves the model with each and stores the results;
5. reads the results back and checks each model's RO energy against the
   curve the data came from.

    python examples/ro_surrogates/example.py
"""

import os
import shutil
from pathlib import Path

import flexops as fo
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from flexparameterize import ConstantIntensityRegressor, LinearRegressor
from pyomo.environ import units as u
from pyomo.network import Arc

from flex_orchestrator import Orchestrator

HERE = Path(__file__).parent


def fresh_store(folder: Path) -> Path:
    """Return ``folder/tmp_flexorch``, the example's store, deleted first.

    Starting from an empty store means a store left by an older Acquirium never
    has to be upgraded. Set ``FLEXORCH_KEEP_EXAMPLE_STORE=1`` to keep it.
    """
    store = folder / "tmp_flexorch"
    if store.exists() and os.environ.get("FLEXORCH_KEEP_EXAMPLE_STORE") != "1":
        shutil.rmtree(store)
    return store


flow = u.m**3 / u.hr

UNITS = {"permeate": "m^3/hr", "power_electrical": "kW"}  # of the operating data
KEEP = [  # what each result stores
    "plant.ro.permeate",
    "plant.ro.power_electrical",
    "costing.aggregate_operating_cost",
]


def sec(permeate):
    """The RO's true specific energy (kWh/m^3), lowest at 90 m^3/hr."""
    return 2.5 + 4e-4 * (permeate - 90) ** 2


def operating_data():
    """A month of hourly RO data: power = sec(permeate) * permeate, plus noise."""
    rng = np.random.default_rng(0)
    time = pd.date_range("2025-06-01", periods=30 * 24, freq="h", tz="UTC")
    permeate = rng.uniform(30, 150, len(time))
    power = sec(permeate) * permeate * rng.normal(1, 0.02, len(time))
    return pd.DataFrame({"permeate": permeate, "power_electrical": power}, time)


def ro_model():
    """The model, built in Python and priced by the tariff file."""
    m = pyo.ConcreteModel(name="ro_plant")
    m.time_block = fo.TimeBlock(
        start_date="2025-07-08", end_date="2025-07-10", time_step=1 * u.hr
    )
    m.properties = fo.SimpleAqueousFlow()
    m.costing = fo.FlexCosting(
        time_block=m.time_block, tariff_file=str(HERE / "tariff.csv")
    )
    m.plant = fo.PlantBlock(time_block=m.time_block)
    m.plant.feed = fo.Feed(property_package=m.properties, max_withdrawal=300 * flow)
    m.plant.ro = fo.ReverseOsmosis(
        property_package=m.properties,
        costing_package=m.costing,
        recovery=0.5,
        energy_intensity=3.0 * u.kWh / u.m**3,
    )
    m.plant.tank = fo.Tank(
        property_package=m.properties,
        max_volume=1500 * u.m**3,
        initial_volume=750 * u.m**3,
    )
    m.plant.product = fo.Product(
        property_package=m.properties, min_demand=90 * flow, max_demand=90 * flow
    )
    m.plant.brine = fo.Product(property_package=m.properties)
    m.plant.feed_to_ro = Arc(source=m.plant.feed.outlet_a, destination=m.plant.ro.inlet)
    m.plant.ro_to_tank = Arc(source=m.plant.ro.outlet_a, destination=m.plant.tank.inlet)
    m.plant.ro_to_brine = Arc(
        source=m.plant.ro.outlet_b, destination=m.plant.brine.inlet_a
    )
    m.plant.tank_to_product = Arc(
        source=m.plant.tank.outlet, destination=m.plant.product.inlet_a
    )
    pyo.TransformationFactory("network.expand_arcs").apply_to(m)
    m.costing.cost_process()
    m.objective = pyo.Objective(expr=m.costing.aggregate_operating_cost)
    return m


def compare(orch):
    """Print each result's cost and its RO energy against the true curve.

    "True" energy is what the RO would really use running the schedule the
    model picked.
    """
    print("Comparing estimated vs real power use due to surrogate simplifications")
    for result in orch.stored_model_results():
        cost = result.scalars()["costing.aggregate_operating_cost"]
        ro = result.to_pandas()
        model_kwh = ro["plant.ro.power_electrical"].sum()
        true_kwh = (sec(ro["plant.ro.permeate"]) * ro["plant.ro.permeate"]).sum()
        print(
            f"{result.tag}: ${cost:,.2f}, RO energy {model_kwh:,.0f} kWh "
            f"(true {true_kwh:,.0f} kWh)"
        )


if __name__ == "__main__":
    with Orchestrator(data_dir=fresh_store(HERE)) as orch:
        if "ro_surrogates" in orch.studies():
            orch.delete_study("ro_surrogates")
        orch.create_study("ro_surrogates")

        # 1. The synthetic plant data.
        orch.store_data("ro_operation", operating_data(), units=UNITS)

        # 2. The model's spec, emitted by flex-pse.
        orch.store_model_spec(
            "base", fo.emit_model(ro_model(), relative_to=HERE), base_dir=HERE
        )

        # 3. Two fits from the stored data: the average intensity, and power as
        # a linear function of permeate flow.
        data = orch.get_data("ro_operation", frame="pandas")
        X, y = data[["permeate"]], data[["power_electrical"]]
        average = ConstantIntensityRegressor().fit(X, y)
        linear = LinearRegressor().fit(
            X, y, input_units={"permeate": "m^3/hr"}, output_units="kW"
        )
        print(f"average intensity: {average.coefficient:.3f} kWh/m^3")
        slope = linear.coefficients["permeate"]
        intercept = linear.coefficients["intercept"]
        print(f"linear surrogate: power = {slope:.3f} * permeate {intercept:+.1f} kW")
        orch.store_surrogate_spec("ro_average", average.to_surrogate_spec())
        orch.store_surrogate_spec("ro_linear", linear.to_surrogate_spec())

        # 4. Solve with each: the average fixes the RO's energy_intensity, the
        # linear fit replaces its power relation.
        fixed = {
            "plant.ro.energy_intensity": {
                "value": average.coefficient,
                "units": "kWh/m^3",
            }
        }
        orch.solve_and_store(
            "fixed_intensity", orch.build("base", **fixed), include=KEEP
        )
        spec = orch.apply_surrogate("base", unit="plant.ro", surrogate="ro_linear")
        orch.store_model_spec("ro_linear", spec, parent="base")
        orch.solve_and_store("linear_surrogate", orch.build("ro_linear"), include=KEEP)

        # 5. The results, read back from Acquirium.
        compare(orch)
