"""Studies and the explicit results API (PLAN.md D4, D9) on the memory backend."""

import copy
from datetime import UTC, datetime, timedelta

import pyomo.environ as pyo
import pytest
from pyomo.environ import units as pu

from flex_orchestrator.errors import (
    ExportError,
    HookError,
    StorageError,
    StudyExistsError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
    UnitError,
)
from flex_orchestrator.export import export_model_data
from flex_orchestrator.hooks import DEFAULT_HOOKS, HookSet, SolveOutcome
from flex_orchestrator.orchestrator import SOLVE_OUTCOME, Orchestrator
from flex_orchestrator.results import MODEL_MANIFEST, MODEL_VALUES
from flex_orchestrator.testing import fake_solve

pytestmark = pytest.mark.unit

POWER = "costing.aggregate_electrical_power"
OK = SolveOutcome(status="ok", termination="optimal", solver="ipopt")


@pytest.fixture
def memory_orch(memory_backend):
    orch = Orchestrator(memory_backend)
    orch.create_study("example_a")
    return orch


def runs(orch, **metadata):
    return orch.backend.runs(orch._runs, metadata=metadata or None)


def utc(timestamps):
    return [t.to_pydatetime().replace(tzinfo=UTC) for t in timestamps]


# Studies (D9)


def test_create_select_and_get_or_create(memory_backend):
    orch = Orchestrator(memory_backend)
    with pytest.raises(StudyNotFoundError, match="No study is selected"):
        orch.store_model_results("a", model=None)
    assert orch.create_study("example_a") == orch.study == "example_a"
    with pytest.raises(StudyExistsError, match="select_study"):
        orch.create_study("example_a")
    with pytest.raises(StudyNotFoundError, match="create_study"):
        orch.select_study("example_b")
    assert orch.get_or_create_study("example_b") == "example_b"
    assert orch.get_or_create_study("example_a") == "example_a"
    assert Orchestrator(memory_backend, study="example_b").study == "example_b"


def test_study_names_follow_the_tag_rules(memory_orch):
    with pytest.raises(Exception, match="not valid"):
        memory_orch.create_study("a::runs")


# store_model_results


def test_store_and_read_back(memory_orch, demo_model):
    stored = memory_orch.store_model_results(
        "base", demo_model, OK, include=["costing.*", "demo.pump.*"]
    )
    assert (stored.status, stored.ok, stored.outcome) == ("succeeded", True, OK)
    exported = export_model_data(demo_model, include=["costing.*", "demo.pump.*"])
    assert stored.keys == exported.keys()

    back = memory_orch.model_results("base")
    assert back.keys() == exported.keys()
    power = back[POWER]
    assert power.values == exported[POWER].values
    assert power.timestamps == utc(exported[POWER].timestamps)
    assert (power.unit_pyomo, power.component, power.index) == ("kW", POWER, [])
    # The snapshot keeps the exporter's key; without a resolver it is unverified.
    assert (power.unit_qudt, power.unit_verified) == ("KiloW", False)
    for before in exported:
        after = back[before.key]
        assert (after.kind, after.values, after.unit_pyomo) == (
            before.kind,
            before.values,
            before.unit_pyomo,
        )
    element = back["costing.aggregate_power[electrical]"]
    assert (element.component, element.index) == (
        "costing.aggregate_power",
        ["electrical"],
    )
    intensity = back["demo.pump.energy_intensity"]
    assert (intensity.kind, intensity.value) == ("scalar", 0.5)


def test_run_metadata_and_solve_outcome(memory_orch, demo_model):
    stored = memory_orch.store_model_results("base", demo_model, OK, keys=[POWER])
    [run] = runs(memory_orch)
    assert run.run_id == stored.run_id
    assert run.metadata["flexorch_kind"] == "result"
    assert run.metadata["flexorch_tag"] == "base"
    assert run.metadata["flexorch_version"]
    records = {
        r.label: r for r in memory_orch.backend.records(memory_orch._runs, run.run_id)
    }
    assert records[SOLVE_OUTCOME].value == OK.model_dump(mode="json")
    assert SOLVE_OUTCOME not in memory_orch.model_results("base").keys()


def test_a_result_is_a_snapshot(memory_orch, demo_model):
    stored = memory_orch.store_model_results("base", demo_model, keys=[POWER])
    records = memory_orch.backend.records(memory_orch._runs, stored.run_id)
    assert [r.label for r in records] == [MODEL_MANIFEST, MODEL_VALUES]
    manifest, table = (r.value for r in records)
    assert manifest["schema"] == "flexorch-model/1"
    assert [e["key"] for e in manifest["series"]] == [POWER]
    assert table.columns == ["time", POWER]
    assert stored.streams == []


def test_streams_are_also_stored_natively(memory_orch, demo_model):
    stored = memory_orch.store_model_results(
        "base",
        demo_model,
        include=["costing.aggregate*", "demo.pump.energy_intensity"],
        streams=[POWER, "costing.aggregate_power", "demo.pump.*"],
    )
    assert sorted(stored.streams) == sorted(
        [POWER, "costing.aggregate_power[electrical]", "demo.pump.energy_intensity"]
    )
    by_label = {
        r.label: r
        for r in memory_orch.backend.records(memory_orch._runs, stored.run_id)
    }
    exported = export_model_data(demo_model, keys=[POWER])[POWER]
    assert [v for _, v in by_label[POWER].rows] == exported.values
    assert by_label["demo.pump.energy_intensity"].value == 0.5
    native = memory_orch.backend.variable(memory_orch._runs, POWER)
    assert native.metadata == {"pyomo_unit": "kW", "component": POWER, "index": "[]"}


def test_streams_must_match_something(memory_orch, demo_model):
    with pytest.raises(ExportError, match="match no exported key"):
        memory_orch.store_model_results(
            "base", demo_model, keys=[POWER], streams=["nope.*"]
        )
    assert runs(memory_orch) == []


def test_stale_values_stay_in_the_snapshot_but_have_no_samples(memory_orch, demo_model):
    stored = memory_orch.store_model_results(
        "base", demo_model, keys=["demo.battery.soc"], streams=["demo.battery.soc"]
    )
    soc = memory_orch.model_results("base")["demo.battery.soc"]
    assert soc.values == [None] * len(soc.values) and soc.stale
    [native] = [
        r
        for r in memory_orch.backend.records(memory_orch._runs, stored.run_id)
        if r.label == "demo.battery.soc"
    ]
    assert native.rows == []


def test_scalars_only_store_no_table(memory_orch, demo_model):
    stored = memory_orch.store_model_results(
        "base", demo_model, keys=["demo.pump.energy_intensity"]
    )
    labels = [
        r.label for r in memory_orch.backend.records(memory_orch._runs, stored.run_id)
    ]
    assert labels == [MODEL_MANIFEST]
    assert memory_orch.model_results("base")["demo.pump.energy_intensity"].value == 0.5


def test_read_converts_units_with_pint(memory_orch, demo_model):
    memory_orch.store_model_results(
        "base", demo_model, keys=[POWER, "costing.aggregate_power"]
    )
    exported = export_model_data(demo_model, keys=[POWER])[POWER]
    back = memory_orch.model_results(
        "base", units={POWER: "W", "costing.aggregate_power": "MW"}
    )
    assert back[POWER].unit_pyomo == "W"
    assert back[POWER].values == pytest.approx([v * 1000 for v in exported.values])
    assert (back[POWER].unit_qudt, back[POWER].unit_verified) == (None, False)
    assert back["costing.aggregate_power[electrical]"].unit_pyomo == "MW"
    with pytest.raises(ExportError, match="weren't stored"):
        memory_orch.model_results("base", units={"nope": "W"})
    with pytest.raises(UnitError, match="dimensions"):
        memory_orch.model_results("base", units={POWER: "m"})


def test_a_tag_is_stored_once(memory_orch, demo_model):
    memory_orch.store_model_results("base", demo_model, keys=[POWER])
    with pytest.raises(TagExistsError, match="already has a succeeded result 'base'"):
        memory_orch.store_model_results("base", demo_model, keys=[POWER])


def test_a_failed_solve_stores_only_its_outcome_and_frees_the_tag(
    memory_orch, demo_model
):
    bad = SolveOutcome(status="failed", termination="infeasible", message="no")
    stored = memory_orch.store_model_results("base", demo_model, bad)
    assert (stored.status, stored.ok, stored.keys) == ("failed", False, [])
    [run] = runs(memory_orch)
    assert run.status == "failed"
    assert run.error == {
        "type": "SolveNotOptimal",
        "message": "solve failed (infeasible): no",
    }
    with pytest.raises(TagNotFoundError):
        memory_orch.model_results("base")
    retry = memory_orch.store_model_results("base", demo_model, OK, keys=[POWER])
    assert retry.ok and memory_orch.model_results("base").keys() == [POWER]


def test_the_latest_succeeded_run_is_read(memory_orch, demo_model):
    # A failed run doesn't hold the tag; only the succeeded one is read.
    memory_orch.store_model_results("base", demo_model, SolveOutcome(status="error"))
    memory_orch.store_model_results("base", demo_model, keys=[POWER])
    assert memory_orch.model_results("base").keys() == [POWER]


def test_outcome_must_be_a_solve_outcome(memory_orch, demo_model):
    with pytest.raises(TypeError, match="SolveOutcome"):
        memory_orch.store_model_results("base", demo_model, {"status": "ok"})


def test_bad_tag(memory_orch, demo_model):
    with pytest.raises(Exception, match="not valid"):
        memory_orch.store_model_results("no spaces", demo_model)


# Native streams keep their first declaration


def test_streams_convert_to_the_declared_unit(memory_orch, demo_model):
    memory_orch.backend.declare(
        memory_orch._runs,
        POWER,
        role="output",
        kind="timeseries",
        metadata={"pyomo_unit": "W", "component": POWER, "index": "[]"},
    )
    stored = memory_orch.store_model_results(
        "base", demo_model, keys=[POWER], streams=[POWER]
    )
    exported = export_model_data(demo_model, keys=[POWER])[POWER]
    [native] = [
        r
        for r in memory_orch.backend.records(memory_orch._runs, stored.run_id)
        if r.label == POWER
    ]
    assert [v for _, v in native.rows] == pytest.approx(
        [v * 1000 for v in exported.values]
    )
    # The snapshot keeps the exported unit.
    assert memory_orch.model_results("base")[POWER].unit_pyomo == "kW"


@pytest.mark.parametrize(
    ("declared", "error", "match"),
    [
        (
            {"kind": "timeseries", "metadata": {"pyomo_unit": "m"}},
            UnitError,
            "dimensions",
        ),
        ({"kind": "timeseries", "metadata": {}}, UnitError, "no unit"),
        (
            {"kind": "scalar", "metadata": {"pyomo_unit": "kW"}},
            StorageError,
            "stored as a scalar",
        ),
    ],
)
def test_values_that_cant_be_stored_leave_no_run(
    memory_orch, demo_model, declared, error, match
):
    memory_orch.backend.declare(memory_orch._runs, POWER, role="output", **declared)
    with pytest.raises(error, match=match):
        memory_orch.store_model_results(
            "base", demo_model, keys=[POWER], streams=[POWER]
        )
    assert runs(memory_orch) == []


def test_a_write_that_fails_fails_the_run(memory_orch, demo_model, monkeypatch):
    def broken(run_id, label, value):
        raise StorageError("disk full")

    monkeypatch.setattr(memory_orch.backend, "record", broken)
    with pytest.raises(StorageError, match="disk full"):
        memory_orch.store_model_results("base", demo_model, keys=[POWER])
    [run] = runs(memory_orch)
    assert (run.status, run.error["message"]) == ("failed", "disk full")


# Time


def test_flexpse_partial_horizon_sets_are_timeseries(memory_orch, logic_model):
    # The default time_sets hook makes startup a timeseries over t >= 1.
    memory_orch.store_model_results("logic", logic_model, include=["p.ro*startup"])
    startup = memory_orch.model_results("logic")["p.ro[0].startup"]
    assert startup.kind == "timeseries"
    assert startup.timestamps == utc(logic_model.time_block.datetime_index)[1:]


def user_model():
    m = pyo.ConcreteModel()
    m.t = pyo.Set(initialize=range(3), ordered=True)
    m.flow = pyo.Var(m.t, units=pu.L / pu.day, initialize=2.0)
    return m


def test_a_user_model_needs_datetimes(memory_orch):
    m = user_model()
    with pytest.raises(StorageError, match="Pass datetime_index="):
        memory_orch.store_model_results("user", m, time_sets=[m.t])
    assert runs(memory_orch) == []
    start = datetime(2025, 1, 1, tzinfo=UTC)
    when = [start + timedelta(hours=i) for i in range(3)]
    memory_orch.store_model_results("user", m, time_sets=[m.t], datetime_index=when)
    assert memory_orch.model_results("user")["flow"].timestamps == when


def test_a_user_model_without_time_stores_scalars(memory_orch):
    # The flex-pse time_sets hook finds no TimeBlock and leaves time alone.
    memory_orch.store_model_results("user", user_model())
    assert {s.kind for s in memory_orch.model_results("user")} == {"scalar"}


def test_default_time_sets_hook_is_skipped_without_flexpse(memory_orch, monkeypatch):
    def missing(ref, kind):
        raise HookError("flex-pse isn't installed")

    monkeypatch.setattr(memory_orch.registry, "resolve_ref", missing)
    assert memory_orch._time_sets(user_model()) is None


def test_a_users_own_time_sets_hook_must_resolve(memory_backend):
    orch = Orchestrator(memory_backend, hooks=HookSet(time_sets="nopkg.nomod:fn"))
    with pytest.raises(HookError):
        orch._time_sets(user_model())
    assert Orchestrator(memory_backend, hooks=HookSet(build="x:y")).hooks.time_sets == (
        DEFAULT_HOOKS.time_sets
    )


def test_no_time_sets_hook(memory_backend):
    orch = Orchestrator(memory_backend)
    orch.hooks = HookSet()
    assert orch._time_sets(user_model()) is None


# solve_and_store


def test_solve_and_store(memory_orch, demo_model):
    stored = memory_orch.solve_and_store(
        "base", demo_model, keys=[POWER], solve="flex_orchestrator.testing:fake_solve"
    )
    assert (stored.ok, stored.outcome.solver, stored.keys) == (True, "fake", [POWER])


@pytest.mark.parametrize(
    ("status", "termination"), [("failed", "infeasible"), ("error", None)]
)
def test_solve_and_store_failures(memory_orch, demo_model, status, termination):
    stored = memory_orch.solve_and_store(
        "base", demo_model, solve=fake_solve, status=status
    )
    assert (stored.status, stored.outcome.termination) == ("failed", termination)


def test_solve_and_store_checks_the_tag_before_solving(memory_orch, demo_model):
    memory_orch.store_model_results("base", demo_model, keys=[POWER])
    calls = []

    def solve(model, **options):
        calls.append(model)
        return OK

    with pytest.raises(TagExistsError):
        memory_orch.solve_and_store("base", demo_model, solve=solve)
    assert calls == []


def test_solve_and_store_uses_the_orchestrators_solve_hook(memory_backend, demo_spec):
    from flex_orchestrator.flexpse import build

    orch = Orchestrator(
        memory_backend, hooks=HookSet(solve="flex_orchestrator.testing:fake_solve")
    )
    orch.create_study("s")
    stored = orch.solve_and_store("base", build(copy.deepcopy(demo_spec)), keys=[POWER])
    assert stored.outcome.solver == "fake"


# store_model_data


def test_store_model_data_takes_exported_values(memory_orch, demo_model):
    data = export_model_data(demo_model, keys=[POWER])
    stored = memory_orch.store_model_data("base", data, OK)
    assert stored.keys == [POWER]
    assert memory_orch.model_results("base")[POWER].values == data[POWER].values
    with pytest.raises(TagExistsError):
        memory_orch.store_model_data("base", data)
    with pytest.raises(TypeError, match="SolveOutcome"):
        memory_orch.store_model_data("other", data, "ok")


# Acquirium access (D8)


def test_memory_backend_has_no_acquirium(memory_orch):
    with pytest.raises(StorageError, match="needs the Acquirium backend"):
        _ = memory_orch.acquirium


# Result handles and frames (D7)


def test_result_handles(memory_orch, demo_model):
    import pandas as pd
    import polars as pl

    keys = [POWER, "costing.aggregate_power", "demo.pump.energy_intensity"]
    memory_orch.store_model_results("a", demo_model, OK, keys=keys)
    memory_orch.store_model_results("b", demo_model, keys=[POWER])
    handles = memory_orch.stored_model_results()
    assert [h.tag for h in handles] == ["a", "b"]
    assert [h.tag for h in memory_orch.stored_model_results(tags=["b"])] == ["b"]

    a = memory_orch.result("a")
    exported = export_model_data(demo_model, keys=[POWER])[POWER]
    df = a.to_polars()
    assert isinstance(df, pl.DataFrame)
    assert df.columns == ["time", POWER, "costing.aggregate_power[electrical]"]
    assert df[POWER].to_list() == exported.values
    pdf = a.to_pandas(units={POWER: "W"})
    assert isinstance(pdf, pd.DataFrame) and str(pdf.index.tz) == "UTC"
    assert pdf[POWER].tolist() == pytest.approx([v * 1000 for v in exported.values])
    assert isinstance(a.to_frame(), pl.DataFrame)  # the orchestrator's default
    assert a.scalars() == {"demo.pump.energy_intensity": 0.5}
    assert a.units[POWER] == "kW"
    assert a.outcome == OK
    assert a.metadata["flexorch_tag"] == "a" and a.started_at is not None
    assert memory_orch.result("b").outcome is None


def test_a_frame_aligns_subsets_of_time(memory_orch, logic_model):
    memory_orch.store_model_results("logic", logic_model, include=["p.ro*"])
    df = memory_orch.result("logic").to_polars()
    assert df["p.ro[0].startup"][0] is None  # startup has no first point
    assert df.height == len(logic_model.time_block.time_index)
