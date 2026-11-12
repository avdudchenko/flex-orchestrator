"""Specs as data: normalising, hashing, formats and SpecRecord."""

import math

import pytest
from pydantic import BaseModel

from flexorchestrator import specs
from flexorchestrator.errors import SpecError
from flexorchestrator.specs import (
    SpecRecord,
    canonical_json,
    check_tag,
    detect_format,
    run_hash,
    spec_hash,
    to_json_spec,
    validate_spec,
)

pytestmark = pytest.mark.unit

FLOWSHEET = {"schema_version": "0.1.1", "name": "demo", "elements": []}
SURROGATE = {"surrogate_type": "multilinear", "data": {}}


class Point(BaseModel):
    x: float
    tags: tuple[str, ...] = ()


# to_json_spec


def test_to_json_spec_returns_an_independent_copy():
    spec = {"a": {"b": [1, 2]}}
    data = to_json_spec(spec)
    spec["a"]["b"].append(3)
    assert data == {"a": {"b": [1, 2]}}


def test_to_json_spec_dumps_pydantic_models():
    assert to_json_spec(Point(x=1, tags=("a",))) == {"x": 1.0, "tags": ["a"]}


def test_to_json_spec_normalises_like_json():
    assert to_json_spec({"t": (1, 2), 3: "x"}) == {"t": [1, 2], "3": "x"}


@pytest.mark.parametrize("spec", [[1, 2], "spec", None])
def test_to_json_spec_rejects_non_objects(spec):
    with pytest.raises(SpecError, match="must be a JSON object"):
        to_json_spec(spec)


@pytest.mark.parametrize("value", [{1, 2}, object(), math.nan, math.inf])
def test_to_json_spec_rejects_non_json_values(value):
    with pytest.raises(SpecError, match="not JSON-serialisable"):
        to_json_spec({"v": value})


# canonical JSON and hashes


def test_canonical_json_sorts_keys_and_drops_whitespace():
    assert (
        canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'
    )


def test_canonical_json_writes_integral_floats_as_ints():
    assert canonical_json({"a": 1.0, "b": 0.5, "c": [2.0, True]}) == (
        '{"a":1,"b":0.5,"c":[2,true]}'
    )


def test_canonical_json_keeps_unicode():
    assert canonical_json({"unit": "m³"}) == '{"unit":"m³"}'


def test_spec_hash_ignores_key_order_and_int_float():
    assert spec_hash({"a": 1, "b": {"c": 2.0}}) == spec_hash({"b": {"c": 2}, "a": 1.0})


def test_spec_hash_sees_values_and_list_order():
    base = spec_hash({"a": [1, 2]})
    assert spec_hash({"a": [2, 1]}) != base
    assert spec_hash({"a": [1, 2.5]}) != base
    assert spec_hash({"a": [1, 2], "b": None}) != base


def test_spec_hash_is_pinned():
    # Stored hashes drive resume: if this changes, every stored run's hash
    # stops matching. Never update this value without a migration plan.
    assert spec_hash({"b": [1, 0.5], "a": "x"}) == (
        "b0ffca5db2f8b2b87ae0937a7a17c59c822208bb02037499d96209611247aeb2"
    )


def test_run_hash_depends_on_spec_and_overrides():
    base = run_hash("abc", {"x": 1, "y": 2})
    assert run_hash("abc", {"y": 2.0, "x": 1}) == base
    assert run_hash("abc", {"x": 1, "y": 3}) != base
    assert run_hash("abd", {"x": 1, "y": 2}) != base
    assert run_hash("abc") == run_hash("abc", {})


# formats, validation, tags


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (FLOWSHEET, "flex-pse.flowsheet/0.1.1"),
        (SURROGATE, "flex-pse.surrogate"),
        ({"anything": 1}, "json"),
    ],
)
def test_detect_format(spec, expected):
    assert detect_format(spec) == expected


def test_validate_spec_skips_formats_without_validator():
    assert validate_spec({"anything": 1}, "json") is None


def test_validate_spec_skips_when_library_missing(monkeypatch):
    monkeypatch.setitem(
        specs.VALIDATORS, "custom", ("no_such_module", "no_such_module:validate")
    )
    assert validate_spec({}, "custom/1") is None


def test_validate_spec_runs_validator(monkeypatch):
    seen = []

    def validator(spec):
        seen.append(spec)
        return {**spec, "default": True}

    monkeypatch.setitem(specs.VALIDATORS, "custom", ("json", "pkg:validate"))
    monkeypatch.setattr(specs, "import_hook", lambda ref: validator)
    assert validate_spec({"a": 1}, "custom/1") == {"a": 1, "default": True}
    assert seen == [{"a": 1}]
    record = SpecRecord.create("model_spec", "m", {"a": 1}, spec_format="custom/1")
    assert record.spec == {"a": 1, "default": True}
    assert record.spec_hash == spec_hash({"a": 1, "default": True})


@pytest.mark.parametrize(
    "tag", ["base", "model_with_surrogate_biogas_h1", "a.b-c", "1"]
)
def test_check_tag_accepts(tag):
    assert check_tag(tag) == tag


@pytest.mark.parametrize("tag", ["", "_x", "has space", "a/b", "a:b", "x" * 129, None])
def test_check_tag_rejects(tag):
    with pytest.raises(SpecError, match="is not valid"):
        check_tag(tag)


# SpecRecord


def test_create_fills_format_hash_and_metadata():
    record = SpecRecord.create("model_spec", "base", {"a": 1}, parent_tag="root")
    assert record.spec == {"a": 1}
    assert record.spec_format == "json"
    assert record.spec_hash == spec_hash({"a": 1})
    assert record.metadata() == {
        "flexorch_kind": "model_spec",
        "flexorch_tag": "base",
        "spec_hash": record.spec_hash,
        "spec_format": "json",
        "parent_tag": "root",
    }


def test_metadata_omits_missing_parent():
    assert "parent_tag" not in SpecRecord.create("model_spec", "b", {}).metadata()


def test_create_detects_format_and_respects_explicit_one():
    record = SpecRecord.create("surrogate_spec", "s", SURROGATE, validate=False)
    assert record.spec_format == "flex-pse.surrogate"
    custom = SpecRecord.create("model_spec", "m", {"a": 1}, spec_format="mylib/2")
    assert custom.spec_format == "mylib/2"


def test_create_rejects_kind_that_does_not_match_format():
    with pytest.raises(SpecError, match="stored as a surrogate_spec, not a model_spec"):
        SpecRecord.create("model_spec", "s", SURROGATE, validate=False)


def test_create_rejects_bad_tags():
    with pytest.raises(SpecError):
        SpecRecord.create("model_spec", "bad tag", {})
    with pytest.raises(SpecError):
        SpecRecord.create("model_spec", "ok", {}, parent_tag="bad tag")


def test_create_skips_validation_when_asked(monkeypatch):
    monkeypatch.setattr(
        "flexorchestrator.specs.validate_spec",
        lambda spec, fmt: pytest.fail("validated"),
    )
    SpecRecord.create("model_spec", "m", FLOWSHEET, validate=False)


def test_record_round_trips_through_json():
    record = SpecRecord.create("model_spec", "base", {"a": [1, 2]}, parent_tag="p")
    again = SpecRecord.model_validate_json(record.model_dump_json())
    assert again == record
    again.verify()


def test_verify_catches_an_edited_spec():
    record = SpecRecord.create("model_spec", "base", {"a": 1})
    edited = record.model_copy(update={"spec": {"a": 2}})
    with pytest.raises(SpecError, match="doesn't match its hash"):
        edited.verify()


def test_record_is_frozen():
    record = SpecRecord.create("model_spec", "base", {})
    with pytest.raises(ValueError):
        record.tag = "other"
