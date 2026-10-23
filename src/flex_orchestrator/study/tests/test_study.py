"""StudyConfig, expansion into tasks, and execute_study (PLAN.md M4).

These run at the unit tier: a tiny Pyomo build hook and the fake solve need no
solver. ``test_study_flexpse.py`` runs the fit-then-sweep exit with flex-pse.
"""

import dataclasses
import json

import pyomo.environ as pyo
import pytest

from flex_orchestrator.errors import HookError, StudyConfigError, TagExistsError
from flex_orchestrator.hooks import HookSet
from flex_orchestrator.orchestrator import Orchestrator
from flex_orchestrator.study import StudyConfig, expand, load_config, run_task
from flex_orchestrator.study.task import TaskSpec
from flex_orchestrator.testing import fake_solve, memory_orchestrator

pytestmark = pytest.mark.unit


def tiny_build(spec, **overrides):
    """A Pyomo model whose values record the spec, surrogates and overrides."""
    if spec.get("explode"):
        raise RuntimeError("build failed")
    m = pyo.ConcreteModel()
    m.base = pyo.Var(initialize=spec["value"])
    m.surrogates = pyo.Var(initialize=len(spec.get("surrogates", {})))
    m.override = pyo.Var(initialize=sum(float(v) for v in overrides.values()))
    m.options = pyo.Var(initialize=sum(spec.get("options", {}).values()))
    return m


def fake_apply(spec, unit, surrogate, relation=None):
    return {**spec, "surrogates": {**spec.get("surrogates", {}), unit: surrogate}}


def fake_configure(spec, path, value):
    return {**spec, "options": {**spec.get("options", {}), path: value}}


HOOKS = HookSet(
    build=tiny_build,
    solve=fake_solve,
    apply_surrogate=fake_apply,
    configure=fake_configure,
)


@pytest.fixture
def orch():
    orch = memory_orchestrator(study="s", hooks=HOOKS)
    orch.store_model_spec("base", {"value": 1.0})
    orch.store_model_spec("other", {"value": 2.0})
    orch.store_surrogate_spec("sa", {"kind": "a"})
    orch.store_surrogate_spec("sb", {"kind": "b"})
    return orch


def config(**fields):
    return {
        "base_model_spec": "base",
        "sweeps": [
            {"type": "surrogate", "unit": "u", "surrogates": ["sa", "sb"]},
            {"type": "variable", "path": "p.x", "values": [1, 2]},
        ],
        "output_name": "{surrogate}_{p.x}",
        **fields,
    }


# Config


def test_load_config_from_a_mapping_a_model_or_a_file(tmp_path):
    cfg = load_config(config())
    assert load_config(cfg) is cfg
    (tmp_path / "s.json").write_text(json.dumps(config()))
    assert load_config(tmp_path / "s.json") == cfg
    (tmp_path / "s.yaml").write_text(
        "base_model_spec: base\nsweeps:\n  - {type: spec_list, specs: [a]}\n"
    )
    assert load_config(str(tmp_path / "s.yaml")).sweeps[0].specs == ["a"]


@pytest.mark.parametrize(
    ("config_", "message"),
    [
        ({"sweeps": [{"type": "nope"}]}, "sweeps.0"),
        ({"execution": {"retries": -1}}, "execution.retries"),
        ({"surprise": 1}, "surprise: Extra inputs"),
        (["not", "a", "mapping"], "not list"),
    ],
)
def test_invalid_configs(config_, message):
    with pytest.raises(StudyConfigError, match=message):
        load_config(config_)


def test_ray_tasks_time_out_after_two_hours_unless_set():
    assert load_config(config()).execution.task_timeout_s == 7200
    no_limit = load_config(config(execution={"task_timeout_s": None}))
    assert no_limit.execution.task_timeout_s is None


def test_unreadable_config_files(tmp_path):
    with pytest.raises(StudyConfigError, match="Can't read"):
        load_config(tmp_path / "missing.yaml")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(StudyConfigError, match="Can't parse"):
        load_config(tmp_path / "bad.json")


def test_the_json_schema_names_the_sweeps():
    schema = StudyConfig.model_json_schema()
    assert {
        "SurrogateSweep",
        "VariableSweep",
        "ConfigurationSweep",
        "SpecListSweep",
        "CasesSweep",
        "Case",
    } <= set(schema["$defs"])


# Expansion


def test_product_expansion():
    tasks = expand(load_config(config()))
    assert [t.tag for t in tasks] == ["sa_1", "sa_2", "sb_1", "sb_2"]
    first = tasks[0]
    assert first.base == "base" and first.spec_tag == "sa_1"
    assert first.surrogates[0].surrogate == "sa"
    assert first.overrides == {"p.x": 1}
    assert first.labels == {"surrogate": "sa", "p.x": 1}


def test_zip_expansion_and_its_length_check():
    cfg = load_config(config(combine="zip"))
    assert [t.tag for t in expand(cfg)] == ["sa_1", "sb_2"]
    cfg.sweeps[1].values.append(3)
    with pytest.raises(StudyConfigError, match="one length"):
        expand(cfg)


def test_spec_lists_and_default_names():
    cfg = load_config(
        {
            "sweeps": [
                {"type": "spec_list", "specs": ["base", "other"]},
                {
                    "type": "variable",
                    "path": "p.x",
                    "values": [{"value": 0.5, "units": "kW"}],
                },
            ]
        }
    )
    tasks = expand(cfg)
    assert [t.tag for t in tasks] == ["base_0.5", "other_0.5"]
    assert [t.spec_tag for t in tasks] == ["base", "other"]
    assert [t.tag for t in expand(load_config({"base_model_spec": "b"}))] == ["b"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"base_model_spec": None, "sweeps": []}, "no base_model_spec"),
        ({"output_name": "{nope}"}, "names \\{nope\\}"),
        ({"output_name": "bad tag {p.x}"}, "invalid tag"),
        ({"output_name": "{surrogate}"}, "Two tasks would be stored as 'sa'"),
        (
            {
                "sweeps": [
                    {"type": "surrogate", "unit": "u", "surrogates": ["sa"]},
                    {"type": "surrogate", "unit": "v", "surrogates": ["sb"]},
                ]
            },
            "give them names",
        ),
    ],
)
def test_expansion_errors(fields, message):
    with pytest.raises(StudyConfigError, match=message):
        expand(load_config(config(**fields)))


# Running


def results(orch):
    return {r.tag: r for r in orch.stored_model_results()}


def test_a_dry_run_stores_nothing(orch):
    run = orch.execute_study(config(), dry_run=True)
    assert run.dry_run and run.results == []
    assert [t.tag for t in run.tasks] == ["sa_1", "sa_2", "sb_1", "sb_2"]
    assert results(orch) == {}
    assert [s.tag for s in orch.stored_specs()] == ["base", "other"]


def test_a_study_runs_every_task(orch):
    finished, seen = [], []
    orch.on("job_finished", lambda job: finished.append(job))
    orch.on_task_result = seen.append
    run = orch.execute_study(config())
    assert run.counts() == {"succeeded": 4}
    assert run.study == "s"
    assert [r.tag for r in seen] == ["sa_1", "sa_2", "sb_1", "sb_2"]
    assert finished == [run]
    stored = results(orch)
    assert stored["sb_2"].scalars() == {
        "base": 1.0,
        "surrogates": 1.0,
        "override": 2.0,
        "options": 0.0,
    }
    assert stored["sb_2"].spec_tag == "sb_2"
    assert stored["sb_2"].overrides == {"p.x": 2}
    derived = orch.model_spec("sb_2")
    assert derived.parent_tag == "base"
    assert derived.spec["surrogates"] == {"u": {"kind": "b"}}
    assert run.result("sa_1").attempts == 1
    with pytest.raises(KeyError):
        run.result("nope")


def test_on_existing(orch):
    orch.execute_study(config(sweeps=config()["sweeps"][:1], output_name="{surrogate}"))
    cfg = config()
    cfg["sweeps"][0]["surrogates"] = ["sa", "sb"]
    cfg["output_name"] = "{surrogate}"
    cfg["sweeps"] = cfg["sweeps"][:1]
    before = len(orch.backend.runs(orch._runs))
    with pytest.raises(TagExistsError):
        orch.execute_study(cfg)
    assert len(orch.backend.runs(orch._runs)) == before
    skipped = orch.execute_study({**cfg, "on_existing": "skip"})
    assert skipped.counts() == {"skipped": 2}
    replaced = orch.execute_study({**cfg, "on_existing": "replace"})
    assert replaced.counts() == {"succeeded": 2}
    assert len(results(orch)) == 2


def test_failed_solves_errors_and_on_error(orch):
    run = orch.execute_study(config(solve_options={"status": "failed"}))
    assert run.counts() == {"failed": 4}
    assert run.results[0].error is None or "infeasible" not in run.results[0].error
    stop = orch.execute_study(
        config(
            solve_options={"status": "error"},
            output_name="e_{surrogate}_{p.x}",
            execution={"on_error": "stop", "retries": 2},
        )
    )
    [only] = stop.results
    assert only.status == "error" and only.attempts == 3
    assert "fake solver crashed" in only.error


def test_a_build_that_raises_is_an_error_result(orch):
    orch.store_model_spec("broken", {"value": 0, "explode": True})
    run = orch.execute_study({"base_model_spec": "broken", "execution": {"retries": 1}})
    [result] = run.results
    assert result.status == "error" and result.attempts == 2
    assert "build failed" in result.error
    assert result.spec_tag == "broken"


def test_config_hooks_and_study(orch):
    run = orch.execute_study(
        {
            "study": "elsewhere",
            "base_model_spec": "b",
            "hooks": {"solve": "flex_orchestrator.testing:fake_solve"},
        },
        dry_run=True,
    )
    assert run.study == "elsewhere" == orch.study
    assert run.tasks[0].hooks == {"solve": "flex_orchestrator.testing:fake_solve"}


def test_ray_needs_hooks_it_can_import(orch):
    # Checked before anything runs (or Ray starts): workers import hooks by name.
    with pytest.raises(HookError, match="can't run on a Ray worker"):
        orch.execute_study(config(), max_workers=2, build=lambda spec, **o: None)
    orch.register_hooks("anonymous", build=lambda spec, **o: None)
    cfg = config(execution={"max_workers": 4}, hooks={"build": "anonymous"})
    with pytest.raises(HookError, match="'anonymous' can't run on a Ray worker"):
        orch.execute_study(cfg, dry_run=True)
    assert results(orch) == {}


def test_a_stored_derived_spec_is_reused_or_refused(orch):
    task = TaskSpec(
        index=0,
        tag="d",
        base="base",
        surrogates=[{"unit": "u", "surrogate": "sa"}],
    )
    assert run_task(orch, task).status == "succeeded"
    orch.delete("result", "d")
    assert run_task(orch, task).status == "succeeded"  # same spec: reused
    orch.delete("result", "d")
    different = TaskSpec.model_validate(
        {**task.model_dump(), "surrogates": [{"unit": "u", "surrogate": "sb"}]}
    )
    with pytest.raises(TagExistsError, match="differs"):
        run_task(orch, different)
    replaced = TaskSpec.model_validate(
        {**different.model_dump(), "on_existing": "replace"}
    )
    assert run_task(orch, replaced).status == "succeeded"
    assert orch.model_spec("d").spec["surrogates"] == {"u": {"kind": "b"}}


def test_strict_units_apply_for_one_task(orch, memory_backend):
    m = Orchestrator(memory_backend, hooks=HOOKS, environ={})
    m.create_study("t")
    m.store_model_spec("base", {"value": 1.0})
    mapper = m.unit_mapper
    run = m.execute_study({"base_model_spec": "base", "export": {"strict_units": True}})
    assert run.counts() == {"succeeded": 1}
    assert m.unit_mapper is mapper


def test_fits_run_before_the_sweeps(orch):
    import pandas as pd

    pytest.importorskip("flexparameterize")
    times = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
    orch.store_data(
        "d",
        pd.DataFrame({"q": [1.0, 2, 3, 4, 5, 6], "p": [2.0, 4, 6, 8, 10, 12]}, times),
        units={"q": "m^3/hr", "p": "kW"},
    )
    run = orch.execute_study(
        {
            "base_model_spec": "base",
            "fits": [
                {
                    "tag": "fit_{name}",
                    "X": {"dataset": "d", "columns": ["q"]},
                    "y": {"dataset": "d", "columns": ["p"]},
                    "variants": [{"name": "lin", "regressor": "multilinear"}],
                }
            ],
            "sweeps": [{"type": "surrogate", "unit": "u", "surrogates": ["fit_lin"]}],
        }
    )
    assert run.planned_fits == ["fit_lin"]
    assert run.fits[0].tags == ["fit_lin"]
    assert run.counts() == {"succeeded": 1}
    assert orch.model_spec("base_fit_lin").spec["surrogates"]["u"][
        "surrogate_type"
    ] == ("multilinear")


def test_task_results_say_whether_they_stored():
    from flex_orchestrator.study import TaskResult

    assert TaskResult(index=0, tag="a", status="skipped").ok
    assert not TaskResult(index=0, tag="a", status="failed").ok


def test_named_sweeps_render_by_name(orch):
    tasks = expand(
        load_config(
            {
                "base_model_spec": "base",
                "sweeps": [
                    {"type": "spec_list", "name": "model", "specs": ["base"]},
                    {
                        "type": "surrogate",
                        "name": "s1",
                        "unit": "u",
                        "surrogates": ["sa"],
                    },
                ],
                "output_name": "{model}-{s1}-{index}",
            }
        )
    )
    assert [t.tag for t in tasks] == ["base-sa-0"]


# Configuration options and cases


def test_configuration_options_derive_a_spec(orch):
    run = orch.execute_study(
        {
            "base_model_spec": "base",
            "sweeps": [
                {
                    "type": "configuration_option",
                    "name": "size",
                    "path": "unit.size",
                    "values": [3, 5],
                },
                {"type": "variable", "path": "p.x", "values": [1]},
            ],
            "output_name": "size_{size}",
        }
    )
    assert run.counts() == {"succeeded": 2}
    derived = orch.model_spec("size_5")
    assert derived.parent_tag == "base"
    assert derived.spec["options"] == {"unit.size": 5}
    assert results(orch)["size_5"].scalars()["options"] == 5.0
    assert results(orch)["size_5"].overrides == {"p.x": 1}


def test_options_are_set_before_surrogates_are_applied(orch):
    seen = []

    def apply(spec, unit, surrogate, relation=None):
        seen.append(dict(spec.get("options", {})))
        return fake_apply(spec, unit, surrogate, relation)

    orch.hooks = orch.hooks.merged(HookSet(apply_surrogate=apply))
    orch.execute_study(
        {
            "base_model_spec": "base",
            "sweeps": [
                {"type": "configuration_option", "path": "o", "values": [3]},
                {"type": "surrogate", "unit": "u", "surrogates": ["sa"]},
            ],
        }
    )
    assert seen == [{"o": 3}]


CASES = {
    "type": "cases",
    "cases": [
        {"name": "small", "surrogates": {"u": "sa"}, "configuration_options": {"o": 1}},
        {"name": "big", "spec": "other", "variables": {"p.x": 4}},
    ],
}


def test_cases_set_chosen_combinations(orch):
    cfg = load_config({"base_model_spec": "base", "sweeps": [CASES]})
    small, big = expand(cfg)
    assert (small.tag, big.tag) == ("base_small", "other_big")
    assert small.configuration == {"o": 1}
    assert small.surrogates[0].surrogate == "sa" and small.spec_tag == "base_small"
    assert big.base == "other" and big.overrides == {"p.x": 4}
    assert big.spec_tag == "other" and big.labels == {"case": "big"}
    run = orch.execute_study(
        {
            "base_model_spec": "base",
            "sweeps": [
                CASES,
                {"type": "variable", "name": "y", "path": "p.y", "values": [1, 2]},
            ],
            "output_name": "{case}_{y}",
        }
    )
    assert [r.tag for r in run.results] == ["small_1", "small_2", "big_1", "big_2"]
    assert run.counts() == {"succeeded": 4}
    assert orch.model_spec("small_2").spec["options"] == {"o": 1}
    assert results(orch)["big_2"].scalars()["override"] == 6.0


@pytest.mark.parametrize(
    ("sweeps", "message"),
    [
        (
            [CASES, {"type": "variable", "name": "x", "path": "p.x", "values": [1]}],
            "both set the variable 'p.x'",
        ),
        (
            [
                CASES,
                {
                    "type": "configuration_option",
                    "name": "a",
                    "path": "o",
                    "values": [1],
                },
            ],
            "both set the configuration option 'o'",
        ),
        (
            [CASES, {"type": "surrogate", "unit": "u", "surrogates": ["sb"]}],
            "both pick the surrogate of 'u'",
        ),
        (
            [CASES, {"type": "spec_list", "specs": ["base"]}],
            "both pick the model spec",
        ),
    ],
)
def test_sweeps_that_clash(sweeps, message):
    with pytest.raises(StudyConfigError, match=message):
        expand(load_config({"base_model_spec": "base", "sweeps": sweeps}))


def test_case_names_must_differ():
    cases = {"type": "cases", "cases": [{"name": "a"}, {"name": "a"}]}
    with pytest.raises(StudyConfigError, match="repeated: \\['a'\\]"):
        load_config({"sweeps": [cases]})


VALUES = [0.12312, 12313221.232, 0.1 + 0.2, 1e20, {"value": 1 / 3, "units": "kW"}]


@pytest.mark.parametrize(
    ("output_name", "tags"),
    [
        (
            None,
            [
                "base_0.12312",
                "base_12313221.232",
                "base_0.30000000000000004",
                "base_1e20",  # not 1e-20: '+' is dropped, not replaced
                "base_0.3333333333333333",
            ],
        ),
        ("v_{v:.3g}", ["v_0.123", "v_1.23e07", "v_0.3", "v_1e20", "v_0.333"]),
    ],
)
def test_float_values_in_tags(output_name, tags):
    sweep = {"type": "variable", "name": "v", "path": "p", "values": VALUES}
    cfg = {"base_model_spec": "base", "sweeps": [sweep], "output_name": output_name}
    tasks = expand(load_config(cfg))
    assert [t.tag for t in tasks] == tags
    assert tasks[2].overrides == {"p": 0.1 + 0.2}  # the value itself is exact


@pytest.mark.parametrize(
    ("output_name", "message"),
    [
        ("v_{v:.1f}", "or format its values more precisely"),
        ("v_{v:d}", r"can't format 0.12 as \{:d\}"),
    ],
)
def test_formats_that_fail(output_name, message):
    sweep = {"type": "variable", "name": "v", "path": "p", "values": [0.12, 0.14]}
    cfg = {"base_model_spec": "base", "sweeps": [sweep], "output_name": output_name}
    with pytest.raises(StudyConfigError, match=message):
        expand(load_config(cfg))


def test_the_ray_address_comes_from_the_call_the_config_or_the_environment():
    from flex_orchestrator.backend import MemoryBackend

    seen = []
    orch = Orchestrator(
        MemoryBackend(), hooks=HOOKS, environ={"FLEXORCH_RAY_ADDRESS": "ray://env:1"}
    )
    assert orch.ray_address == "ray://env:1"
    assert Orchestrator(MemoryBackend(), environ={}).ray_address is None
    orch.create_study("s")
    orch.store_model_spec("base", {"value": 1.0})

    def fake_run_tasks(orch, tasks, hooks, execution, workers, progress):
        seen.append(execution.ray_address)
        return []

    import flex_orchestrator.parallel as parallel

    real, parallel.run_tasks = parallel.run_tasks, fake_run_tasks
    try:
        cfg = {
            "base_model_spec": "base",
            "hooks": {"build": "flex_orchestrator.testing:fake_build"},
        }
        orch.execute_study(cfg, max_workers=2)
        orch.execute_study(
            {**cfg, "execution": {"ray_address": "ray://cfg:1"}}, max_workers=2
        )
        orch.execute_study(cfg, max_workers=2, ray_address="ray://call:1")
    finally:
        parallel.run_tasks = real
    assert seen == ["ray://env:1", "ray://cfg:1", "ray://call:1"]


def test_a_task_that_cant_be_prepared_is_an_error_and_isnt_retried(orch):
    # Retries are for the model work; a spec that can't be read stays missing.
    run = orch.execute_study(
        {
            "base_model_spec": "base",
            "sweeps": [{"type": "surrogate", "unit": "u", "surrogates": ["nope"]}],
            "execution": {"retries": 1},
        }
    )
    [result] = run.results
    assert result.status == "error" and result.attempts == 1
    assert "nope" in result.error


def test_a_task_whose_result_cant_be_stored_isnt_retried(orch, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(orch, "store_model_data", refuse)
    run = orch.execute_study(config(execution={"retries": 2}))
    assert {r.status for r in run.results} == {"error"}
    assert {r.attempts for r in run.results} == {1}
    assert "store is down" in run.results[0].error


def test_registered_hooks_run_in_process(orch):
    orch.register_hooks("mine", build=tiny_build, solve=fake_solve)
    run = orch.execute_study(config(hooks={"build": "mine", "solve": "mine"}))
    assert run.counts() == {"succeeded": 4}
    with pytest.raises(HookError, match="No hooks are registered as 'other'"):
        orch.execute_study(config(hooks={"build": "other"}))


def test_cancelling_a_study_cancels_its_fits(orch, monkeypatch):
    from flex_orchestrator.errors import CommandCancelled
    from flex_orchestrator.ext.commands import Progress
    from flex_orchestrator.study import runner

    study = Progress()

    def cancelled_mid_fit(orch, fit, progress, execution, workers):
        study.cancel()
        progress.check()  # the fit's own progress sees it

    monkeypatch.setattr(runner, "run_fits", cancelled_mid_fit)
    cfg = config(
        fits=[
            {
                "tag": "f",
                "regressor": "multilinear",
                "X": {"dataset": "d"},
                "y": {"dataset": "d"},
            }
        ],
    )
    params = runner.ExecuteParams(config=load_config(cfg))
    with pytest.raises(CommandCancelled):
        runner.execute_study.fn(orch, params, study)
    assert results(orch) == {}


def test_time_sets_hooks_in_a_worker(orch, monkeypatch):
    from flex_orchestrator.study import task as steps

    work = steps.prepare(orch, expand(load_config(config()))[0], HOOKS)
    broken = dataclasses.replace(
        work,
        hooks=HookSet(build=tiny_build, solve=fake_solve, time_sets="nope.module:fn"),
    )
    assert "nope.module" in steps.execute(broken).error
    monkeypatch.setattr(steps, "DEFAULT_HOOKS", HookSet(time_sets="nope.module:fn"))
    done = steps.execute(broken)  # flex-pse's hook missing: time is duck-typed
    assert done.error is None and done.outcome.ok


def test_connecting_to_a_named_ray(monkeypatch):
    import sys
    import types

    from flex_orchestrator import parallel

    calls = []
    fake = types.SimpleNamespace(
        is_initialized=lambda: False, init=lambda **kw: calls.append(kw)
    )
    monkeypatch.setitem(sys.modules, "ray", fake)
    assert parallel.connect("ray://head:10001") is fake
    assert calls == [{"address": "ray://head:10001", "namespace": "flexorch"}]
