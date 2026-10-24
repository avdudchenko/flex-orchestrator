"""Study export and import (PLAN.md D17, the M3 exit).

Each test runs on the memory backend (unit tier) and a local Acquirium
(component tier), with a study holding a spec and its file, a derived spec, a
dataset, a fit and results (one with a series covering part of time, a stale
value and a non-string index).
"""

import gzip
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from flex_orchestrator import schemas
from flex_orchestrator.archive import (
    IMPORTED_FROM,
    SCHEMA,
    load_result,
    read_json,
)
from flex_orchestrator.archive.export import CHECKSUMS
from flex_orchestrator.errors import (
    ArchiveError,
    StudyExistsError,
    TagNotFoundError,
)
from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.hooks import SolveOutcome

pytest.importorskip("flexparameterize")

START = datetime(2025, 7, 8, tzinfo=UTC)
STAMPS = [START + timedelta(hours=h) for h in range(4)]
PROVENANCE = {
    "run_id",
    "started_at",
    "created_at",
    "exported_at",
    IMPORTED_FROM,
    "fit_run_id",
    "study",
    "flexorch_study",
}


def model_data(scale=1.0):
    def series(key, kind, values, stamps=None, index=(), unit="kW", qudt="KiloW"):
        return ExportedSeries(
            key=key,
            kind=kind,
            component=key.partition("[")[0],
            index=list(index),
            values=values,
            timestamps=stamps,
            unit_pyomo=unit,
            unit_qudt=qudt,
            unit_verified=qudt is not None,
            raw_index=tuple(index),
        )

    return ModelData(
        [
            series(
                "plant.power",
                "timeseries",
                [scale * v for v in (1.5, 2.5, 3.5, 0.1)],
                STAMPS,
            ),
            series(
                "plant.startup",
                "timeseries",
                [0.0, None, 1.0],
                STAMPS[1:],
                unit=None,
                qudt=None,
            ),
            series("costing.cost", "scalar", [scale * 12.25], unit="USD", qudt=None),
            series("plant.cap[2]", "scalar", [7.0], index=[2]),
        ]
    )


@pytest.fixture
def study(orch, tmp_path):
    (tmp_path / "site.csv").write_text("hour,price\n0,0.1\n")
    orch.store_model_spec(
        "base",
        {"site_file": "site.csv", "value": 1},
        base_dir=tmp_path,
        assets=["/site_file"],
    )
    base = orch.model_spec("base")
    orch.store_model_spec("derived", {**base.spec, "value": 2}, parent="base")
    time = pd.date_range("2025-06-01", periods=24, freq="h", tz="UTC")
    q = np.linspace(10, 50, 24)
    orch.store_data(
        "ops",
        pd.DataFrame({"q": q, "p": 3 * q + 1}, time),
        units={"q": "m^3/hr", "p": "kW"},
    )
    orch.fit_surrogate(
        tag="fit_lin",
        regressor="multilinear",
        X={"dataset": "ops", "columns": ["q"]},
        y={"dataset": "ops", "columns": ["p"]},
        store_series=True,
    )
    outcome = SolveOutcome(status="ok", termination="optimal", solver="fake")
    orch.store_model_data(
        "r1",
        model_data(),
        outcome,
        spec="derived",
        overrides={"x": 1},
        streams=["plant.power"],
    )
    orch.store_model_data("r2", model_data(2.0))
    return orch


def files_of(folder):
    return sorted(
        str(p.relative_to(folder)).replace("\\", "/")
        for p in folder.rglob("*")
        if p.is_file()
    )


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in PROVENANCE}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def test_the_export_layout_and_schemas(study, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    report = study.export_study(tmp_path / "export")
    folder = report.path
    assert report.counts == {"specs": 3, "data": 1, "fits": 1, "results": 2}
    [asset] = study.model_spec("base").assets.values()
    assert files_of(folder) == sorted(
        [
            CHECKSUMS,
            "study.json",
            "data/ops.json",
            "fits/fit_lin.json",
            "results/r1.json",
            "results/r2.json",
            "specs/models/base.json",
            "specs/models/derived.json",
            "specs/surrogates/fit_lin.json",
            f"specs/{asset.name}",
        ]
    )
    published = schemas.all_schemas()
    for relative, name in [
        ("study.json", "study"),
        ("data/ops.json", "dataset"),
        ("fits/fit_lin.json", "fit"),
        ("results/r1.json", "result"),
        ("specs/models/base.json", "spec"),
        ("specs/surrogates/fit_lin.json", "spec"),
    ]:
        payload = read_json(folder / relative)
        assert payload["schema"] == SCHEMA
        jsonschema.validate(payload, published[name])
    manifest = read_json(folder / "study.json")
    assert manifest["study"] == study.study
    assert manifest["files"] == [
        f for f in files_of(folder) if f not in (CHECKSUMS, "study.json")
    ]
    text = "".join((folder / f).read_text(errors="ignore") for f in files_of(folder))
    assert "password" not in text.lower()
    result = read_json(folder / "results/r1.json")
    assert result["model_spec_ref"]["tag"] == "derived"
    assert result["overrides"] == {"x": 1}
    assert result["streams"] == ["plant.power"]
    assert result["timeseries"]["plant.startup"]["rows"] == [1, 2, 3]
    assert result["timeseries"]["plant.startup"]["values"] == [None, 0.0, None, 1.0]
    assert result["scalars"]["plant.cap[2]"]["index"] == [2]
    assert result["stale"] == ["plant.startup"]
    spec = read_json(folder / "specs/surrogates/fit_lin.json")
    fit = read_json(folder / "fits/fit_lin.json")
    assert spec["metadata"]["fit_run_id"] == fit["run_id"]
    assert len(fit["series"]["actual"]) == 24


def test_load_result_needs_no_database(study, tmp_path):
    folder = study.export_study(tmp_path / "e").path
    loaded = load_result(folder / "results" / "r1.json")
    stored = study.result("r1")
    assert loaded.tag == "r1" and loaded.run_id == stored.run_id
    assert loaded.data().keys() == stored.data().keys()
    for a, b in zip(loaded.data(), stored.data(), strict=True):
        assert (a.values, a.timestamps, a.unit_pyomo, a.unit_qudt, a.index) == (
            b.values,
            b.timestamps,
            b.unit_pyomo,
            b.unit_qudt,
            b.index,
        )
    assert loaded.units == stored.units
    assert loaded.scalars() == stored.scalars()
    assert loaded.to_pandas().equals(stored.to_pandas())
    assert loaded.to_polars().equals(stored.to_polars())
    assert (
        loaded.to_frame("pandas", units={"plant.power": "W"})["plant.power"].iloc[0]
        == 1500
    )
    assert loaded.outcome.termination == "optimal"
    assert loaded.spec_tag == "derived" and loaded.overrides == {"x": 1}
    assert loaded.metadata["flexorch_tag"] == "r1"
    assert load_result(folder / "results" / "r2.json").outcome is None


def test_export_result_alone(study, tmp_path):
    plain = study.export_result("r2", tmp_path / "one" / "r2.json")
    packed = study.export_result("r2", tmp_path / "r2.json.gz")
    assert json.loads(gzip.decompress(packed.read_bytes())) == json.loads(
        plain.read_text()
    )
    assert load_result(packed).scalars()["costing.cost"] == 24.5
    with pytest.raises(TagNotFoundError):
        study.export_result("nope", tmp_path / "x.json")


def test_export_options(study, tmp_path):
    target = tmp_path / "e"
    target.mkdir()
    (target / "keep").write_text("x")
    with pytest.raises(ArchiveError, match="isn't an empty folder"):
        study.export_study(target)
    report = study.export_study(
        target,
        overwrite=True,
        compress=True,
        parts=["results", "specs"],
        tags=["r2", "base"],
    )
    assert report.counts == {"specs": 1, "data": 0, "fits": 0, "results": 1}
    names = files_of(target)
    assert (
        "results/r2.json.gz" in names and "study.json" in names and "keep" not in names
    )
    assert read_json(target / "results" / "r2.json.gz")["tag"] == "r2"
    with pytest.raises(ArchiveError, match="Unknown export parts"):
        study.export_study(tmp_path / "f", parts=["everything"])
    (tmp_path / "file").write_text("x")
    with pytest.raises(ArchiveError, match="isn't an empty folder"):
        study.export_study(tmp_path / "file")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert study.export_study(empty).counts["results"] == 2
    assert not list(tmp_path.glob(".*partial*"))


def test_import_then_export_gives_the_same_files(study, tmp_path):
    first = study.export_study(tmp_path / "a").path
    original = study.study
    report = study.import_study(first, name=original + "-copy")
    assert study.study == original + "-copy"
    assert report.counts == {"specs": 3, "data": 1, "fits": 1, "results": 2}
    assert report.skipped == []
    second = study.export_study(tmp_path / "b").path
    assert files_of(first) == files_of(second)
    for relative in files_of(first):
        if relative.endswith(".json"):
            a, b = read_json(first / relative), read_json(second / relative)
            assert strip(a) == strip(b), relative
        elif relative != CHECKSUMS:
            assert (first / relative).read_bytes() == (second / relative).read_bytes()
    r1 = study.result("r1")
    origin = r1.metadata[IMPORTED_FROM]
    assert origin["study"] == original
    assert report.run_ids[origin["run_id"]] == r1.run_id
    exported = read_json(first / "results/r1.json")
    assert r1.metadata["flexorch_hooks"] == exported["hooks"]
    assert r1.spec().spec_hash == exported["model_spec_ref"]["spec_hash"]
    spec_run = study._latest("fit_lin", "surrogate_spec")
    fit = read_json(second / "fits/fit_lin.json")
    assert spec_run.metadata["fit_run_id"] == fit["run_id"]
    assert study.backend.records(study._runs, r1.run_id, labels=["plant.power"])


def test_importing_into_an_existing_study(study, tmp_path):
    folder = study.export_study(tmp_path / "a").path
    with pytest.raises(StudyExistsError):
        study.import_study(folder)
    study.delete("result", "r2")
    report = study.import_study(folder, on_existing="skip")
    assert report.counts["results"] == 1
    assert "result:r1" in report.skipped and "model_spec:base" in report.skipped
    assert {r.tag for r in study.stored_model_results()} == {"r1", "r2"}


def test_import_checks_every_file(study, tmp_path):
    folder = study.export_study(tmp_path / "a").path
    (folder / "results" / "r2.json").write_text("{}")
    with pytest.raises(ArchiveError, match="doesn't match its checksum"):
        study.import_study(folder, name="x1")
    folder = study.export_study(tmp_path / "b").path
    (folder / "results" / "r2.json").unlink()
    with pytest.raises(ArchiveError, match="missing"):
        study.import_study(folder, name="x2")
    folder = study.export_study(tmp_path / "c").path
    (folder / CHECKSUMS).write_text("")
    with pytest.raises(ArchiveError, match="doesn't list"):
        study.import_study(folder, name="x3")
    (folder / CHECKSUMS).unlink()
    with pytest.raises(ArchiveError, match="has no checksums"):
        study.import_study(folder, name="x4")
    assert "x1" not in study.studies()


@pytest.mark.unit
def test_files_of_another_schema_are_refused(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"schema": "flexorch-export/9"}))
    with pytest.raises(ArchiveError, match="this version reads"):
        read_json(tmp_path / "a.json")
    (tmp_path / "b.json").write_text("[1]")
    with pytest.raises(ArchiveError, match="has schema None"):
        read_json(tmp_path / "b.json")
    with pytest.raises(ArchiveError, match="Can't read"):
        read_json(tmp_path / "missing.json")
    (tmp_path / "c.json").write_text(json.dumps({"schema": SCHEMA, "tag": 1}))
    with pytest.raises(ArchiveError, match="isn't a valid result file"):
        load_result(tmp_path / "c.json")


def test_a_result_whose_spec_changed_is_refused(study, tmp_path):
    folder = study.export_study(tmp_path / "a", parts=["results"]).path
    study.create_study(study.study + "-other")
    study.store_model_spec("derived", {"different": True})
    report = None
    with pytest.raises(ArchiveError, match="has another hash"):
        report = study.import_study(folder, name=study.study, on_existing="skip")
    assert report is None
    # Without the spec at all, the result is imported unlinked.
    fresh = study.import_study(folder, name=study.study + "-unlinked")
    assert fresh.counts["results"] == 2
    assert study.result("r1").spec_tag is None


@pytest.mark.unit
def test_published_schemas_are_current():
    from pathlib import Path

    folder = Path(__file__).parents[4] / "docs" / "schemas"
    for name, schema in schemas.all_schemas().items():
        path = folder / f"{name}.schema.json"
        assert path.read_text(encoding="utf-8") == schemas.render(
            schema
        ), f"{path} is stale; run python -m flex_orchestrator.schemas"


def test_a_failed_export_leaves_nothing_behind(study, tmp_path, monkeypatch):
    from flex_orchestrator.archive import export as module

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")
        yield

    monkeypatch.setattr(module, "result_files", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        study.export_study(tmp_path / "e")
    assert [p.name for p in tmp_path.iterdir()] == ["site.csv"]


def test_shared_files_are_written_once_and_tags_filter_every_part(study, tmp_path):
    base = study.model_spec("base")
    study.store_model_spec(
        "sibling", {**base.spec, "value": 3}, parent="base", assets=["/site_file"]
    )
    assert study.model_spec("sibling").assets == base.assets
    report = study.export_study(tmp_path / "e", tags=["base", "sibling", "ops"])
    assert report.counts == {"specs": 2, "data": 1, "fits": 0, "results": 0}
    [asset] = base.assets.values()
    assert report.files.count(f"specs/{asset.name}") == 1


def test_invalid_files_in_an_export(study, tmp_path):
    import hashlib

    folder = study.export_study(tmp_path / "e").path

    def rewrite(relative, payload):
        (folder / relative).write_text(json.dumps(payload))
        lines = [
            f"{hashlib.sha256((folder / name).read_bytes()).hexdigest()}  {name}\n"
            for name in files_of(folder)
            if name != CHECKSUMS
        ]
        (folder / CHECKSUMS).write_text("".join(lines))

    rewrite("results/r2.json", {"schema": SCHEMA, "tag": "r2"})
    with pytest.raises(ArchiveError, match="results/r2.json is invalid"):
        study.import_study(folder, name="bad-item")
    rewrite("study.json", {"schema": SCHEMA, "study": 1})
    with pytest.raises(ArchiveError, match="study.json is invalid"):
        study.import_study(folder, name="bad-manifest")


def test_a_locked_export_folder_is_renamed_once_let_go(study, tmp_path, monkeypatch):
    # On Windows an antivirus can hold a just-written file for a moment.
    from flex_orchestrator.backend import files as module

    real, calls = module.os.replace, []

    def flaky(source, target):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError("[WinError 5] Access is denied")
        real(source, target)

    monkeypatch.setattr(module.os, "replace", flaky)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    study.export_study(tmp_path / "e")
    assert len(calls) == 3
    assert (tmp_path / "e" / CHECKSUMS).is_file()
