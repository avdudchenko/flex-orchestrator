"""The model data exporter on a solved flex-pse demo."""

import pytest

from flex_orchestrator.export import UnitMapper, export_model_data


@pytest.mark.component
@pytest.mark.needs_ipopt
def test_demo_after_solve_has_values():
    from flex_orchestrator.flexpse import build, solve
    from flex_orchestrator.hooks import run_solve
    from flex_orchestrator.testing import DEMO_SPEC

    model = build(DEMO_SPEC, **{"demo.pump.flow_in": 10.0})
    assert run_solve(solve, model).ok
    data = export_model_data(
        model, include=["demo.pump.*", "costing.aggregate_*"], unit_mapper=UnitMapper()
    )
    power = data["demo.pump.power_electrical"]
    assert power.values == pytest.approx([5.0] * len(power.values))
    assert data["demo.pump.energy_intensity"].value == pytest.approx(0.5)
    assert not data.stale()
