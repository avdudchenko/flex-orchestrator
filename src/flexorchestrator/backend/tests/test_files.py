"""What the file backend adds to the contract: it lasts on disk.

The contract itself runs on it in ``test_contract.py``; these tests reopen the
folder, as a later process would.
"""

import json
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from flexorchestrator.backend import FileBackend, FileValue
from flexorchestrator.backend.files import MARKER
from flexorchestrator.errors import StorageError

pytestmark = pytest.mark.unit

T0 = datetime(2025, 7, 8, tzinfo=UTC)


def filled(path):
    """A store with one study holding a run of every kind, and a failed run."""
    backend = FileBackend(path)
    backend.define_study("s::runs")
    for label, kind, unit in [
        ("power", "timeseries", "KiloW"),
        ("cost", "scalar", "USD"),
        ("outcome", "json", None),
        ("values", "table", None),
        ("tariff", "file", None),
    ]:
        backend.declare(
            "s::runs", label, role="output", kind=kind, unit=unit, metadata={"m": 1}
        )
    ok = backend.start_run("s::runs", {"flexorch_tag": "a", "nested": {"k": [1]}})
    backend.record(ok, "power", [(T0 + timedelta(hours=i), float(i)) for i in range(3)])
    backend.record(ok, "cost", 2.5)
    backend.record(ok, "outcome", {"status": "ok", "log": None})
    backend.record(ok, "values", pl.DataFrame({"time": [T0], "x.y[1]": [1.5]}))
    backend.record(ok, "tariff", FileValue("tariff.csv", b"a,b\n\x00\xff"))
    backend.finish_run(ok)
    bad = backend.start_run("s::runs", {"flexorch_tag": "b"})
    backend.fail_run(bad, RuntimeError("infeasible"))
    return backend


def test_everything_reads_back_after_reopening(tmp_path):
    written = filled(tmp_path)
    read = FileBackend(tmp_path)
    assert read.studies() == ["s::runs"]
    assert read.variables("s::runs") == written.variables("s::runs")
    assert read.runs("s::runs") == written.runs("s::runs")
    [ok, bad] = read.runs("s::runs")
    assert (bad.status, bad.error) == (
        "failed",
        {"type": "RuntimeError", "message": "infeasible"},
    )
    again, first = (
        read.records("s::runs", ok.run_id),
        written.records("s::runs", ok.run_id),
    )
    assert [r.label for r in again] == ["power", "cost", "outcome", "values", "tariff"]
    for a, b in zip(again, first, strict=True):
        if a.kind == "table":
            assert a.value.equals(b.value)
        else:
            assert a == b
    assert again[0].rows[1] == (T0 + timedelta(hours=1), 1.0)
    assert again[0].rows[0][0].tzinfo is UTC
    assert read.runs("s::runs", metadata={"nested": {"k": [1]}}) == [ok]


def test_runs_stay_in_order_across_reopenings(tmp_path):
    backend = FileBackend(tmp_path)
    backend.define_study("s")
    first = [backend.start_run("s", {}) for _ in range(3)]
    backend = FileBackend(tmp_path)
    later = [backend.start_run("s", {}) for _ in range(2)]
    assert [r.run_id for r in FileBackend(tmp_path).runs("s")] == first + later


def test_a_run_cut_short_keeps_what_it_recorded(tmp_path):
    # A crash: the process stops before the run finishes.
    backend = FileBackend(tmp_path)
    backend.define_study("s")
    backend.declare("s", "x", role="output", kind="scalar")
    backend.declare("s", "y", role="output", kind="scalar")
    run_id = backend.start_run("s", {})
    backend.record(run_id, "x", 1.0)
    reopened = FileBackend(tmp_path)
    [run] = reopened.runs("s")
    assert run.status == "running"
    assert [r.value for r in reopened.records("s", run_id)] == [1.0]
    reopened.record(run_id, "y", 2.0)  # this process can still finish it
    reopened.finish_run(run_id)
    assert FileBackend(tmp_path).runs("s")[0].status == "succeeded"


def test_leftovers_of_a_crash_are_ignored_or_cleared(tmp_path):
    backend = filled(tmp_path)
    runs = next((tmp_path / "studies").iterdir()) / "runs"
    (runs / "no-run-json").mkdir()  # cut short before run.json was written
    (runs / ".deleting-abc").mkdir()  # a deletion cut short
    (tmp_path / "studies" / ".deleting-def").mkdir()
    (tmp_path / "studies" / "no-study-json").mkdir()
    reopened = FileBackend(tmp_path)
    assert reopened.runs("s::runs") == backend.runs("s::runs")
    assert reopened.studies() == ["s::runs"]
    assert not (runs / ".deleting-abc").exists()
    assert not (tmp_path / "studies" / ".deleting-def").exists()


def test_writes_leave_no_temporary_files(tmp_path):
    filled(tmp_path)
    assert not [p for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]


def test_deleting_removes_the_files(tmp_path):
    backend = filled(tmp_path)
    [ok, bad] = backend.runs("s::runs")
    backend.delete_runs("s::runs", [ok.run_id])
    assert not list(tmp_path.rglob(ok.run_id))
    assert FileBackend(tmp_path).runs("s::runs") == [bad]
    backend.delete_study("s::runs")
    assert list((tmp_path / "studies").iterdir()) == []
    assert FileBackend(tmp_path).studies() == []


def test_names_that_differ_only_in_case_get_their_own_folders(tmp_path):
    backend = FileBackend(tmp_path)
    for name in ("Study", "study", "CON", "a/b::runs"):
        backend.define_study(name)
    assert len(list((tmp_path / "studies").iterdir())) == 4
    assert FileBackend(tmp_path).studies() == sorted(
        ["Study", "study", "CON", "a/b::runs"]
    )


def test_a_folder_holding_something_else_is_refused(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(StorageError, match="isn't empty"):
        FileBackend(tmp_path)


def test_a_newer_layout_is_refused(tmp_path):
    FileBackend(tmp_path)
    (tmp_path / MARKER).write_text(
        json.dumps({"format": "flexorch-files", "version": 99})
    )
    with pytest.raises(StorageError, match="version 99"):
        FileBackend(tmp_path)


def test_metadata_must_be_json(tmp_path):
    backend = FileBackend(tmp_path)
    backend.define_study("s")
    with pytest.raises(StorageError, match="Can't store"):
        backend.start_run("s", {"when": datetime(2025, 1, 1)})
    assert backend.runs("s") == []


def test_a_timeseries_value_must_be_a_number(tmp_path):
    backend = FileBackend(tmp_path)
    backend.define_study("s")
    backend.declare("s", "v", role="output", kind="timeseries")
    run_id = backend.start_run("s", {})
    with pytest.raises(StorageError, match="isn't a number"):
        backend.record(run_id, "v", [(T0, "high")])
    backend.record(run_id, "v", [(T0, 1), (T0 + timedelta(hours=1), True)])
    assert backend.records("s", run_id)[0].rows[0] == (T0, 1.0)


def test_a_study_runs_and_reads_back_in_a_new_orchestrator(tmp_path):
    from flexorchestrator import Orchestrator
    from flexorchestrator.study.tests.ray_hooks import HOOKS

    study = {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [{"type": "variable", "name": "v", "path": "p.v", "values": [1, 2]}],
        "export": {"exclude": ["pid", "threads"]},
    }
    with Orchestrator(FileBackend(tmp_path / "store"), environ={}) as orch:
        orch.create_study("s")
        orch.store_model_spec("base", {"value": 1.0})
        assert orch.execute_study(study).counts() == {"succeeded": 2}
        before = {r.tag: r.scalars() for r in orch.stored_model_results()}
    with Orchestrator(FileBackend(tmp_path / "store"), study="s", environ={}) as orch:
        assert {r.tag: r.scalars() for r in orch.stored_model_results()} == before
        assert orch.execute_study({**study, "on_existing": "skip"}).counts() == {
            "skipped": 2
        }
        orch.export_study(tmp_path / "export")
    assert (tmp_path / "export" / "study.json").is_file()
