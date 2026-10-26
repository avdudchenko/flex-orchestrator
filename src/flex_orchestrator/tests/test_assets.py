"""Finding, collecting and materializing the files a spec references (PLAN.md D2)."""

import hashlib

import pytest

from flex_orchestrator.assets import (
    AssetRef,
    collect_assets,
    find_assets,
    flexpse_assets,
    get_pointer,
    materialize_spec,
    set_pointer,
)
from flex_orchestrator.errors import SpecError
from flex_orchestrator.specs import SpecRecord, record_hash, spec_hash

pytestmark = pytest.mark.unit

FLOWSHEET = "flex-pse.flowsheet/0.1.0"


def costing(**fields):
    return {"schema_version": "0.1.0", "elements": [{"kind": "costing", **fields}]}


def test_flexpse_tariff_sources_of_every_shape():
    spec = costing(
        tariff_source={"electric": "e.csv", "gas": ["g1.json", "PG&E-tag"]},
        dr={"events_source": "events.csv"},
    )
    assert flexpse_assets(spec) == {
        "/elements/0/tariff_source/electric": "e.csv",
        "/elements/0/tariff_source/gas/0": "g1.json",
        "/elements/0/dr/events_source": "events.csv",
    }


def test_flexpse_source_refs_anywhere():
    spec = {
        "schema_version": "0.1.0",
        "elements": [
            {
                "kind": "costing",
                "energy_prices": {"e/x": {"value": {"$source": "p.json"}}},
            },
            {"kind": "fix", "value": {"$source": "fix.json.gz"}},
            {"kind": "unit", "name": "u", "tariff_source": "not costing.csv"},
            "not an element",
        ],
    }
    assert flexpse_assets(spec) == {
        "/elements/0/energy_prices/e~1x/value/$source": "p.json",
        "/elements/1/value/$source": "fix.json.gz",
    }


def test_find_assets_only_searches_flowsheets():
    spec = costing(tariff_source="t.csv")
    assert find_assets(spec, "json") == {}
    assert find_assets(spec, FLOWSHEET) == {"/elements/0/tariff_source": "t.csv"}


def test_explicit_pointers():
    spec = {"inputs": {"prices": "prices.csv", "n": 3}}
    assert find_assets(spec, "json", ["/inputs/prices"]) == {
        "/inputs/prices": "prices.csv"
    }
    with pytest.raises(SpecError, match="names a int"):
        find_assets(spec, "json", ["/inputs/n"])
    with pytest.raises(SpecError, match="doesn't resolve"):
        find_assets(spec, "json", ["/inputs/missing"])


def test_pointers():
    doc = {"a~b": [{"c/d": 1}]}
    assert get_pointer(doc, "/a~0b/0/c~1d") == 1
    set_pointer(doc, "/a~0b/0/c~1d", 2)
    assert doc == {"a~b": [{"c/d": 2}]}
    set_pointer(doc, "/a~0b/0", "x")
    assert doc == {"a~b": ["x"]}
    assert get_pointer(doc, "") is doc
    with pytest.raises(SpecError, match="must start"):
        get_pointer(doc, "a")
    with pytest.raises(SpecError, match="whole spec"):
        set_pointer(doc, "", 1)
    with pytest.raises(SpecError, match="doesn't resolve"):
        set_pointer(doc, "/a~0b/5", 1)


def test_collect_rewrites_paths_to_stored_names(tmp_path):
    (tmp_path / "tariff.csv").write_bytes(b"rates")
    spec = costing(tariff_source="tariff.csv")
    digest = hashlib.sha256(b"rates").hexdigest()
    rewritten, refs, files = collect_assets(spec, flexpse_assets(spec), tmp_path)
    name = f"assets/{digest[:12]}-tariff.csv"
    assert rewritten["elements"][0]["tariff_source"] == name
    assert spec["elements"][0]["tariff_source"] == "tariff.csv"  # not changed
    assert refs == {
        "/elements/0/tariff_source": AssetRef(name=name, sha256=digest, size=5)
    }
    assert files == {name: b"rates"}


def test_collect_reads_absolute_paths_and_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / "t.csv").write_bytes(b"x")
    spec = costing(tariff_source=str(tmp_path / "t.csv"))
    assert collect_assets(spec, flexpse_assets(spec))[2]
    monkeypatch.chdir(tmp_path)
    spec = costing(tariff_source="t.csv")
    assert collect_assets(spec, flexpse_assets(spec))[2]


def test_a_missing_file_is_an_error(tmp_path):
    spec = costing(tariff_source="nope.csv")
    with pytest.raises(SpecError, match="Can't read the file"):
        collect_assets(spec, flexpse_assets(spec), tmp_path)
    spec = costing(tariff_source="assets/0123456789ab-t.csv")
    with pytest.raises(SpecError, match="parent="):
        collect_assets(spec, flexpse_assets(spec), tmp_path)


def test_known_assets_need_no_file(tmp_path):
    ref = AssetRef(name="assets/0123456789ab-t.csv", sha256="0" * 64, size=1)
    spec = costing(tariff_source=ref.name)
    _, refs, files = collect_assets(
        spec, flexpse_assets(spec), tmp_path, {ref.name: (ref, b"x")}
    )
    assert refs["/elements/0/tariff_source"] == ref
    assert files == {ref.name: b"x"}


def test_materialize_writes_files_and_points_at_them(tmp_path):
    (tmp_path / "t.csv").write_bytes(b"rates")
    spec = costing(tariff_source="t.csv")
    stored, refs, files = collect_assets(spec, flexpse_assets(spec), tmp_path)
    out = tmp_path / "out"
    spec = materialize_spec(stored, refs, files, out)
    path = spec["elements"][0]["tariff_source"]
    assert path.startswith(str(out.resolve()))
    assert open(path, "rb").read() == b"rates"
    with pytest.raises(SpecError, match="doesn't match its hash"):
        materialize_spec(stored, refs, {n: b"tampered" for n in files}, out)
    with pytest.raises(SpecError, match="missing"):
        materialize_spec(stored, refs, {}, out)


def test_the_hash_covers_the_files(tmp_path):
    spec = costing(tariff_source="t.csv")
    hashes = []
    for content in (b"v1", b"v1", b"v2"):
        (tmp_path / "t.csv").write_bytes(content)
        stored, refs, _ = collect_assets(spec, flexpse_assets(spec), tmp_path)
        record = SpecRecord.create(
            "model_spec", "m", stored, validate=False, assets=refs
        )
        record.verify()
        hashes.append(record.spec_hash)
    assert hashes[0] == hashes[1] != hashes[2]
    assert record_hash({"a": 1}) == spec_hash({"a": 1})  # no assets: unchanged


def test_verify_catches_an_edited_asset_hash(tmp_path):
    (tmp_path / "t.csv").write_bytes(b"v1")
    spec = costing(tariff_source="t.csv")
    stored, refs, _ = collect_assets(spec, flexpse_assets(spec), tmp_path)
    record = SpecRecord.create("model_spec", "m", stored, validate=False, assets=refs)
    pointer = "/elements/0/tariff_source"
    edited = record.model_copy(
        update={"assets": {pointer: refs[pointer].model_copy(update={"sha256": "0"})}}
    )
    with pytest.raises(SpecError, match="doesn't match its hash"):
        edited.verify()


def test_a_spec_run_that_fails_to_record_is_failed(memory_backend, monkeypatch):
    from flex_orchestrator import spec_store

    memory_backend.define_study("s::specs")
    record = SpecRecord.create("model_spec", "m", {"a": 1})

    def broken(run_id, label, value):
        raise OSError("disk full")

    monkeypatch.setattr(memory_backend, "record", broken)
    with pytest.raises(OSError):
        spec_store.write(memory_backend, "s::specs", record, {}, {})
    assert [r.status for r in memory_backend.runs("s::specs")] == ["failed"]


def test_a_spec_run_without_its_record(memory_backend):
    from flex_orchestrator import spec_store

    memory_backend.define_study("s::specs")
    run_id = memory_backend.start_run("s::specs", {})
    memory_backend.finish_run(run_id)
    with pytest.raises(SpecError, match="has no spec_record"):
        spec_store.read(memory_backend, "s::specs", run_id)
