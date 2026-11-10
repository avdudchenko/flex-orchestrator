"""Model and surrogate specs as stored data.

A spec is any JSON object. Storing one as data, not as a live model, is what
lets a run be replayed: the stored spec goes back through the build hook. This
module is format-agnostic and imports neither pyomo nor flex-pse.

- :func:`to_json_spec` turns a dict or pydantic model into a plain JSON dict.
- :func:`spec_hash` is the sha256 of the spec's canonical JSON. Two specs that
  differ only in key order, or in ``1`` versus ``1.0``, hash the same. The hash
  is how ``execute_study`` recognises runs it has already done, so the
  canonical form must never change.
- :func:`detect_format` names the spec's format; :func:`validate_spec` checks
  it with the format's validator when that format's library is installed.
- :class:`SpecRecord` is what a store call writes: the spec with its kind,
  tag, format, hash, parent and the files it references
  (:mod:`flexorchestrator.assets`).
"""

import hashlib
import importlib.util
import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flexorchestrator.assets import AssetRef
from flexorchestrator.errors import SpecError
from flexorchestrator.hooks import import_hook

_log = logging.getLogger(__name__)

SpecKind = Literal["model_spec", "surrogate_spec"]
"""What a stored spec describes."""

FLEXPSE_FLOWSHEET = "flex-pse.flowsheet"
"""str: Format family of a flex-pse flowsheet spec (a ``FlowsheetSpec``)."""

FLEXPSE_SURROGATE = "flex-pse.surrogate"
"""str: Format family of a flex-pse ``SurrogateSpec``."""

FLEXPSE_DESIGN = "flex-pse.design"
"""str: Format of a flex-pse ``DesignSpec``. It has no version part: a design spec's
``schema_version`` is the version of that study, not of the format."""

_DESIGN_FIELDS = frozenset({"base", "years", "months", "capacities"})

GENERIC_JSON = "json"
"""str: Format of any spec no validator knows."""

VALIDATORS: dict[str, tuple[str, str]] = {
    FLEXPSE_FLOWSHEET: ("flexops", "flexorchestrator.flexpse:validate_model_spec"),
    FLEXPSE_SURROGATE: ("flexops", "flexorchestrator.flexpse:validate_surrogate_spec"),
    FLEXPSE_DESIGN: ("flexdesign", "flexorchestrator.flexpse:validate_design_spec"),
}
"""dict: Format family -> (module that must be importable, validator import
string). A validator takes the spec, raises :class:`SpecError` if it is invalid,
and returns it normalised: validated and dumped back to JSON, so defaults are
written out and every way of building the same spec stores (and hashes) the same
JSON."""

_FAMILY_KIND: dict[str, SpecKind] = {
    FLEXPSE_FLOWSHEET: "model_spec",
    FLEXPSE_SURROGATE: "surrogate_spec",
    FLEXPSE_DESIGN: "model_spec",
}

_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}")


def to_json_spec(spec: Any) -> dict[str, Any]:
    """Return ``spec`` as a plain JSON object, independent of the input.

    Args:
        spec: A mapping, or a pydantic model (dumped with ``mode="json"``).

    Returns:
        A new dict holding only JSON types. Tuples become lists and non-string
        keys become strings, as in any JSON round trip.

    Raises:
        SpecError: If ``spec`` isn't a mapping or model, or holds a value JSON
            can't represent (a set, an arbitrary object, NaN or infinity).

    Example:
        >>> to_json_spec({"order": (0, 0, 1)})
        {'order': [0, 0, 1]}
    """
    if isinstance(spec, BaseModel):
        spec = spec.model_dump(mode="json")
    if not isinstance(spec, Mapping):
        raise SpecError(
            "A spec must be a JSON object (a dict or a pydantic model), got "
            f"{type(spec).__name__}."
        )
    try:
        text = json.dumps(spec, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SpecError(
            f"The spec is not JSON-serialisable: {exc}. Convert arrays with "
            ".tolist() and pydantic models with model_dump(mode='json')."
        ) from exc
    return json.loads(text)


def canonical_json(spec: Any) -> str:
    """Return the canonical JSON text that :func:`spec_hash` hashes.

    Keys are sorted, there is no whitespace, and a float with an integral value
    is written as an integer, so ``{"b": 1.0, "a": 2}`` and ``{"a": 2, "b": 1}``
    give the same text. Only the hash uses this form; stored specs keep their
    values as given.

    Args:
        spec: Anything :func:`to_json_spec` accepts.

    Returns:
        The canonical text.

    Raises:
        SpecError: As :func:`to_json_spec`.

    Example:
        >>> canonical_json({"b": 1.0, "a": [2, 0.5]})
        '{"a":[2,0.5],"b":1}'
    """
    return json.dumps(
        _canonical(to_json_spec(spec)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def spec_hash(spec: Any) -> str:
    """Return the sha256 hex digest of the spec's :func:`canonical_json`.

    Args:
        spec: Anything :func:`to_json_spec` accepts.

    Returns:
        64 lowercase hex characters.

    Raises:
        SpecError: As :func:`to_json_spec`.

    Example:
        >>> spec_hash({"a": 1}) == spec_hash({"a": 1.0})
        True
    """
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def run_hash(model_spec_hash: str, overrides: Mapping[str, Any] | None = None) -> str:
    """Return the hash that identifies one run: a spec plus its overrides.

    ``execute_study`` compares it with the runs already stored to skip work
    that succeeded before.

    Args:
        model_spec_hash: The :func:`spec_hash` of the model spec.
        overrides: The run's overrides; ``None`` is the same as ``{}``.

    Returns:
        64 lowercase hex characters.

    Example:
        >>> run_hash("ab", {"x": 1}) == run_hash("ab", {"x": 1.0})
        True
    """
    return spec_hash({"spec_hash": model_spec_hash, "overrides": dict(overrides or {})})


def record_hash(spec: Any, assets: Mapping[str, AssetRef] | None = None) -> str:
    """Return the hash of a stored spec and the files it references.

    Without assets it is :func:`spec_hash` of the spec, unchanged. With assets
    it also covers each file's sha256, so editing a referenced file (a tariff,
    say) changes the hash even though the spec text doesn't.

    Args:
        spec: The stored spec JSON.
        assets: ``{JSON pointer: AssetRef}`` for each file it references.

    Returns:
        The hex sha256.

    Example:
        >>> record_hash({"a": 1}) == spec_hash({"a": 1})
        True
    """
    if not assets:
        return spec_hash(spec)
    return spec_hash(
        {
            "spec": to_json_spec(spec),
            "assets": {pointer: ref.sha256 for pointer, ref in assets.items()},
        }
    )


def normalise_spec(
    kind: SpecKind, spec: Any, spec_format: str | None = None, validate: bool = True
) -> tuple[dict[str, Any], str]:
    """Return a spec as stored JSON and its format, validated when possible.

    Args:
        kind: What the spec describes: ``"model_spec"`` or ``"surrogate_spec"``.
        spec: A mapping or pydantic model.
        spec_format: Its format; detected from the spec when omitted.
        validate: Run the format's validator, when its library is installed.

    Returns:
        ``(the stored JSON, the format)``.

    Raises:
        SpecError: If the spec isn't JSON, its format belongs to the other
            kind, or validation fails.
    """
    data = to_json_spec(spec)
    detected = spec_format is None
    spec_format = spec_format or detect_format(data)
    expected = _FAMILY_KIND.get(spec_format.partition("/")[0])
    if expected is not None and expected != kind:
        raise SpecError(
            f"This is a {spec_format} spec, which is stored as a {expected}, "
            f"not a {kind}."
        )
    if validate:
        data = validate_spec(data, spec_format) or data
        if detected:
            # Validating can migrate the spec to a newer schema.
            spec_format = detect_format(data)
    return data, spec_format


def detect_format(spec: Mapping[str, Any]) -> str:
    """Name the format of a JSON spec from its shape.

    Args:
        spec: A JSON spec.

    Returns:
        ``"flex-pse.flowsheet/<schema_version>"`` for a flex-pse flowsheet,
        ``"flex-pse.surrogate"`` for a flex-pse surrogate spec,
        ``"flex-pse.design"`` for a flex-pse design study, otherwise ``"json"``.

    Example:
        >>> detect_format({"surrogate_type": "multilinear", "data": {}})
        'flex-pse.surrogate'
    """
    if "elements" in spec and "schema_version" in spec:
        return f"{FLEXPSE_FLOWSHEET}/{spec['schema_version']}"
    if "surrogate_type" in spec and "data" in spec:
        return FLEXPSE_SURROGATE
    if is_design_spec(spec):
        return FLEXPSE_DESIGN
    return GENERIC_JSON


def is_design_spec(spec: Mapping[str, Any]) -> bool:
    """Whether a JSON spec is a flex-pse design study, from its shape."""
    return _DESIGN_FIELDS <= spec.keys()


def validate_spec(spec: dict[str, Any], spec_format: str) -> dict[str, Any] | None:
    """Check and normalise ``spec`` with its format's validator, if available.

    A format with no validator, or whose library isn't installed, is accepted
    unchecked: specs are data, and the orchestrator doesn't require flex-pse.

    Args:
        spec: A JSON spec.
        spec_format: Its format, e.g. ``"flex-pse.flowsheet/0.1.1"``. The part
            before ``/`` picks the validator.

    Returns:
        The normalised spec, or ``None`` if no validator ran.

    Raises:
        SpecError: If the validator rejects the spec.
    """
    entry = VALIDATORS.get(spec_format.partition("/")[0])
    if entry is None:
        return None
    requires, validator = entry
    if importlib.util.find_spec(requires) is None:
        _log.debug(
            "Not validating a %s spec: %s isn't installed.", spec_format, requires
        )
        return None
    return to_json_spec(import_hook(validator)(spec))


def check_tag(tag: str) -> str:
    """Check that ``tag`` can name a stored item.

    Tags become Acquirium labels and the file names of a study export,
    so they are limited to letters, digits, ``_``, ``.`` and ``-``, start with a
    letter or digit, and are at most 128 characters.

    Args:
        tag: The tag.

    Returns:
        ``tag``.

    Raises:
        SpecError: If the tag isn't valid.

    Example:
        >>> check_tag("model_with_surrogate_biogas_h1")
        'model_with_surrogate_biogas_h1'
    """
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise SpecError(
            f"Tag {tag!r} is not valid: use 1-128 letters, digits, '_', '.' or "
            "'-', starting with a letter or digit."
        )
    return tag


class SpecRecord(BaseModel):
    """A spec as stored: the data plus what identifies it.

    This is the content of a ``<s>::specs`` run and of a study export's
    spec file. Storage adds who stored it and when.

    Attributes:
        kind: ``"model_spec"`` or ``"surrogate_spec"``.
        tag: The tag, unique per kind within a study.
        spec: The spec, as plain JSON.
        spec_format: See :func:`detect_format`.
        spec_hash: See :func:`record_hash`: the spec's hash, covering its
            assets' hashes when it has any.
        parent_tag: The tag of the spec this one was derived from, e.g. the base
            model a surrogate was applied to.
        assets: ``{JSON pointer: AssetRef}`` for each file the spec references;
            the spec holds each asset's stored name at its pointer.

    Example:
        >>> record = SpecRecord.create("model_spec", "base", {"a": 1})
        >>> record.spec_format, record.spec_hash == spec_hash({"a": 1})
        ('json', True)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SpecKind
    tag: str
    spec: dict[str, Any]
    spec_format: str
    spec_hash: str
    parent_tag: str | None = None
    assets: dict[str, AssetRef] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        kind: SpecKind,
        tag: str,
        spec: Any,
        *,
        parent_tag: str | None = None,
        spec_format: str | None = None,
        validate: bool = True,
        assets: Mapping[str, AssetRef] | None = None,
    ) -> "SpecRecord":
        """Normalise, check and hash a spec for storing.

        Args:
            kind: ``"model_spec"`` or ``"surrogate_spec"``.
            tag: The tag (see :func:`check_tag`).
            spec: A mapping or pydantic model.
            parent_tag: The tag of the spec this one was derived from.
            spec_format: The format; detected from the spec when omitted.
            validate: Run the format's validator (see :func:`validate_spec`),
                which also normalises the spec: a flex-pse spec is stored with
                its defaults written out.
            assets: The files the spec references, already collected (see
                :func:`flexorchestrator.assets.collect_assets`).

        Returns:
            The record.

        Raises:
            SpecError: If the tag is invalid, the spec isn't JSON, its format
                belongs to the other kind, or validation fails.
        """
        check_tag(tag)
        if parent_tag is not None:
            check_tag(parent_tag)
        try:
            data, spec_format = normalise_spec(kind, spec, spec_format, validate)
        except SpecError as exc:
            raise SpecError(f"Spec {tag!r}: {exc}") from exc
        assets = dict(assets or {})
        return cls(
            kind=kind,
            tag=tag,
            spec=data,
            spec_format=spec_format,
            spec_hash=record_hash(data, assets),
            parent_tag=parent_tag,
            assets=assets,
        )

    def metadata(self) -> dict[str, str]:
        """Return the run metadata stored with the spec.

        Returns:
            ``flexorch_kind``, ``flexorch_tag``, ``spec_hash``, ``spec_format``
            and, when set, ``parent_tag``.
        """
        metadata = {
            "flexorch_kind": self.kind,
            "flexorch_tag": self.tag,
            "spec_hash": self.spec_hash,
            "spec_format": self.spec_format,
        }
        if self.parent_tag is not None:
            metadata["parent_tag"] = self.parent_tag
        return metadata

    def verify(self) -> None:
        """Check that ``spec_hash`` still matches ``spec``.

        Used when reading a record from storage or an export, to catch a spec
        that was edited without its hash.

        Raises:
            SpecError: If the hash doesn't match.
        """
        actual = record_hash(self.spec, self.assets)
        if actual != self.spec_hash:
            raise SpecError(
                f"Spec {self.tag!r} doesn't match its hash: stored "
                f"{self.spec_hash}, computed {actual}."
            )


class SpecRecordWithFiles(SpecRecord):
    """A stored spec with its files' bytes, for a caller that has no folder.

    What ``model_spec(tag, with_files=True)`` returns. ``files``
    is ``{stored name: bytes}``, the names the spec holds at its assets'
    pointers; it isn't part of the record's JSON. Pass the spec and the files
    to ``store_model_spec(..., files=...)`` to store it again, unchanged.

    Attributes:
        files: ``{stored name: bytes}`` for every asset.
    """

    files: dict[str, bytes] = Field(default_factory=dict, exclude=True, repr=False)


def _canonical(value: Any) -> Any:
    """Rewrite integral floats as ints, recursively, for :func:`canonical_json`."""
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
