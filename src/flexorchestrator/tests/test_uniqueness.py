"""No duplicates, no overwrites, real deletion on the memory backend."""

import pandas as pd
import pytest

from flexorchestrator.errors import (
    StorageError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
)
from flexorchestrator.export import ExportedSeries, ModelData
from flexorchestrator.hooks import SolveOutcome
from flexorchestrator.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

STAMPS = pd.date_range("2025-01-01", periods=3, freq="15min", tz="UTC")


def model(value):
    return ModelData(
        [
            ExportedSeries(
                "p", "timeseries", "p", [], [value] * 3, list(STAMPS), "kW", None
            )
        ]
    )


def table(value):
    return pd.DataFrame({"x": [value] * 3}, index=STAMPS)


@pytest.fixture
def orch(memory_backend):
    orch = Orchestrator(memory_backend)
    orch.create_study("s")
    return orch


def live(orch, kind="result"):
    ledger = "s::runs" if kind == "result" else "s::data"
    return [r for r in orch.backend.runs(ledger) if r.status != "failed"]


# replace=True


def test_replace_writes_the_new_result_then_deletes_the_old(orch):
    old = orch.store_model_data("base", model(1.0))
    new = orch.store_model_data("base", model(2.0), replace=True)
    assert [r.run_id for r in live(orch)] == [new.run_id]
    assert orch.model_results("base")["p"].values == [2.0] * 3
    assert old.run_id != new.run_id


def test_replace_without_an_old_result_just_stores(orch):
    orch.store_model_data("base", model(1.0), replace=True)
    assert orch.model_results("base")["p"].values == [1.0] * 3


def test_a_failed_replacement_keeps_the_old_result(orch):
    orch.store_model_data("base", model(1.0))
    failed = orch.store_model_data(
        "base", model(2.0), SolveOutcome(status="failed"), replace=True
    )
    assert not failed.ok
    assert orch.model_results("base")["p"].values == [1.0] * 3


def test_replace_a_dataset(orch):
    orch.store_data("d", table(1.0))
    orch.store_data("d", table(2.0), replace=True)
    assert orch.get_data("d")["x"].to_list() == [2.0] * 3
    assert len(live(orch, "data")) == 1


def test_replace_needs_deletion(memory_backend):
    orch = Orchestrator(memory_backend, allow_delete=False)
    orch.create_study("s")
    orch.store_model_data("base", model(1.0))
    with pytest.raises(StorageError, match="allow_delete=False"):
        orch.store_model_data("base", model(2.0), replace=True)
    with pytest.raises(StorageError, match="allow_delete=False"):
        orch.delete("result", "base")
    with pytest.raises(StorageError, match="allow_delete=False"):
        orch.delete_study("s")


# Races: the check-then-write isn't atomic in Acquirium


def race(orch, monkeypatch, value):
    """Make another writer store `base` between our tag check and our write."""
    original = orch._store

    def store_with_a_rival(tag, data, outcome, streams, *link):
        monkeypatch.setattr(orch, "_store", original)
        original(tag, model(value), None, None)  # the rival's run starts first
        return original(tag, data, outcome, streams, *link)

    monkeypatch.setattr(orch, "_store", store_with_a_rival)


def test_a_racing_duplicate_is_removed(orch, monkeypatch):
    race(orch, monkeypatch, 9.0)
    with pytest.raises(TagExistsError, match="at the same time"):
        orch.store_model_data("base", model(1.0))
    assert len(live(orch)) == 1
    assert orch.model_results("base")["p"].values == [9.0] * 3  # the first one won


def test_a_racing_replacement_wins_over_older_runs(orch, monkeypatch):
    orch.store_model_data("base", model(1.0))
    race(orch, monkeypatch, 9.0)
    orch.store_model_data("base", model(2.0), replace=True)
    assert len(live(orch)) == 1
    assert orch.model_results("base")["p"].values == [2.0] * 3  # the latest one won


def test_a_replacement_that_lost_the_race_is_removed(orch, monkeypatch):
    orch.store_model_data("base", model(1.0))
    original = orch._store

    def store_then_a_rival(tag, data, outcome, streams, *link):
        stored = original(tag, data, outcome, streams, *link)
        original(tag, model(9.0), None, None)  # a later replacement
        return stored

    monkeypatch.setattr(orch, "_store", store_then_a_rival)
    with pytest.raises(TagExistsError, match="at the same time"):
        orch.store_model_data("base", model(2.0), replace=True)
    monkeypatch.setattr(orch, "_store", original)
    tables = [orch.backend.records("s::runs", r.run_id)[1].value for r in live(orch)]
    # The old result and the rival's remain; this one (2.0) is gone.
    assert sorted(t["p"][0] for t in tables) == [1.0, 9.0]


def test_a_running_result_blocks_replace(orch):
    orch.backend.start_run(
        "s::runs", {"flexorch_kind": "result", "flexorch_tag": "base"}
    )
    with pytest.raises(TagExistsError, match="running result 'base'"):
        orch.store_model_data("base", model(1.0), replace=True)


# delete


def test_delete_removes_every_run_of_a_tag(orch):
    orch.store_model_data("base", model(1.0), SolveOutcome(status="failed"))
    orch.store_model_data("base", model(2.0))
    orch.store_model_data("other", model(3.0))
    assert orch.delete("result", "base") == 2
    with pytest.raises(TagNotFoundError):
        orch.model_results("base")
    with pytest.raises(TagNotFoundError, match="no stored result 'base'"):
        orch.delete("result", "base")
    assert orch.model_results("other")["p"].values == [3.0] * 3
    orch.store_model_data("base", model(4.0))  # the tag is free again


def test_delete_a_dataset(orch):
    orch.store_data("d", table(1.0))
    assert orch.delete("data", "d") == 1
    with pytest.raises(TagNotFoundError):
        orch.get_data("d")


def test_delete_needs_a_known_kind(orch):
    with pytest.raises(ValueError, match="kind must be one of"):
        orch.delete("spec", "base")


def test_delete_refuses_a_running_run(orch):
    orch.backend.start_run(
        "s::runs", {"flexorch_kind": "result", "flexorch_tag": "base"}
    )
    with pytest.raises(StorageError, match="still running"):
        orch.delete("result", "base")


# delete_study


def test_delete_study(orch):
    orch.store_model_data("base", model(1.0))
    orch.store_data("d", table(1.0))
    orch.delete_study("s")
    assert not any(orch.backend.has_study(n) for n in ("s", "s::runs", "s::data"))
    with pytest.raises(StudyNotFoundError, match="No study is selected"):
        _ = orch.study
    orch.create_study("s")  # the name is free again
    with pytest.raises(StudyNotFoundError, match="no study 'nope'"):
        orch.delete_study("nope")


def test_delete_study_keeps_the_selection_of_another(orch):
    orch.create_study("t")
    orch.delete_study("s")
    assert orch.study == "t"


def test_delete_study_refuses_a_running_run(orch):
    orch.backend.start_run("s::data", {})
    with pytest.raises(StorageError, match="running run"):
        orch.delete_study("s")
    assert orch.backend.has_study("s::runs")


def test_delete_study_without_ledgers(memory_backend):
    memory_backend.define_study("bare")  # made outside the orchestrator
    orch = Orchestrator(memory_backend)
    orch.delete_study("bare")
    assert not memory_backend.has_study("bare")
