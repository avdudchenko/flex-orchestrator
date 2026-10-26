"""The plugin's own tests: no database, no solver (flex_orchestrator.testing)."""

import pytest
from ro_tools import ROOrchestrator, cost_table, plugin

from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.testing import memory_orchestrator, run_command_in_test

pytestmark = pytest.mark.unit


def cost(value):
    series = ExportedSeries(
        key="costing.aggregate_operating_cost",
        kind="scalar",
        component="costing.aggregate_operating_cost",
        index=[],
        values=[value],
        timestamps=None,
        unit_pyomo="USD",
        unit_qudt=None,
    )
    return ModelData([series])


def test_cost_table_lists_each_result():
    orch = memory_orchestrator(plugins=[plugin])
    assert isinstance(orch, ROOrchestrator)
    orch.store_model_data("a", cost(1.5))
    orch.store_model_data("b", cost(2.5))
    assert run_command_in_test("cost_table", orch=orch) == {"a": 1.5, "b": 2.5}
    assert run_command_in_test(cost_table, orch=orch, tags=["b"]) == {"b": 2.5}


def test_the_subclass_stamps_and_refuses():
    orch = memory_orchestrator(plugins=[plugin])
    orch.store_model_data("a", cost(1.0))
    assert orch.result("a").metadata["ro_tools_version"] == "0.1.0"
    with pytest.raises(ValueError, match="scratch"):
        orch.store_model_data("scratch_1", cost(1.0))
    assert orch.default_export_selection()["include"][0] == "plant.*"
