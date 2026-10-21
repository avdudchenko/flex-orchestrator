"""The flex-pse default hooks: build, overrides, surrogates and solve."""

import copy

import pytest

pytest.importorskip("flexops")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyomo.environ as pyo  # noqa: E402
from flexcore.config.schema import SurrogateSpec, SurrogateType  # noqa: E402
from flexcore.exceptions import FlexConfigError  # noqa: E402
from flexparameterize.regression import get_regressor  # noqa: E402

from flex_orchestrator.errors import SpecError  # noqa: E402
from flex_orchestrator.flexpse import (  # noqa: E402
    DEFAULT_RELATION,
    apply_surrogate,
    build,
    solve,
    swap_surrogate,
    time_sets,
    with_overrides,
)
from flex_orchestrator.hooks import DEFAULT_HOOKS, HookRegistry, run_solve  # noqa: E402
from flex_orchestrator.specs import SpecRecord  # noqa: E402
from flex_orchestrator.testing import DEMO_SPEC, LINEAR_POWER  # noqa: E402

FLOW_10 = {"demo.pump.flow_in": 10.0}


def surrogates(spec):
    return [e for e in spec["elements"] if e["kind"] == "surrogate"]


def first_t(model):
    return model.time_block.time_index.first()


@pytest.mark.unit
def test_default_hooks_resolve_to_flexpse_functions():
    resolved = HookRegistry().resolve(DEFAULT_HOOKS)
    assert resolved.build is build
    assert resolved.solve is solve
    assert resolved.apply_surrogate is apply_surrogate
    assert resolved.swap_surrogate is swap_surrogate
    assert resolved.time_sets is time_sets


@pytest.mark.unit
def test_build_expands_arcs():
    model = build(DEMO_SPEC)
    assert not model.demo.arc_0.active
    assert model.demo.find_component("arc_0_expanded") is not None


# Overrides


@pytest.mark.unit
def test_with_overrides_adds_named_fix_elements_without_mutating():
    original = copy.deepcopy(DEMO_SPEC)
    spec = with_overrides(
        DEMO_SPEC,
        {
            "demo.pump.flow_in": 10.0,
            "demo.tank.flow_out": {"value": 1, "units": "m^3/s"},
        },
    )
    assert DEMO_SPEC == original
    assert spec["elements"][-2:] == [
        {
            "kind": "fix",
            "name": "override:demo.pump.flow_in",
            "variable": "demo.pump.flow_in",
            "value": 10.0,
        },
        {
            "kind": "fix",
            "name": "override:demo.tank.flow_out",
            "variable": "demo.tank.flow_out",
            "value": 1,
            "units": "m^3/s",
        },
    ]


@pytest.mark.unit
def test_build_fixes_overrides_at_every_time_point():
    model = build(DEMO_SPEC, **FLOW_10)
    flow = model.demo.pump.flow_in
    assert all(flow[t].fixed and pyo.value(flow[t]) == 10.0 for t in flow)


@pytest.mark.unit
def test_build_override_with_units_is_converted():
    model = build(
        DEMO_SPEC, **{"demo.pump.flow_in[0]": {"value": 1.0, "units": "m^3/min"}}
    )
    assert pyo.value(model.demo.pump.flow_in[0]) == pytest.approx(60.0)
    assert not model.demo.pump.flow_in[1].fixed


@pytest.mark.unit
def test_build_rejects_unknown_override():
    with pytest.raises(FlexConfigError, match="demo.pump.nope"):
        build(DEMO_SPEC, **{"demo.pump.nope": 1.0})


# Surrogates


@pytest.mark.unit
def test_apply_surrogate_adds_element_without_mutating_input():
    original = copy.deepcopy(DEMO_SPEC)
    spec = apply_surrogate(DEMO_SPEC, "demo.pump", LINEAR_POWER)
    assert DEMO_SPEC == original
    [element] = surrogates(spec)
    assert element["unit"] == "demo.pump"
    assert element["relation"] == DEFAULT_RELATION == "power_electrical_relation"
    model = build(spec)
    assert model.demo.pump.find_component("surrogate_power_electrical") is not None
    assert not model.demo.pump.power_electrical_relation.active


@pytest.mark.unit
def test_apply_surrogate_replaces_same_unit_and_relation_only():
    spec = apply_surrogate(DEMO_SPEC, "demo.pump", LINEAR_POWER)
    other = copy.deepcopy(LINEAR_POWER)
    other["data"]["coefficients"]["flow_in"] = 0.7
    spec = apply_surrogate(spec, "demo.pump", other)
    spec = apply_surrogate(spec, "demo.pump", LINEAR_POWER, relation="other_relation")
    assert [
        (e["relation"], e["data"]["coefficients"]["flow_in"]) for e in surrogates(spec)
    ] == [
        ("power_electrical_relation", 0.7),
        ("other_relation", 0.6),
    ]


@pytest.mark.unit
def test_apply_surrogate_unknown_unit_lists_units():
    with pytest.raises(SpecError, match=r"Units: \['demo.battery', 'demo.pump'"):
        apply_surrogate(DEMO_SPEC, "demo.digester", LINEAR_POWER)


@pytest.mark.unit
def test_apply_surrogate_invalid_spec():
    with pytest.raises(SpecError, match="Invalid surrogate spec"):
        apply_surrogate(DEMO_SPEC, "demo.pump", {"surrogate_type": "nope", "data": {}})


@pytest.mark.unit
def test_swap_surrogate_on_live_model():
    model = build(DEMO_SPEC)
    swap_surrogate(model, "demo.pump", LINEAR_POWER)
    assert model.demo.pump.find_component("surrogate_power_electrical") is not None
    assert not model.demo.pump.power_electrical_relation.active


@pytest.mark.unit
def test_swap_surrogate_unknown_unit():
    model = build(DEMO_SPEC)
    with pytest.raises(FlexConfigError):
        swap_surrogate(model, "demo.nope", LINEAR_POWER)


@pytest.mark.unit
def test_swap_surrogate_invalid_spec():
    model = build(DEMO_SPEC)
    with pytest.raises(SpecError, match="Invalid surrogate spec"):
        swap_surrogate(model, "demo.pump", {"data": {}})


# Solving. get_solver classifies the demo as NLP, so it routes to IPOPT.


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_solve_demo_is_optimal():
    outcome = run_solve(solve, build(DEMO_SPEC), prefer="ipopt")
    assert outcome.ok, outcome
    assert outcome.termination == "optimal"
    assert outcome.solver == "ipopt"
    assert outcome.wall_time > 0


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_solve_uses_spec_solver(caplog):
    spec = {**DEMO_SPEC, "solver": "not_a_solver"}
    outcome = run_solve(solve, build(spec))
    assert outcome.ok
    assert "not_a_solver" in caplog.text


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_surrogate_changes_the_solution():
    base = build(DEMO_SPEC, **FLOW_10)
    swapped = build(apply_surrogate(DEMO_SPEC, "demo.pump", LINEAR_POWER), **FLOW_10)
    assert run_solve(solve, base).ok
    assert run_solve(solve, swapped).ok
    assert pyo.value(base.demo.pump.power_electrical[first_t(base)]) == pytest.approx(
        5.0
    )
    assert pyo.value(
        swapped.demo.pump.power_electrical[first_t(swapped)]
    ) == pytest.approx(6.0)


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_infeasible_model_is_failed_not_error():
    model = build(DEMO_SPEC)
    model.x = pyo.Var()
    model.y = pyo.Var()
    model.lower = pyo.Constraint(expr=model.x + model.y >= 2)
    model.upper = pyo.Constraint(expr=model.x + model.y <= 1)
    outcome = run_solve(solve, model, load_solutions=False)
    assert outcome.status == "failed", outcome
    assert outcome.termination != "optimal"


# Surrogate cases: one per surrogate type the pinned flex-pse implements end to
# end (PLAN.md §7, surrogate coverage rule). Each fits the type's own regressor
# on synthetic pump data with known coefficients.

TIME_INDEX = pd.date_range("2025-01-01", periods=48, freq="h")
FLOW = pd.DataFrame({"flow_in": np.linspace(1.0, 20.0, 48)}, index=TIME_INDEX)


def fit_multilinear():
    power = pd.DataFrame({"power_electrical": 0.6 * FLOW["flow_in"] + 2.0})
    regressor = get_regressor("multilinear")().fit(
        FLOW, power, input_units={"flow_in": "m^3/hr"}, output_units="kW"
    )
    return regressor.to_surrogate_spec()


def fit_constant_intensity():
    power = pd.DataFrame({"power_electrical": 0.7 * FLOW["flow_in"]})
    regressor = get_regressor("constant_intensity")().fit(FLOW, power)
    return regressor.to_surrogate_spec(input_units="m^3/hr", output_units="kW")


SURROGATE_CASES = {
    # surrogate type: (fit, expected coefficients, power at 10 m^3/hr in kW)
    "multilinear": (fit_multilinear, {"flow_in": 0.6, "intercept": 2.0}, 8.0),
    "constant_intensity": (fit_constant_intensity, {"energy_intensity": 0.7}, 7.0),
}

RESERVED_TYPES = ["quadratic", "exponential", "arima", "neural_network"]


@pytest.fixture(params=sorted(SURROGATE_CASES))
def surrogate_case(request):
    fit, coefficients, power_at_10 = SURROGATE_CASES[request.param]
    return request.param, fit().model_dump(mode="json"), coefficients, power_at_10


@pytest.mark.unit
def test_cases_cover_every_implemented_type():
    implemented = {t.value for t in SurrogateType} - set(RESERVED_TYPES)
    assert implemented == set(SURROGATE_CASES)


@pytest.mark.unit
def test_regressor_recovers_known_coefficients(surrogate_case):
    surrogate_type, spec, coefficients, _ = surrogate_case
    assert spec["surrogate_type"] == surrogate_type
    assert spec["data"]["coefficients"] == pytest.approx(coefficients)


@pytest.mark.unit
def test_fitted_spec_applies_and_builds(surrogate_case):
    surrogate_type, spec, _, _ = surrogate_case
    new_spec = apply_surrogate(DEMO_SPEC, "demo.pump", spec)
    assert surrogates(new_spec)[0]["surrogate_type"] == surrogate_type
    build(new_spec)


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_fitted_surrogate_solves_to_expected_power(surrogate_case):
    _, spec, _, power_at_10 = surrogate_case
    model = build(apply_surrogate(DEMO_SPEC, "demo.pump", spec), **FLOW_10)
    assert run_solve(solve, model).ok
    power = model.demo.pump.power_electrical
    assert all(pyo.value(power[t]) == pytest.approx(power_at_10) for t in power)


@pytest.mark.unit
@pytest.mark.parametrize("surrogate_type", RESERVED_TYPES)
def test_reserved_types_still_unimplemented(surrogate_type):
    # Tripwire: when a pin bump implements one of these, move it into
    # SURROGATE_CASES (with its regressor and data) and the docs.
    with pytest.raises(NotImplementedError):
        get_regressor(surrogate_type)
    with pytest.raises(NotImplementedError):
        build(
            apply_surrogate(
                DEMO_SPEC, "demo.pump", {"surrogate_type": surrogate_type, "data": {}}
            )
        )


# Specs as data (D2): validation of flex-pse specs, and replay from a record.


@pytest.mark.unit
def test_model_spec_record_validates_with_flexpse():
    record = SpecRecord.create("model_spec", "base", DEMO_SPEC)
    assert record.spec_format == "flex-pse.flowsheet/0.1.0"
    bad = {
        **DEMO_SPEC,
        "elements": [e for e in DEMO_SPEC["elements"] if e["kind"] != "time"],
    }
    with pytest.raises(SpecError, match="Invalid flex-pse flowsheet spec"):
        SpecRecord.create("model_spec", "bad", bad)


@pytest.mark.unit
def test_surrogate_spec_record_validates_with_flexpse(surrogate_case):
    _, spec, _, _ = surrogate_case
    record = SpecRecord.create("surrogate_spec", "fit", spec)
    assert record.spec_format == "flex-pse.surrogate"
    with pytest.raises(SpecError, match="Invalid flex-pse surrogate spec"):
        SpecRecord.create("surrogate_spec", "bad", {**spec, "surrogate_type": "nope"})


@pytest.mark.unit
def test_dict_and_pydantic_surrogate_store_the_same_spec():
    from_dict = SpecRecord.create("surrogate_spec", "s", LINEAR_POWER)
    from_model = SpecRecord.create(
        "surrogate_spec", "s", SurrogateSpec.model_validate(LINEAR_POWER)
    )
    assert from_dict.spec == from_model.spec
    assert from_dict.spec_hash == from_model.spec_hash
    assert from_dict.spec["provenance"] == {}


@pytest.mark.unit
def test_flowsheet_normalisation_is_idempotent():
    record = SpecRecord.create("model_spec", "base", DEMO_SPEC)
    assert record.spec != DEMO_SPEC  # defaults written out
    again = SpecRecord.create("model_spec", "base", record.spec)
    assert again.spec == record.spec
    assert again.spec_hash == record.spec_hash


@pytest.mark.unit
def test_stored_spec_replays_without_the_live_model():
    derived = apply_surrogate(DEMO_SPEC, "demo.pump", LINEAR_POWER)
    record = SpecRecord.create("model_spec", "with_fit", derived, parent_tag="base")
    stored = SpecRecord.model_validate_json(record.model_dump_json())
    stored.verify()
    model = build(stored.spec)
    assert model.demo.pump.find_component("surrogate_power_electrical") is not None
