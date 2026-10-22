"""The storage contract, run against every backend (PLAN.md §7).

Each test runs on the memory backend (unit tier) and on a local Acquirium
(component tier), so the memory backend can't drift from Acquirium.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from flex_orchestrator.errors import StorageError

T0 = datetime(2025, 7, 8)


@pytest.fixture
def study(backend, study_name):
    backend.define_study(study_name)
    return study_name


def hours(n):
    return [T0 + timedelta(hours=i) for i in range(n)]


def test_studies(backend, study_name):
    assert not backend.has_study(study_name)
    backend.define_study(study_name)
    backend.define_study(study_name)  # idempotent, as in Acquirium
    assert backend.has_study(study_name)


def test_declare_is_idempotent_and_keeps_its_shape(backend, study):
    first = backend.declare(
        study,
        "p.power",
        role="output",
        kind="timeseries",
        unit="KiloW",
        metadata={"pyomo_unit": "kW", "component": "p.power"},
    )
    assert (first.kind, first.unit, first.metadata["pyomo_unit"]) == (
        "timeseries",
        "KiloW",
        "kW",
    )
    again = backend.declare(
        study,
        "p.power",
        role="output",
        kind="timeseries",
        unit="KiloW",
        metadata={"pyomo_unit": "kW", "component": "p.power"},
    )
    assert again == first == backend.variable(study, "p.power")
    assert backend.variable(study, "nope") is None


@pytest.mark.parametrize(
    "change",
    [{"unit": "W"}, {"kind": "scalar"}, {"metadata": {"pyomo_unit": "W"}}],
)
def test_declaring_differently_raises(backend, study, change):
    shape = {
        "role": "output",
        "kind": "timeseries",
        "unit": "KiloW",
        "metadata": {"pyomo_unit": "kW"},
    }
    backend.declare(study, "p.power", **shape)
    with pytest.raises(StorageError):
        backend.declare(study, "p.power", **{**shape, **change})


def test_none_metadata_is_dropped(backend, study):
    found = backend.declare(
        study,
        "x",
        role="output",
        kind="scalar",
        metadata={"pyomo_unit": None, "a": "b"},
    )
    assert found.metadata == {"a": "b"} and found.unit is None


def test_a_run_records_and_reads_back(backend, study):
    backend.declare(study, "p.power", role="output", kind="timeseries", unit="KiloW")
    backend.declare(study, "x[1]", role="output", kind="scalar")
    backend.declare(study, "outcome", role="output", kind="json")
    run_id = backend.start_run(study, {"flexorch_tag": "a"})
    backend.record(run_id, "p.power", list(zip(hours(3), [1.0, 2.0, 3.0], strict=True)))
    backend.record(run_id, "x[1]", 2.5)
    backend.record(run_id, "outcome", {"status": "ok", "wall_time": None})
    backend.finish_run(run_id)

    records = {r.label: r for r in backend.records(study, run_id)}
    assert list(records) == ["p.power", "x[1]", "outcome"]
    assert records["p.power"].rows == [
        (T0.replace(tzinfo=UTC) + timedelta(hours=i), v)
        for i, v in enumerate([1.0, 2.0, 3.0])
    ]
    assert records["x[1]"].value == 2.5
    assert records["outcome"].value == {"status": "ok", "wall_time": None}


def test_timestamps_come_back_in_utc(backend, study):
    backend.declare(study, "v", role="output", kind="timeseries")
    run_id = backend.start_run(study, {})
    plus2 = timezone(timedelta(hours=2))
    backend.record(run_id, "v", [(datetime(2025, 7, 8, 2, tzinfo=plus2), 1.0)])
    backend.finish_run(run_id)
    [record] = backend.records(study, run_id)
    assert record.rows == [(datetime(2025, 7, 8, tzinfo=UTC), 1.0)]


def test_runs_filter_by_status_and_metadata(backend, study):
    backend.declare(study, "x", role="output", kind="scalar")
    ids = {}
    for tag, ok in [("a", True), ("b", False), ("a2", True)]:
        run_id = backend.start_run(
            study, {"flexorch_kind": "result", "flexorch_tag": tag}
        )
        if ok:
            backend.finish_run(run_id)
        else:
            backend.fail_run(run_id, RuntimeError("infeasible"))
        ids[tag] = run_id
    assert [r.run_id for r in backend.runs(study)] == [ids["a"], ids["b"], ids["a2"]]
    assert [r.run_id for r in backend.runs(study, status="failed")] == [ids["b"]]
    [b] = backend.runs(study, metadata={"flexorch_tag": "b"})
    assert (b.status, b.error["type"], b.error["message"]) == (
        "failed",
        "RuntimeError",
        "infeasible",
    )
    assert b.metadata == {"flexorch_kind": "result", "flexorch_tag": "b"}


def test_writes_need_a_running_run(backend, study):
    backend.declare(study, "x", role="output", kind="scalar")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError):
        backend.record(run_id, "undeclared", 1.0)
    backend.finish_run(run_id)
    with pytest.raises(StorageError):
        backend.record(run_id, "x", 1.0)
    with pytest.raises(StorageError):
        backend.finish_run(run_id)


def test_nan_is_rejected(backend, study):
    backend.declare(study, "x", role="output", kind="scalar")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError):
        backend.record(run_id, "x", float("nan"))
    backend.finish_run(run_id)


def test_unknown_study_or_run(backend, study):
    with pytest.raises(StorageError):
        backend.variable("no-such-study", "x")
    with pytest.raises(StorageError):
        backend.records(study, "00000000-0000-0000-0000-000000000000")


def test_json_variables_have_no_unit(backend, study):
    with pytest.raises(StorageError, match="can't have a unit"):
        backend.declare(study, "outcome", role="output", kind="json", unit="KiloW")


def test_nan_in_a_timeseries_is_rejected(backend, study):
    backend.declare(study, "v", role="output", kind="timeseries")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError):
        backend.record(
            run_id, "v", list(zip(hours(2), [float("nan"), 1.0], strict=True))
        )
    backend.finish_run(run_id)


def test_variables_lists_every_declaration(backend, study):
    backend.declare(
        study, "a/x", role="input", kind="timeseries", metadata={"column": "x"}
    )
    backend.declare(study, "manifest", role="input", kind="json")
    found = {v.label: v for v in backend.variables(study)}
    assert set(found) == {"a/x", "manifest"}
    assert found["a/x"].metadata == {"column": "x"}


def test_records_can_read_only_some_labels(backend, study):
    for label in ("a", "b", "c"):
        backend.declare(study, label, role="output", kind="scalar")
    run_id = backend.start_run(study, {})
    for i, label in enumerate(("a", "b", "c")):
        backend.record(run_id, label, float(i))
    backend.finish_run(run_id)
    assert [r.label for r in backend.records(study, run_id, labels=["c", "a"])] == [
        "a",
        "c",
    ]


def test_a_timeseries_row_needs_a_value(backend, study):
    backend.declare(study, "v", role="output", kind="timeseries")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError):
        backend.record(run_id, "v", list(zip(hours(2), [None, 1.0], strict=True)))
    backend.finish_run(run_id)


def test_a_table_round_trips(backend, study):
    import polars as pl

    frame = pl.DataFrame(
        {
            "time": pl.Series([T0.replace(tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")),
            "a.b[c]": pl.Series([None], dtype=pl.Float64),
            "x": [1.5],
        }
    )
    backend.declare(study, "values", role="output", kind="table")
    run_id = backend.start_run(study, {})
    backend.record(run_id, "values", frame)
    backend.finish_run(run_id)
    [record] = backend.records(study, run_id)
    assert record.kind == "table"
    assert record.value.equals(frame)
    with pytest.raises(StorageError, match="can't have a unit"):
        backend.declare(study, "other", role="output", kind="table", unit="KiloW")


def test_a_table_must_be_a_polars_frame(backend, study):
    backend.declare(study, "values", role="output", kind="table")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError):
        backend.record(run_id, "values", {"x": [1.0]})
    backend.finish_run(run_id)


def test_delete_runs(backend, study):
    backend.declare(study, "x", role="output", kind="scalar")
    kept, gone, running = (backend.start_run(study, {"n": n}) for n in "abc")
    for run_id in (kept, gone):
        backend.record(run_id, "x", 1.0)
        backend.finish_run(run_id)
    with pytest.raises(StorageError):
        backend.delete_runs(study, [gone, running])  # one is still running
    assert len(backend.runs(study)) == 3
    with pytest.raises(StorageError):
        backend.delete_runs(study, ["00000000-0000-0000-0000-000000000000"])
    backend.delete_runs(study, [gone])
    assert [r.run_id for r in backend.runs(study)] == [kept, running]
    backend.delete_runs(study, [])
    backend.finish_run(running)


def test_delete_study(backend, study):
    backend.declare(study, "x", role="output", kind="scalar")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError):
        backend.delete_study(study)  # a run is still running
    backend.finish_run(run_id)
    backend.delete_study(study)
    assert not backend.has_study(study)
    backend.define_study(study)
    assert backend.variable(study, "x") is None


def test_a_file_round_trips(backend, study):
    from flex_orchestrator.backend import FileValue

    data = b"utility,type\nelectric,energy\n\x00\xff"
    backend.declare(study, "assets/abc-tariff.csv", role="input", kind="file")
    run_id = backend.start_run(study, {})
    backend.record(run_id, "assets/abc-tariff.csv", FileValue("tariff.csv", data))
    backend.finish_run(run_id)
    [record] = backend.records(study, run_id)
    assert record.kind == "file"
    assert record.value == FileValue("tariff.csv", data)
    with pytest.raises(StorageError, match="can't have a unit"):
        backend.declare(study, "other", role="input", kind="file", unit="KiloW")


def test_a_file_must_be_a_file_value(backend, study):
    backend.declare(study, "f", role="input", kind="file")
    run_id = backend.start_run(study, {})
    with pytest.raises(StorageError, match="FileValue"):
        backend.record(run_id, "f", b"raw bytes")
    backend.finish_run(run_id)


def test_studies_lists_every_study(backend, study_name):
    names = [f"{study_name}-a", f"{study_name}::runs"]
    for name in names:
        backend.define_study(name)
    assert set(names) <= set(backend.studies())
    assert backend.studies() == sorted(backend.studies())
