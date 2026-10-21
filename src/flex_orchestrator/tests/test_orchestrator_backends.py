"""Results round-trip on every backend: export -> store -> read back (PLAN.md §7).

Each test runs on the memory backend (unit tier) and a local Acquirium
(component tier).
"""

from datetime import UTC

import pytest

from flex_orchestrator.errors import TagExistsError
from flex_orchestrator.export import export_model_data
from flex_orchestrator.hooks import SolveOutcome

SELECTION = ["costing.aggregate*", "costing.annualized_cost", "demo.pump.*"]
OK = SolveOutcome(status="ok", termination="optimal")


def test_round_trip(orch, demo_model):
    exported = export_model_data(demo_model, include=SELECTION)
    stored = orch.store_model_results("base", demo_model, OK, include=SELECTION)
    back = orch.model_results("base")
    assert back.keys() == stored.keys == exported.keys()
    for before in exported:
        after = back[before.key]
        assert (after.kind, after.component, after.index, after.unit_pyomo) == (
            before.kind,
            before.component,
            before.index,
            before.unit_pyomo,
        )
        assert after.values == pytest.approx(before.values, nan_ok=False)
        if before.kind == "timeseries":
            assert after.timestamps == [
                t.to_pydatetime().replace(tzinfo=UTC) for t in before.timestamps
            ]
    with pytest.raises(TagExistsError):
        orch.store_model_results("base", demo_model, include=SELECTION)


def test_stale_values_round_trip_as_none(orch, demo_model):
    orch.store_model_results("base", demo_model, keys=["demo.battery.soc"])
    soc = orch.model_results("base")["demo.battery.soc"]
    assert soc.values == [None] * len(soc.values)
    assert soc.stale


def test_a_second_result_reuses_the_declarations(orch, demo_model):
    orch.store_model_results("a", demo_model, include=SELECTION)
    demo_model.demo.pump.energy_intensity.set_value(0.75)
    orch.store_model_results("b", demo_model, include=SELECTION)
    assert orch.model_results("a")["demo.pump.energy_intensity"].value == 0.5
    assert orch.model_results("b")["demo.pump.energy_intensity"].value == 0.75


# Acquirium only


@pytest.mark.component
def test_acquirium_verifies_units_and_declares_only_known_ones(
    acquirium_local, study_name, demo_model
):
    from flex_orchestrator.backend import AcquiriumBackend
    from flex_orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(AcquiriumBackend(acquirium_local))
    orch.create_study(study_name)
    orch.store_model_results("base", demo_model, include=SELECTION)
    back = orch.model_results("base")
    power = back["costing.aggregate_electrical_power"]
    assert (power.unit_qudt, power.unit_verified) == ("KiloW", True)
    assert back["demo.pump.flow_in"].unit_qudt == "M3-PER-HR"
    # QUDT has no USD/a: stored with its Pyomo unit only (PLAN.md U9).
    annual = back["costing.annualized_cost"]
    assert (annual.unit_qudt, annual.unit_pyomo) == (None, "USD/a")


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_solve_and_store_on_acquirium(acquirium_local, study_name, demo_model):
    from flex_orchestrator.backend import AcquiriumBackend
    from flex_orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(AcquiriumBackend(acquirium_local))
    orch.create_study(study_name)
    stored = orch.solve_and_store("solved", demo_model, include=["demo.battery.*"])
    assert stored.ok and stored.outcome.termination == "optimal"
    soc = orch.model_results("solved")["demo.battery.soc"]
    assert not soc.stale
    solved = export_model_data(demo_model, keys=["demo.battery.soc"])
    assert soc.values == pytest.approx(solved["demo.battery.soc"].values)
