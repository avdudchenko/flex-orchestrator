"""Design studies (flex-pse v0.3.0's flexdesign) through the orchestrator."""

import json

import pytest

pytest.importorskip("flexdesign")

from flexcore.config import design as upstream  # noqa: E402
from flexdesign import design_results  # noqa: E402

from flex_orchestrator.archive import export_study, import_study  # noqa: E402
from flex_orchestrator.assets import find_assets  # noqa: E402
from flex_orchestrator.errors import (  # noqa: E402
    ExportError,
    SpecError,
    StudyConfigError,
)
from flex_orchestrator.export import export_model_data  # noqa: E402
from flex_orchestrator.flexpse import (  # noqa: E402
    apply_surrogate,
    apply_values,
    build,
    configure,
    solve,
    time_sets,
)
from flex_orchestrator.hooks import DEFAULT_HOOKS  # noqa: E402
from flex_orchestrator.specs import FLEXPSE_DESIGN, detect_format  # noqa: E402
from flex_orchestrator.study.config import (  # noqa: E402
    CapacityDecision,
    DesignStudy,
    ParameterChange,
    PlanningYear,
    RepresentativeMonth,
    load_config,
)
from flex_orchestrator.study.design import compose_design  # noqa: E402
from flex_orchestrator.study.runner import expand  # noqa: E402
from flex_orchestrator.testing import DEMO_SPEC, LINEAR_POWER  # noqa: E402
from flex_orchestrator.testing.orchestrators import (  # noqa: E402
    memory_orchestrator,
)

BATTERY = "battery.energy_capacity"


def flowsheet():
    """A one-day pump load, solar and a cyclic battery, flat priced."""
    return {
        "schema_version": "0.1.1",
        "name": "base",
        "elements": [
            {
                "kind": "time",
                "start_date": "2025-01-01",
                "end_date": "2025-01-02",
                "time_step": "1 hr",
            },
            {
                "kind": "property_package",
                "name": "properties",
                "property_class": "SimpleAqueousFlow",
                "options": {
                    "has_pressure": True,
                    "density": {"value": 1000.0, "units": "kg/m^3"},
                },
            },
            {
                "kind": "costing",
                "name": "costing",
                "energy_prices": {"electrical": {"value": 0.10, "units": "USD/kWh"}},
                "export_prices": {"electrical": {"value": 0.0, "units": "USD/kWh"}},
                "discount_rate": 0.07,
            },
            {
                "kind": "unit",
                "name": "pump",
                "unit_model_class": "Pump",
                "construction_options": {
                    "power_relation": "hydraulic",
                    "efficiency": 0.75,
                },
            },
            {
                "kind": "unit",
                "name": "pv",
                "unit_model_class": "GenericRenewables",
                "property_package": None,
                "construction_options": {
                    "capacity": {"value": 500.0, "units": "kW"},
                    "capacity_factor": [0.0] * 6 + [0.8] * 12 + [0.0] * 6,
                },
            },
            {
                "kind": "unit",
                "name": "battery",
                "unit_model_class": "BatteryModel",
                "property_package": None,
                "construction_options": {
                    "energy_capacity": {"value": 5000.0, "units": "kWh"},
                    "power_charge_max": {"value": 2000.0, "units": "kW"},
                    "power_discharge_max": {"value": 2000.0, "units": "kW"},
                    "eta_charge": 0.95,
                    "eta_discharge": 0.95,
                    "soc_min": 0.0,
                    "soc_max": 1.0,
                    "soh": 1.0,
                    "cyclic": True,
                },
            },
            {"kind": "fix", "variable": "pump.delta_pressure", "value": 300000.0},
            {"kind": "fix", "variable": "pump.inlet_state.pressure", "value": 100000.0},
            {
                "kind": "fix",
                "name": "load",
                "variable": "pump.inlet_state.flow_vol_phase[*,Liq]",
                "value": 30.0,
            },
            {"kind": "objective"},
        ],
    }


def design():
    """Two modeled years, two representative months, one day each."""
    return {
        "years": [{"year": 2030, "represents": 5}, {"year": 2035, "represents": 5}],
        "months": [
            {"month": 1, "weight": 6.0, "days": 1},
            {"month": 7, "weight": 6.0, "days": 1},
        ],
        "changes": [{"element": "load", "years": [2035], "values": {"value": 60.0}}],
        "capacities": [
            {
                "name": "battery_energy",
                "variable": BATTERY,
                "capital_cost": 300.0,
                "lifetime_years": 15,
                "upper": 1500.0,
            },
            {
                "name": "solar",
                "variable": "pv.capacity",
                "capital_cost": 1200.0,
                "lifetime_years": 25,
                "upper": 1500.0,
            },
        ],
    }


def spec():
    full = DesignStudy.model_validate(design()).model_dump(mode="json")
    return compose_design(flowsheet(), full, "study")


# Unit: the spec format, its files, and the hooks' dispatch.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ours", "theirs", "composed"),
    [
        (DesignStudy, upstream.DesignSpec, {"name", "base"}),
        (PlanningYear, upstream.PlanningYear, set()),
        (RepresentativeMonth, upstream.RepresentativeMonth, set()),
        (ParameterChange, upstream.ParameterChange, set()),
        (CapacityDecision, upstream.CapacitySpec, set()),
    ],
)
def test_config_models_mirror_flex_pse(ours, theirs, composed):
    assert set(ours.model_fields) | composed == set(theirs.model_fields)


@pytest.mark.unit
def test_format_and_files_of_a_design_spec():
    study = spec()
    costing = next(e for e in study["base"]["elements"] if e["kind"] == "costing")
    costing["tariff_source"] = "base.csv"
    study["changes"] += [
        {"element": "costing", "values": {"tariff_source": "later.csv"}},
        {"element": "pv", "values": {"capacity_factor": {"$source": "sun.json"}}},
        {"element": "load", "values": {"tariff_source": "not_a_tariff.csv"}},
    ]
    assert detect_format(study) == FLEXPSE_DESIGN
    index = study["base"]["elements"].index(costing)
    assert find_assets(study, FLEXPSE_DESIGN) == {
        f"/base/elements/{index}/tariff_source": "base.csv",
        "/changes/1/values/tariff_source": "later.csv",
        "/changes/2/values/capacity_factor/$source": "sun.json",
    }


@pytest.mark.unit
def test_configure_sets_a_capacity_or_a_base_option():
    study = spec()
    priced = configure(study, "capacities.solar.capital_cost", 900.0)
    assert priced["capacities"][1]["capital_cost"] == 900.0
    assert study["capacities"][1]["capital_cost"] == 1200.0
    bigger = configure(study, "pv.capacity.value", 800.0)
    pv = next(e for e in bigger["base"]["elements"] if e.get("name") == "pv")
    assert pv["construction_options"]["capacity"]["value"] == 800.0
    with pytest.raises(SpecError, match="Capacities: "):
        configure(study, "capacities.wind.capital_cost", 1.0)


@pytest.mark.unit
def test_apply_surrogate_edits_the_base():
    applied = apply_surrogate(spec(), "pump", LINEAR_POWER)
    assert applied["base"]["elements"][-1]["kind"] == "surrogate"
    assert "elements" not in applied


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "problem"),
    [({BATTERY: 10.0}, "sizes it as capacity"), ({"x": [1.0, 2.0]}, "per time point")],
)
def test_build_refuses_overrides_a_design_cant_honour(overrides, problem):
    with pytest.raises(SpecError, match=problem):
        build(spec(), **overrides)


@pytest.mark.unit
def test_a_bad_design_is_refused_when_stored():
    bad = {**spec(), "months": [{"month": 1, "weight": 5.0}]}
    with pytest.raises(SpecError, match="must sum to 12"):
        memory_orchestrator().store_model_spec("bad", bad)


@pytest.mark.unit
def test_a_design_cant_use_model_inputs():
    entry = {"dataset": "d", "columns": ["x"]}
    with pytest.raises(StudyConfigError, match="model_inputs"):
        load_config(
            {"base_model_spec": "b", "design": design(), "model_inputs": [entry]}
        )


@pytest.mark.unit
def test_a_study_without_design_is_unchanged():
    [task] = expand(load_config({"base_model_spec": "base"}))
    assert task.design is None and not task.derives and task.spec_tag == "base"
    assert detect_format(DEMO_SPEC).startswith("flex-pse.flowsheet/")


@pytest.mark.unit
def test_the_study_refuses_a_design_it_cant_run():
    orch = memory_orchestrator()
    orch.store_model_spec("plant", flowsheet())
    bad = {**design(), "capacities": []}
    with pytest.raises(StudyConfigError, match="capacities"):
        orch.execute_study({"base_model_spec": "plant", "design": bad})
    bad = {**design(), "months": [{"month": 1, "weight": 5.0}]}
    with pytest.raises(SpecError, match="must sum to 12"):
        orch.execute_study({"base_model_spec": "plant", "design": bad})
    orch.store_model_spec("sizing", spec())
    with pytest.raises(StudyConfigError, match="already a design study"):
        orch.execute_study({"base_model_spec": "sizing", "design": design()})


# Component: real solves.


@pytest.fixture
def memory_orch():
    orchestrator = memory_orchestrator(hooks=DEFAULT_HOOKS)
    orchestrator.store_model_spec("plant", flowsheet())
    return orchestrator


def study_config(**fields):
    return {"base_model_spec": "plant", "design": design(), **fields}


@pytest.mark.component
@pytest.mark.needs_highs
def test_stored_summary_matches_flexdesign(memory_orch):
    run = memory_orch.execute_study(study_config(output_name="sized"))
    assert run.counts() == {"succeeded": 1}, run.results

    model = build(spec())
    assert solve(model).ok
    expected = design_results(model)

    summary = memory_orch.design_results("sized")
    assert summary.study_years == expected.study_years == 10
    assert summary.annualized_cost == pytest.approx(expected.annualized_cost)
    assert summary.total_capital_cost == pytest.approx(expected.total_capital_cost)
    for name, capacity in expected.capacities.items():
        assert summary.capacities[name].size == pytest.approx(capacity.size)
        assert summary.capacities[name].at_upper_bound == capacity.at_upper_bound
    assert summary.annual_operating_cost_by_year == pytest.approx(
        expected.annual_operating_cost_by_year
    )
    assert summary.operating_cost_by_horizon == pytest.approx(
        expected.operating_cost_by_horizon
    )
    # The default stores the summary, not every horizon's every variable.
    assert not memory_orch.model_results("sized").timeseries()


@pytest.mark.component
@pytest.mark.needs_highs
def test_a_direct_design_spec_runs_like_a_design_config(memory_orch):
    memory_orch.store_model_spec("sizing", spec())
    assert memory_orch.model_spec("sizing").spec_format == FLEXPSE_DESIGN
    run = memory_orch.execute_study({"base_model_spec": "sizing"})
    assert run.counts() == {"succeeded": 1}, run.results
    assert memory_orch.design_results("sizing").capacities["solar"].size >= 0


@pytest.mark.component
@pytest.mark.needs_highs
def test_per_horizon_series_are_opt_in_with_their_own_timestamps(memory_orch):
    include = ["capacity*", "horizon[2030,*].battery.charge"]
    run = memory_orch.execute_study(
        study_config(output_name="o", export={"include": include})
    )
    assert run.counts() == {"succeeded": 1}, run.results
    data = memory_orch.model_results("o")
    assert {s.key for s in data.timeseries()} == {
        "horizon[2030,1].battery.charge",
        "horizon[2030,7].battery.charge",
    }
    for month in (1, 7):
        stamps = data[f"horizon[2030,{month}].battery.charge"].timestamps
        assert len(stamps) == 24
        assert stamps[0].year == 2030 and stamps[0].month == month
    with pytest.raises(ExportError, match="didn't store"):
        memory_orch.design_results("o")


@pytest.mark.component
@pytest.mark.needs_highs
def test_sweep_resume_and_files_in_changes(memory_orch, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sun.json").write_text(json.dumps([0.0] * 6 + [0.5] * 12 + [0.0] * 6))
    study = design()
    study["changes"].append(
        {
            "element": "pv",
            "months": [7],
            "values": {
                "construction_options": {"capacity_factor": {"$source": "sun.json"}}
            },
        }
    )
    config = study_config(
        design=study,
        sweeps=[
            {
                "type": "configuration_option",
                "name": "cost",
                "path": "capacities.solar.capital_cost",
                "values": [600.0, 2400.0],
            }
        ],
        output_name="solar_{cost:.0f}",
        on_existing="skip",
    )
    assert memory_orch.execute_study(config).counts() == {"succeeded": 2}
    cheap = memory_orch.design_results("solar_600").capacities["solar"].size
    dear = memory_orch.design_results("solar_2400").capacities["solar"].size
    assert cheap > dear

    record = memory_orch.model_spec("solar_600")
    assert record.parent_tag == "plant"
    assert [ref.name for ref in record.assets.values()][0].startswith("assets/")

    # The stored spec holds the file, so the study resumes and rebuilds without it.
    (tmp_path / "sun.json").unlink()
    assert memory_orch.execute_study(config).counts() == {"skipped": 2}
    assert solve(memory_orch.build("solar_600")).ok


@pytest.mark.component
@pytest.mark.needs_highs
def test_rerun_of_a_failed_task_reuses_its_stored_spec(
    memory_orch, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sun.json").write_text(json.dumps([0.5] * 24))
    study = design()
    study["changes"].append(
        {
            "element": "pv",
            "values": {
                "construction_options": {"capacity_factor": {"$source": "sun.json"}}
            },
        }
    )
    failing = {"base_model_spec": "plant", "design": study, "output_name": "again"}
    hooks = {"solve": "flex_orchestrator.testing:fake_solve"}
    failed = memory_orch.execute_study(
        {**failing, "hooks": hooks, "solve_options": {"status": "failed"}}
    )
    assert failed.counts() == {"failed": 1}, failed.results
    assert memory_orch.execute_study(failing).counts() == {"succeeded": 1}


@pytest.mark.component
@pytest.mark.needs_highs
def test_design_study_runs_on_ray(memory_orch):
    pytest.importorskip("ray")
    run = memory_orch.execute_study(
        study_config(
            sweeps=[
                {
                    "type": "configuration_option",
                    "name": "cost",
                    "path": "capacities.solar.capital_cost",
                    "values": [600.0, 2400.0],
                }
            ],
            output_name="ray_{cost:.0f}",
            execution={"max_workers": 2},
        )
    )
    assert run.counts() == {"succeeded": 2}, run.results
    assert memory_orch.design_results("ray_600").annualized_cost > 0


@pytest.mark.component
@pytest.mark.needs_highs
def test_archive_round_trip_keeps_a_design_study(memory_orch, tmp_path):
    assert memory_orch.execute_study(study_config(output_name="kept")).counts() == {
        "succeeded": 1
    }
    expected = memory_orch.design_results("kept")
    folder = tmp_path / "archive"
    export_study(memory_orch, folder)
    other = memory_orchestrator(study="restored", hooks=DEFAULT_HOOKS)
    import_study(other, folder)
    assert other.design_results("kept") == expected


@pytest.mark.component
@pytest.mark.needs_highs
def test_exporting_a_merged_model():
    model = build(spec())
    assert solve(model).ok
    assert len(time_sets(model)) == 4
    with pytest.raises(ExportError, match="several time blocks"):
        export_model_data(model, include=["capacity*"])
    with pytest.raises(ExportError, match="several time blocks"):
        export_model_data(
            model,
            include=["capacity*"],
            time_sets=time_sets(model),
            datetime_index=[0],
        )
    data = export_model_data(
        model, include=["horizon[2035,7].battery.charge"], time_sets=time_sets(model)
    )
    assert data["horizon[2035,7].battery.charge"].timestamps[0].year == 2035
    with pytest.raises(SpecError, match="merged design model"):
        apply_values(model, {"x": 1.0})


@pytest.mark.component
@pytest.mark.needs_highs
@pytest.mark.on_acquirium
def test_design_study_end_to_end_on_every_backend(orch):
    orch.store_model_spec("plant", flowsheet())
    include = ["capacity*", "annual*", "horizon*.costing.aggregate_operating_cost"]
    include.append("horizon[2035,*].battery.charge")
    run = orch.execute_study(
        study_config(output_name="e2e", export={"include": include})
    )
    assert run.counts() == {"succeeded": 1}, run.results
    summary = orch.design_results("e2e")
    assert summary.annualized_cost == pytest.approx(
        summary.annualized_operating_cost + summary.annualized_capital_cost
    )
    charge = orch.model_results("e2e")["horizon[2035,7].battery.charge"]
    assert charge.timestamps[0].year == 2035 and charge.timestamps[0].month == 7
