"""Results round-trip on every backend: export -> store -> read back (PLAN.md §7).

Each test runs on memory and files (unit tier) and the shared Acquirium
(component tier): storing results is what these tests are about.
"""

from datetime import UTC
from importlib.metadata import version

import pytest

from flex_orchestrator.errors import TagExistsError
from flex_orchestrator.export import ModelData, export_model_data
from flex_orchestrator.hooks import DEFAULT_HOOKS, SolveOutcome
from flex_orchestrator.orchestrator import Orchestrator

SELECTION = ["costing.aggregate*", "costing.annualized_cost", "demo.pump.*"]
OK = SolveOutcome(status="ok", termination="optimal")

pytestmark = pytest.mark.on_acquirium


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


# Datasets (D6)


def test_dataset_round_trip(orch):
    import warnings
    from datetime import datetime, timedelta

    import pandas as pd

    stamps = [datetime(2025, 1, 1) + timedelta(minutes=15 * i) for i in range(96)]
    df = pd.DataFrame(
        {
            "biogas_volume": [float(i % 7) if i % 10 else None for i in range(96)],
            "flow_in_feed": [10.0 + i for i in range(96)],
        },
        index=pd.DatetimeIndex(stamps),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the memory backend can't verify units
        orch.store_data(
            "biogas_data", df, units={"biogas_volume": "m3/day", "flow_in_feed": "kW"}
        )
    back = orch.get_data("biogas_data", frame="pandas")
    assert list(back.columns) == ["biogas_volume", "flow_in_feed"]
    assert back.index.tolist() == [pd.Timestamp(t, tz="UTC") for t in stamps]
    expected = df["biogas_volume"].tolist()
    assert [None if v != v else v for v in back["biogas_volume"]] == [
        None if v != v else v for v in expected
    ]
    one = orch.get_data(column="flow_in_feed")
    assert one["flow_in_feed"].to_list() == df["flow_in_feed"].tolist()


@pytest.mark.component
def test_acquirium_resolves_data_units(acquirium_local, study_name):
    from datetime import datetime, timedelta

    import pandas as pd

    from flex_orchestrator.backend import AcquiriumBackend
    from flex_orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(AcquiriumBackend(acquirium_local))
    orch.create_study(study_name)
    stamps = [datetime(2025, 1, 1) + timedelta(hours=i) for i in range(3)]
    df = pd.DataFrame({c: [1.0, 2.0, 3.0] for c in "abcd"}, index=stamps)
    stored = orch.store_data(
        "d", df, units={"a": "m3/day", "b": "gal/min", "c": "GAL_US-PER-MIN", "d": "kW"}
    )
    assert stored.units == {
        "a": "M3-PER-DAY",
        "b": "GAL_US-PER-MIN",
        "c": "GAL_US-PER-MIN",
        "d": "KiloW",
    }


@pytest.mark.component
def test_orch_acquirium_is_the_raw_client(acquirium_local, study_name, demo_model):
    from flex_orchestrator.backend import AcquiriumBackend
    from flex_orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(AcquiriumBackend(acquirium_local))
    orch.create_study(study_name)
    orch.store_model_results("base", demo_model, keys=["demo.pump.flow_in"])
    assert orch.acquirium is acquirium_local
    runs = orch.acquirium.study.get(f"{study_name}::runs").experiments.where(
        metadata={"flexorch_kind": "result", "flexorch_tag": "base"}
    )
    assert [r.status for r in runs.all()] == ["succeeded"]


def test_run_metadata_records_what_reproduces_a_result(orch, monkeypatch):
    """D10: versions, the user and the hooks are in every result's metadata."""
    monkeypatch.setenv("FLEXORCH_USER", "alice")
    orch.store_model_data("base", ModelData([]), OK)
    meta = orch.result("base").metadata
    assert meta["flexorch_user"] == "alice"
    assert meta["flexorch_version"] == version("flex-orchestrator")
    assert meta["flexpse_version"] == version("flex-pse")
    assert meta["flexorch_hooks"]["solve"] == DEFAULT_HOOKS.solve
    assert "flexorch_connection" not in meta  # the backend was passed in


@pytest.mark.component
def test_attach_to_the_local_acquirium(acquirium_local, acquirium_dir, study_name):
    """D11 mode 1 on the session's data_dir attaches to its running server."""
    orch = Orchestrator(data_dir=acquirium_dir)  # not closed: the server is shared
    assert orch.connection.acquirium is acquirium_local
    orch.create_study(study_name)
    orch.store_model_data("base", ModelData([]), OK)
    meta = orch.result("base").metadata
    assert meta["flexorch_connection"] == {
        "mode": "local",
        "target": str(acquirium_dir.resolve()),
    }
    assert meta["acquirium_version"] == version("acquirium")


def test_studies_lists_orchestrator_studies(orch, study_name):
    orch.create_study(f"{study_name}-b")
    found = orch.studies()
    assert {study_name, f"{study_name}-b"} <= set(found)
    assert not any("::" in name for name in found)


def test_register_hooks(orch):
    from flex_orchestrator.testing import fake_build

    hooks = orch.register_hooks("mine", build=fake_build)
    assert orch.registry.resolve_ref("mine", "build") is fake_build
    assert hooks.build is fake_build
