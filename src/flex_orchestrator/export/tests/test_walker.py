"""The model data exporter on flex-pse models built from real specs."""

import math

import pyomo.environ as pyo
import pytest
from flexops import TimeBlock
from pyomo.environ import units as pu

from flex_orchestrator.errors import ExportError
from flex_orchestrator.export import ModelData, export_model_data
from flex_orchestrator.flexpse import time_sets

pytestmark = pytest.mark.unit

COEFFS = "site.pump.surrogate_power_electrical.coefficient_vars"


def keys_of(data: ModelData, component: str) -> list[str]:
    return [s.key for s in data if s.component == component]


def timestamps(model) -> list:
    return list(model.time_block.datetime_index)


# Time-only, time x custom, scalars (the demo plant)


def test_time_only_is_one_timeseries_with_the_time_blocks_datetimes(demo_model):
    power = export_model_data(demo_model)["costing.aggregate_electrical_power"]
    assert (power.kind, power.index, power.unit_pyomo, power.unit_qudt) == (
        "timeseries",
        [],
        "kW",
        "KiloW",
    )
    assert power.timestamps == timestamps(demo_model)


@pytest.mark.parametrize(
    ("component", "custom"),
    [
        ("costing.aggregate_power", "electrical"),  # (t, power kind)
        ("demo.tank.outlet_state.flow_vol_phase", "Liq"),  # (t, phase), a state block
    ],
)
def test_time_by_custom_is_a_series_per_custom_value(demo_model, component, custom):
    data = export_model_data(demo_model)
    key = f"{component}[{custom}]"
    assert key in keys_of(data, component)
    series = data[key]
    assert (series.kind, series.index, series.raw_index) == (
        "timeseries",
        [custom],
        (custom,),
    )
    assert series.timestamps == timestamps(demo_model)


def test_references_take_units_from_their_elements(demo_model):
    # flex-pse's flow_in is a Reference; the component itself says dimensionless.
    flow = export_model_data(demo_model)["demo.pump.flow_in"]
    assert (flow.kind, flow.unit_pyomo, flow.unit_qudt) == (
        "timeseries",
        "m**3/h",
        "M3-PER-HR",
    )


def test_unindexed_values_are_scalars(demo_model):
    data = export_model_data(demo_model)
    intensity = data["demo.pump.energy_intensity"]
    assert (intensity.kind, intensity.value, intensity.unit_qudt) == (
        "scalar",
        0.5,
        "KiloW-HR-PER-M3",
    )
    assert data["costing.effective_rate"].unit_qudt == "UNITLESS"


def test_costing_units_get_generated_keys(demo_model):
    annual = export_model_data(demo_model)["costing.annualized_cost"]
    assert (annual.unit_pyomo, annual.unit_qudt, annual.unit_verified) == (
        "USD/a",
        "CCY_USD-PER-YR",
        False,
    )


def test_unsolved_values_are_stale(demo_model):
    # flex-pse initialises most variables; the battery's state isn't, until a solve.
    data = export_model_data(demo_model, include=["demo.battery.*"])
    assert set(data.stale()) == {"demo.battery.charge", "demo.battery.soc"}
    assert data["demo.battery.charge"].values[0] is None


def test_nan_is_missing(demo_model):
    demo_model.demo.pump.energy_intensity.set_value(math.nan, skip_validation=True)
    data = export_model_data(demo_model, keys=["demo.pump.energy_intensity"])
    assert data["demo.pump.energy_intensity"].value is None
    assert data.stale() == ["demo.pump.energy_intensity"]


# Custom-only, fixed values, indexed blocks, networks


def test_custom_only_is_a_scalar_per_element(surrogate_model):
    data = export_model_data(surrogate_model)
    assert keys_of(data, COEFFS) == [f"{COEFFS}[flow_out]", f"{COEFFS}[intercept]"]
    intercept = data[f"{COEFFS}[intercept]"]
    assert (intercept.kind, intercept.value, intercept.index) == (
        "scalar",
        1.5,
        ["intercept"],
    )


def test_dispatched_values_are_exported(surrogate_model):
    charge = export_model_data(surrogate_model)["site.battery.power_charge"]
    assert charge.values == [0.0, 1.0, 2.0, 2.0, 0.5, 0.0]
    assert not charge.stale


def test_indexed_blocks_name_each_member(logic_model):
    keys = export_model_data(logic_model).keys()
    assert {"p.ro[0].status", "p.ro[1].status", "p.ro[2].status"} <= set(keys)


def test_network_paths(network_model):
    flow = export_model_data(network_model)["net.a.pump.flow_in"]
    assert flow.kind == "timeseries"
    assert flow.timestamps == timestamps(network_model)


# Time detection by identity, on real flex-pse sets


def test_a_set_of_time_like_integers_is_not_time(logic_model):
    # p.trains = [0, 1, 2] holds valid time positions, but it isn't time, so the
    # trains stay separate block members rather than becoming points of a series.
    assert set(logic_model.p.trains) <= set(logic_model.time_block.time_index)
    data = export_model_data(logic_model, include=["p.ro*power_electrical"])
    assert data.keys() == [f"p.ro[{i}].power_electrical" for i in range(3)]
    assert {s.kind for s in data} == {"timeseries"}


def test_startup_without_its_time_set_is_custom(logic_model):
    # startup is indexed over t >= 1 by an anonymous Set: by identity, not time.
    data = export_model_data(logic_model, include=["p.ro*startup"])
    n = len(logic_model.time_block.time_index)
    assert keys_of(data, "p.ro[0].startup") == [
        f"p.ro[0].startup[{t}]" for t in range(1, n)
    ]
    assert {s.kind for s in data} == {"scalar"}


def test_flexpse_time_sets_hook_makes_startup_a_timeseries(logic_model):
    sets = time_sets(logic_model)
    assert sets[0] is logic_model.time_block.time_index
    assert any(s is logic_model.p.ro[0].startup.index_set() for s in sets)
    assert all(s is not logic_model.p.trains for s in sets)
    data = export_model_data(logic_model, time_sets=sets, include=["p.ro*"])
    startup = data["p.ro[0].startup"]
    assert startup.kind == "timeseries"
    assert (
        startup.timestamps == timestamps(logic_model)[1:]
    )  # every point but the first
    assert data["p.ro[0].status"].timestamps == timestamps(logic_model)


def test_time_sets_hook_ignores_a_startup_var_that_isnt_over_time(demo_model):
    # A user extension may add a Var called startup over something else.
    demo_model.demo.pump.startup = pyo.Var(["cold", "warm"])
    assert time_sets(demo_model) == [demo_model.time_block.time_index]


def test_time_sets_hook_on_a_model_without_logic(demo_model):
    sets = time_sets(demo_model)
    assert len(sets) == 1 and sets[0] is demo_model.time_block.time_index


def test_two_time_blocks_are_ambiguous(demo_model):
    demo_model.other = TimeBlock(
        start_date="2025-07-08", end_date="2025-07-08T02:00", time_step=pu.hr
    )
    with pytest.raises(ExportError, match="several time blocks"):
        export_model_data(demo_model)
    t = demo_model.time_block.time_index
    data = export_model_data(demo_model, keys=["demo.pump.flow_in"], time_sets=[t])
    assert data["demo.pump.flow_in"].timestamps == timestamps(demo_model)


def test_time_subset_must_be_a_subset(demo_model, network_model):
    # The network's 24-point time set isn't a subset of the demo's 6 points.
    with pytest.raises(ExportError, match="isn't a subset"):
        export_model_data(
            demo_model,
            time_sets=[
                demo_model.time_block.time_index,
                network_model.time_block.time_index,
            ],
        )


def test_datetime_index_length_must_match(demo_model):
    with pytest.raises(ExportError, match="datetime_index has 2 entries"):
        export_model_data(
            demo_model,
            time_sets=[demo_model.time_block.time_index],
            datetime_index=demo_model.time_block.datetime_index[:2],
        )


# Selection


def test_default_walk_skips_private_names(demo_model):
    keys = export_model_data(demo_model).keys()
    assert not [key for key in keys if "._" in key or key.startswith("_")]


def test_private_names_export_when_named(demo_model):
    key = "demo.pump._flow_vol_phase_inlet_ref"
    assert keys_of(export_model_data(demo_model, keys=[key]), key)


def test_deactivated_blocks_are_skipped(demo_model):
    demo_model.demo.battery.deactivate()
    keys = export_model_data(demo_model).keys()
    assert not [key for key in keys if key.startswith("demo.battery.")]


def test_keys_only(demo_model):
    keys = ["demo.pump.flow_in", "costing.annualized_cost"]
    assert export_model_data(demo_model, keys=keys).keys() == keys


def test_key_for_one_element(surrogate_model):
    key = f"{COEFFS}[intercept]"
    assert export_model_data(surrogate_model, keys=[key]).keys() == [key]


def test_keys_plus_include(demo_model):
    data = export_model_data(
        demo_model, keys=["costing.annualized_cost"], include=["demo.pump.*"]
    )
    first, *rest = list(data)
    assert first.key == "costing.annualized_cost"
    assert rest and all(s.component.startswith("demo.pump.") for s in rest)


def test_include_and_exclude_globs(demo_model):
    data = export_model_data(demo_model, include=["demo.pump.*"], exclude=["*flow*"])
    assert "demo.pump.power_electrical" in data.keys()
    assert not [s for s in data if "flow" in s.component]


@pytest.mark.parametrize(
    ("key", "match"),
    [
        ("demo.pump.nope", "names no"),
        ("demo.pump.power_electrical_relation", "Constraint"),
    ],
)
def test_bad_keys(demo_model, key, match):
    with pytest.raises(ExportError, match=match):
        export_model_data(demo_model, keys=[key])


def test_max_scalars_skips_big_custom_components_unless_named(logic_model):
    with pytest.warns(UserWarning, match="max_scalars_per_component=3"):
        data = export_model_data(
            logic_model, include=["p.ro*"], max_scalars_per_component=3
        )
    assert keys_of(data, "p.ro[0].startup") == []
    named = export_model_data(
        logic_model, keys=["p.ro[0].startup"], max_scalars_per_component=3
    )
    assert len(named) == len(logic_model.time_block.time_index) - 1


# ModelData


def test_model_data_access(surrogate_model):
    data = export_model_data(
        surrogate_model, keys=["site.battery.power_charge", COEFFS]
    )
    assert [s.key for s in data.timeseries()] == ["site.battery.power_charge"]
    assert len(data.scalars()) == 2
    assert len(data) == 3
    with pytest.raises(TypeError, match="timeseries"):
        _ = data["site.battery.power_charge"].value
    with pytest.raises(KeyError):
        data["nope"]
