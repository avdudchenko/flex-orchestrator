"""Model inputs in studies and builds (PLAN.md D21, M5.6).

On memory and files: a ``model_inputs`` section, a ``model_inputs`` sweep and
a case, what each result records, the clash with a ``variable`` sweep, export
and import, and ``orch.build``. A tiny Pyomo model stands in for flex-pse; its
``time_grid`` hook reads the spec's ``start`` and ``steps``.
"""

import numpy as np
import pandas as pd
import pyomo.environ as pyo
import pytest

from flex_orchestrator.archive import load_result
from flex_orchestrator.errors import ModelInputError, StudyConfigError
from flex_orchestrator.hooks import HookSet
from flex_orchestrator.orchestrator import Orchestrator
from flex_orchestrator.study import StudyConfig, expand
from flex_orchestrator.testing import fake_solve


def tiny_build(spec, **overrides):
    m = pyo.ConcreteModel()
    m.inputs = pyo.Var(initialize=len(overrides))
    return m


def hourly_grid(spec):
    return pd.date_range(spec["start"], periods=spec["steps"], freq="h")


HOOKS = HookSet(build=tiny_build, solve=fake_solve, time_grid=hourly_grid)
DEMAND = {"dataset": "ops", "columns": ["demand"], "rename": {"demand": "plant.d"}}
LINES = {
    "type": "model_inputs",
    "name": "line",
    "values": [
        {
            "label": "a",
            "inputs": [
                {
                    "dataset": "ops",
                    "columns": ["feed_a"],
                    "rename": {"feed_a": "plant.f"},
                }
            ],
        },
        {
            "label": "b",
            "inputs": [
                {
                    "dataset": "ops",
                    "columns": ["feed_b"],
                    "rename": {"feed_b": "plant.f"},
                },
                {**DEMAND, "reduce": "first"},  # replaces the study's demand
            ],
        },
    ],
}


@pytest.fixture
def orch(orch_backend, study_name):
    orch = Orchestrator(orch_backend, hooks=HOOKS, environ={})
    orch.create_study(study_name)
    hours = pd.date_range("2025-07-07", periods=72, freq="h", tz="UTC")
    orch.store_data(
        "ops",
        pd.DataFrame(
            {
                "demand": np.arange(72.0),
                "feed_a": np.full(72, 1.0),
                "feed_b": np.full(72, 2.0),
            },
            hours,
        ),
        units={"demand": "m^3/hr"},
    )
    orch.store_model_spec("base", {"start": "2025-07-08", "steps": 3})
    return orch


def study(**fields):
    return {
        "base_model_spec": "base",
        "model_inputs": [DEMAND],
        "sweeps": [LINES],
        "output_name": "line_{line}",
        **fields,
    }


def test_a_study_sets_variables_from_stored_data(orch):
    run = orch.execute_study(study())
    assert run.counts() == {"succeeded": 2}
    a, b = orch.result("line_a"), orch.result("line_b")
    assert a.overrides == {
        "plant.d": {"value": [24.0, 25.0, 26.0], "units": "m^3/hr"},
        "plant.f": [1.0, 1.0, 1.0],
    }
    assert b.overrides["plant.d"] == {"value": 24.0, "units": "m^3/hr"}
    assert b.overrides["plant.f"] == [2.0, 2.0, 2.0]
    assert [m["paths"] for m in a.model_inputs] == [["plant.d"], ["plant.f"]]
    # b's own demand entry replaced the study's, so only it is recorded.
    assert [m["paths"] for m in b.model_inputs] == [["plant.f"], ["plant.d"]]
    assert b.model_inputs[1]["input"]["reduce"] == "first"
    data_run = a.model_inputs[0]["resolved"]["run_id"]
    assert data_run == orch._latest("ops", "data").run_id


def test_cases_set_model_inputs_too(orch):
    case = {
        "name": "feed_b",
        "model_inputs": [
            {"dataset": "ops", "columns": ["feed_b"], "rename": {"feed_b": "plant.f"}}
        ],
    }
    run = orch.execute_study(
        study(sweeps=[{"type": "cases", "cases": [case]}], output_name="{case}")
    )
    assert run.counts() == {"succeeded": 1}
    assert orch.result("feed_b").overrides["plant.f"] == [2.0, 2.0, 2.0]


def test_a_variable_set_both_ways_is_an_error(orch):
    sweep = {"type": "variable", "path": "plant.d", "values": [1.0]}
    run = orch.execute_study(study(sweeps=[sweep], output_name="v"))
    [result] = run.results
    assert result.status == "error"
    assert "set both by model_inputs and by a variable" in result.error


def test_a_failed_input_is_an_error_result(orch):
    run = orch.execute_study(
        study(model_inputs=[{**DEMAND, "offset": "3650D"}], sweeps=[], output_name="x")
    )
    [result] = run.results
    assert result.status == "error" and "ModelInputError" in result.error


def test_the_plan_carries_the_entries(orch):
    plan = orch.execute_study(study(), dry_run=True)
    [a, b] = plan.tasks
    assert a.labels == {"line": "a"} and a.model_inputs[0].dataset == "ops"
    assert [i.columns for i in b.swept_inputs] == [["feed_b"], ["demand"]]
    assert orch.stored_model_results() == []


def test_export_and_import_keep_where_inputs_came_from(orch, tmp_path):
    orch.execute_study(study())
    folder = orch.export_study(tmp_path / "e").path
    loaded = load_result(folder / "results" / "line_a.json")
    assert loaded.model_inputs == orch.result("line_a").model_inputs
    assert loaded.overrides == orch.result("line_a").overrides
    orch.import_study(folder, name=orch.study + "-copy")
    assert orch.result("line_a").model_inputs == loaded.model_inputs


def test_a_build_takes_model_inputs(orch):
    model = orch.build("base", model_inputs=[DEMAND], **{"plant.x": 1.0})
    assert pyo.value(model.inputs) == 2
    stored = orch.store_model_results("built", model, fake_solve(model))
    result = orch.result("built")
    assert stored.ok and result.overrides["plant.x"] == 1.0
    assert result.overrides["plant.d"]["value"] == [24.0, 25.0, 26.0]
    assert result.model_inputs[0]["paths"] == ["plant.d"]
    with pytest.raises(ModelInputError, match="set both"):
        orch.build("base", model_inputs=[DEMAND], **{"plant.d": 1.0})
    found = orch.resolve_model_inputs("base", [{**DEMAND, "reduce": "mean"}])
    assert found.values == {"plant.d": {"value": 25.0, "units": "m^3/hr"}}


@pytest.mark.unit
def test_sweep_labels_name_tasks_and_must_differ():
    tasks = expand(StudyConfig.model_validate({**study(), "output_name": None}))
    assert [t.tag for t in tasks] == ["base_a", "base_b"]
    repeated = {**LINES, "values": [LINES["values"][0]] * 2}
    with pytest.raises(StudyConfigError, match="labels must differ"):
        from flex_orchestrator.study import load_config

        load_config({"base_model_spec": "base", "sweeps": [repeated]})
    schema = StudyConfig.model_json_schema()
    assert "model_inputs" in schema["properties"]
    assert "ModelInputsSweep" in schema["$defs"]
