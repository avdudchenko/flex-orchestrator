"""Stored specs, their files, building from them, and results linked to them.

PLAN.md D2 and D10 (``<s>::specs``). Each test runs on memory and files (unit
tier); the tariff rebuild also runs on the shared Acquirium (component tier).
"""

import copy
import shutil
from importlib.resources import files

import pyomo.environ as pyo
import pytest

from flex_orchestrator.errors import SpecError, TagExistsError, TagNotFoundError
from flex_orchestrator.testing import DEMO_SPEC, LINEAR_POWER

pytest.importorskip("flexops")

TARIFF = files("flex_orchestrator.testing") / "data" / "tariff_tou_demo.csv"
COSTING = 2  # DEMO_SPEC's costing element
KEYS = ["costing.aggregate_operating_cost", "demo.pump.power_electrical"]


def tariff_spec(path="tariff.csv"):
    spec = copy.deepcopy(DEMO_SPEC)
    costing = spec["elements"][COSTING]
    del costing["energy_prices"]
    costing["tariff_source"] = path
    return spec


@pytest.fixture
def tariff_dir(tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    shutil.copy(TARIFF, folder / "tariff.csv")
    return folder


def fingerprint(model):
    """Every constraint body summed with every variable at 1: a fingerprint
    of the model's coefficients (tariff rates, prices) that needs no solver."""
    for var in model.component_data_objects(pyo.Var, descend_into=True):
        var.set_value(1, skip_validation=True)
    return sum(
        pyo.value(c.body)
        for c in model.component_data_objects(pyo.Constraint, active=True)
    )


FAKE = "flex_orchestrator.testing:fake_solve"


def test_store_and_read_a_model_spec(orch):
    record = orch.store_model_spec("base", DEMO_SPEC)
    assert record.spec_format == "flex-pse.flowsheet/0.1.0"
    assert record.assets == {}
    back = orch.model_spec("base")
    assert back == record
    with pytest.raises(TagExistsError):
        orch.store_model_spec("base", DEMO_SPEC)
    with pytest.raises(TagNotFoundError):
        orch.model_spec("nope")
    assert [r.tag for r in orch.stored_specs()] == ["base"]


@pytest.mark.on_acquirium
def test_a_spec_with_a_tariff_rebuilds_from_anywhere(
    orch, tariff_dir, tmp_path, monkeypatch
):
    """The M1 exit: stored with its tariff, rebuilt elsewhere, the same model."""
    record = orch.store_model_spec("base", tariff_spec(), base_dir=tariff_dir)
    [(pointer, ref)] = record.assets.items()
    assert pointer == f"/elements/{COSTING}/tariff_source"
    assert record.spec["elements"][COSTING]["tariff_source"] == ref.name
    assert ref.name.endswith("-tariff.csv") and ref.size == TARIFF.stat().st_size

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the tariff isn't here
    rebuilt = orch.build("base")
    monkeypatch.chdir(tariff_dir)
    from flex_orchestrator.flexpse import build

    direct = build(tariff_spec())
    assert fingerprint(rebuilt) == pytest.approx(fingerprint(direct))
    path = tariff_dir / "tariff.csv"
    # The demo's six hours are all off-peak.
    path.write_text(path.read_text().replace(",0.09,", ",0.12,"))
    assert fingerprint(build(tariff_spec())) != pytest.approx(fingerprint(direct))


def test_editing_the_tariff_changes_the_hash(orch, tariff_dir):
    first = orch.store_model_spec("v1", tariff_spec(), base_dir=tariff_dir)
    same = orch.store_model_spec("v1_again", tariff_spec(), base_dir=tariff_dir)
    path = tariff_dir / "tariff.csv"
    path.write_text(path.read_text().replace("0.18", "0.25"))
    edited = orch.store_model_spec("v2", tariff_spec(), base_dir=tariff_dir)
    assert first.spec_hash == same.spec_hash != edited.spec_hash
    assert first.spec != edited.spec  # the stored name carries the content hash


def test_a_missing_file_stores_nothing(orch, tmp_path):
    with pytest.raises(SpecError, match="Can't read the file"):
        orch.store_model_spec("base", tariff_spec(), base_dir=tmp_path)
    assert orch.stored_specs() == []


def test_source_data_files(orch, tmp_path, monkeypatch):
    prices = [0.1, 0.2, 0.3, 0.1, 0.2, 0.3]
    (tmp_path / "prices.json").write_text(str(prices))
    spec = copy.deepcopy(DEMO_SPEC)
    inline = copy.deepcopy(DEMO_SPEC)
    spec["elements"][COSTING]["energy_prices"]["electrical"]["value"] = {
        "$source": "prices.json"
    }
    inline["elements"][COSTING]["energy_prices"]["electrical"]["value"] = prices
    monkeypatch.chdir(tmp_path)  # a dict spec's paths resolve here, as in flex-pse
    record = orch.store_model_spec("base", spec)
    [pointer] = record.assets
    assert pointer.endswith("/value/$source")
    (tmp_path / "prices.json").unlink()
    from flex_orchestrator.flexpse import build

    assert fingerprint(orch.build("base")) == pytest.approx(fingerprint(build(inline)))


def test_materialize_spec(orch, tariff_dir, tmp_path):
    orch.store_model_spec("base", tariff_spec(), base_dir=tariff_dir)
    spec = orch.materialize_spec("base", tmp_path / "replay")
    path = spec["elements"][COSTING]["tariff_source"]
    assert open(path).read() == TARIFF.read_text()


def test_apply_surrogate_and_store_the_derived_spec(orch, tariff_dir):
    orch.store_model_spec("base", tariff_spec(), base_dir=tariff_dir)
    surrogate = orch.store_surrogate_spec("pump_linear", LINEAR_POWER)
    assert surrogate.spec_format == "flex-pse.surrogate"
    spec = orch.apply_surrogate("base", unit="demo.pump", surrogate="pump_linear")
    assert any(e["kind"] == "surrogate" for e in spec["elements"])
    with pytest.raises(SpecError, match="parent="):
        orch.store_model_spec("with_surrogate_orphan", spec)
    derived = orch.store_model_spec("with_surrogate", spec, parent="base")
    assert derived.parent_tag == "base"
    assert derived.assets == orch.model_spec("base").assets  # the same stored file
    # It builds without the tariff file, and the surrogate (0.6 kWh/m^3)
    # changes the model.
    assert fingerprint(orch.build("with_surrogate")) != pytest.approx(
        fingerprint(orch.build("base"))
    )


def test_the_kinds_are_kept_apart(orch):
    with pytest.raises(SpecError, match="stored as a surrogate_spec"):
        orch.store_model_spec("s", LINEAR_POWER)
    with pytest.raises(SpecError, match="stored as a model_spec"):
        orch.store_surrogate_spec("m", DEMO_SPEC)
    orch.store_surrogate_spec("same_tag", LINEAR_POWER)
    orch.store_model_spec("same_tag", DEMO_SPEC)  # tags are unique per kind
    with pytest.raises(TagNotFoundError):
        orch.store_model_spec("x", DEMO_SPEC, parent="nope")
    with pytest.raises(ValueError, match="kind must be"):
        orch.stored_specs("result")
    assert [r.tag for r in orch.stored_specs("surrogate_spec")] == ["same_tag"]


def test_delete_and_replace_a_spec(orch):
    orch.store_model_spec("base", DEMO_SPEC)
    edited = copy.deepcopy(DEMO_SPEC)
    edited["elements"][COSTING]["energy_prices"]["electrical"]["value"] = 0.2
    new = orch.store_model_spec("base", edited, replace=True)
    assert orch.model_spec("base").spec_hash == new.spec_hash
    assert orch.delete("model_spec", "base") == 1
    with pytest.raises(TagNotFoundError):
        orch.model_spec("base")


def test_a_result_built_from_a_spec_links_to_it(orch):
    record = orch.store_model_spec("base", DEMO_SPEC)
    model = orch.build("base", **{"demo.pump.energy_intensity": 0.6})
    orch.solve_and_store("r1", model, keys=KEYS, solve=FAKE)
    result = orch.result("r1")
    assert result.spec_tag == "base"
    assert result.metadata["spec_hash"] == record.spec_hash
    assert result.spec() == record
    assert result.overrides == {"demo.pump.energy_intensity": 0.6}
    inputs = {
        r.label: r.value
        for r in orch.backend.records(f"{orch.study}::runs", result.run_id)
        if r.label in ("model_spec_ref", "overrides")
    }
    assert inputs == {
        "model_spec_ref": {"tag": "base", "spec_hash": record.spec_hash},
        "overrides": {"demo.pump.energy_intensity": 0.6},
    }
    # The run hash tells runs with other overrides apart (D9 resume).
    other = orch.build("base")
    orch.solve_and_store("r2", other, keys=KEYS, solve=FAKE)
    assert orch.result("r2").metadata["run_hash"] != result.metadata["run_hash"]


def test_a_result_names_its_spec_explicitly(orch, demo_model):
    orch.store_model_spec("base", DEMO_SPEC)
    orch.solve_and_store("r1", demo_model, keys=KEYS, spec="base", solve=FAKE)
    assert orch.result("r1").spec_tag == "base"
    with pytest.raises(TagNotFoundError):
        orch.solve_and_store("r2", demo_model, keys=KEYS, spec="nope", solve=FAKE)
    orch.solve_and_store("r3", demo_model, keys=KEYS, solve=FAKE)
    assert orch.result("r3").spec_tag is None
    assert orch.result("r3").overrides == {}
    with pytest.raises(TagNotFoundError, match="records no model spec"):
        orch.result("r3").spec()


def test_a_taken_tag_is_refused_before_reading_files(orch, tariff_dir, tmp_path):
    base = orch.store_model_spec("base", tariff_spec(), base_dir=tariff_dir)
    with pytest.raises(TagExistsError):
        orch.store_model_spec("base", base.spec, base_dir=tmp_path)


@pytest.mark.unit
def test_a_study_run_reads_each_spec_once(monkeypatch):
    from flex_orchestrator import orchestrator as module
    from flex_orchestrator.testing import memory_orchestrator

    orch = memory_orchestrator(study="s")
    orch.store_model_spec("a", {"value": 1})
    reads = []
    real = module.spec_store.read
    monkeypatch.setattr(
        module.spec_store, "read", lambda *a: reads.append(a) or real(*a)
    )
    with orch._caching_specs():
        assert orch.model_spec("a") == orch.model_spec("a")
        assert len(reads) == 1
        orch.store_model_spec("b", {"value": 2}, parent="a")  # parent from the cache
        assert orch.model_spec("b").spec == {"value": 2} and len(reads) == 1
        orch.delete("model_spec", "b")
        with pytest.raises(LookupError):
            orch.model_spec("b")
    orch.model_spec("a")
    assert len(reads) == 2  # outside the block, reads go to the store again
