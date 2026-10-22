"""Files a spec references but doesn't embed (PLAN.md D2, "Referenced files").

A flex-pse flowsheet can point at a tariff file, a demand-response events file,
or ``{"$source": "<path>"}`` data files, which flex-pse reads when it builds
the model. The stored spec alone can't rebuild that model, so the files are
stored with it:

- :func:`find_assets` lists every reference as a JSON pointer into the spec and
  the path written there. A format's finder knows where its references live
  (:func:`flexpse_assets`, which reads the JSON only and needs no flex-pse);
  any spec can also name pointers explicitly.
- :func:`collect_assets` reads each file, and rewrites its path in the spec to
  the stable name ``assets/<sha256[:12]>-<filename>``, so the stored spec
  doesn't depend on where the files were.
- :func:`materialize_spec` writes the files to a directory and points the spec
  at them, so the build hook gets ordinary paths.

Nothing here imports pyomo or flex-pse.
"""

import copy
import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from flex_orchestrator.errors import SpecError

ASSET_DIR = "assets"
"""str: The directory part of every stored asset name."""

# The file suffixes flex-pse reads as files; anything else in a tariff or
# events source is a historian tag, not a path.
_FLEXPSE_FILE_SUFFIXES = (".json", ".csv")


class AssetRef(BaseModel):
    """One stored file a spec references.

    Attributes:
        name: The stored name, ``assets/<sha256[:12]>-<filename>``; the spec
            holds this name where it held the path.
        sha256: The file's sha256, hex.
        size: The file's size in bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    sha256: str
    size: int


def find_assets(
    spec: Mapping[str, Any],
    spec_format: str,
    pointers: Iterable[str] = (),
) -> dict[str, str]:
    """Return ``{JSON pointer: path}`` for every file ``spec`` references.

    Args:
        spec: The spec, as JSON.
        spec_format: Its format; ``flex-pse.flowsheet/...`` uses
            :func:`flexpse_assets`.
        pointers: More references, as JSON pointers to path strings, for
            formats without a finder.

    Raises:
        SpecError: If a pointer doesn't name a string in the spec.
    """
    from flex_orchestrator.specs import FLEXPSE_FLOWSHEET

    found = {}
    if spec_format.partition("/")[0] == FLEXPSE_FLOWSHEET:
        found.update(flexpse_assets(spec))
    for pointer in pointers:
        value = get_pointer(spec, pointer)
        if not isinstance(value, str):
            raise SpecError(
                f"Asset pointer {pointer!r} names a {type(value).__name__}, not "
                "a file path."
            )
        found[pointer] = value
    return found


def flexpse_assets(spec: Mapping[str, Any]) -> dict[str, str]:
    """Find the files a flex-pse flowsheet references.

    These are each costing element's ``tariff_source`` (a path, a list, or a
    ``{name: path}`` mapping) and ``dr.events_source``, where they end in
    ``.json`` or ``.csv``, plus every ``{"$source": path}`` value anywhere.

    Returns:
        ``{JSON pointer: path}``.

    Example:
        >>> flexpse_assets({"elements": [{"kind": "costing",
        ...     "tariff_source": "tariff.csv"}]})
        {'/elements/0/tariff_source': 'tariff.csv'}
    """
    found: dict[str, str] = {}
    for i, element in enumerate(spec.get("elements") or []):
        if not isinstance(element, Mapping) or element.get("kind") != "costing":
            continue
        _sources(found, f"/elements/{i}/tariff_source", element.get("tariff_source"))
        dr = element.get("dr")
        if isinstance(dr, Mapping):
            _sources(found, f"/elements/{i}/dr/events_source", dr.get("events_source"))
    _source_refs(found, "", spec)
    return found


def collect_assets(
    spec: Mapping[str, Any],
    references: Mapping[str, str],
    base_dir: str | Path | None = None,
    known: Mapping[str, tuple[AssetRef, bytes]] | None = None,
) -> tuple[dict[str, Any], dict[str, AssetRef], dict[str, bytes]]:
    """Read the referenced files and point the spec at their stored names.

    Args:
        spec: The spec, as JSON (not changed).
        references: ``{JSON pointer: path}``, from :func:`find_assets`.
        base_dir: The directory relative paths resolve against; the working
            directory by default.
        known: Assets already stored, ``{name: (ref, bytes)}``. A reference
            that is one of these names is kept as it is, so a spec derived
            from a stored one (e.g. by ``apply_surrogate``) needs no files.

    Returns:
        The spec with each path replaced by its stored name, ``{pointer:
        AssetRef}``, and ``{name: bytes}`` for every file.

    Raises:
        SpecError: If a file can't be read; a missing file is never skipped.
    """
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    known = known or {}
    rewritten = copy.deepcopy(dict(spec))
    refs: dict[str, AssetRef] = {}
    files: dict[str, bytes] = {}
    for pointer, path in references.items():
        if path in known:
            ref, data = known[path]
        else:
            full = Path(path) if Path(path).is_absolute() else base / path
            try:
                data = full.read_bytes()
            except OSError as exc:
                hint = (
                    " It looks like a stored asset name: store the spec with "
                    "parent= set to the spec it came from."
                    if path.startswith(f"{ASSET_DIR}/")
                    else ""
                )
                raise SpecError(
                    f"Can't read the file the spec references at {pointer} "
                    f"({full}): {exc.strerror}.{hint}"
                ) from None
            digest = hashlib.sha256(data).hexdigest()
            ref = AssetRef(
                name=f"{ASSET_DIR}/{digest[:12]}-{full.name}",
                sha256=digest,
                size=len(data),
            )
        set_pointer(rewritten, pointer, ref.name)
        refs[pointer] = ref
        files[ref.name] = data
    return rewritten, refs, files


def materialize_spec(
    spec: Mapping[str, Any],
    assets: Mapping[str, AssetRef],
    files: Mapping[str, bytes],
    directory: str | Path,
) -> dict[str, Any]:
    """Write a stored spec's files to ``directory`` and point the spec at them.

    Args:
        spec: The stored spec, holding asset names.
        assets: Its ``{pointer: AssetRef}``.
        files: ``{name: bytes}`` for every asset.
        directory: Where to write the files (``<directory>/assets/...``).

    Returns:
        A copy of the spec with each asset name replaced by the file's
        absolute path.

    Raises:
        SpecError: If a file is missing or doesn't match its hash.
    """
    root = Path(directory).resolve()
    materialized = copy.deepcopy(dict(spec))
    for pointer, ref in assets.items():
        data = files.get(ref.name)
        if data is None or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise SpecError(
                f"The stored file {ref.name} is missing or doesn't match its hash."
            )
        target = root / ref.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        set_pointer(materialized, pointer, str(target))
    return materialized


def get_pointer(document: Any, pointer: str) -> Any:
    """Return the value at a JSON pointer (RFC 6901).

    Raises:
        SpecError: If the pointer doesn't resolve.
    """
    value = document
    for part in _parts(pointer):
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise SpecError(f"JSON pointer {pointer!r} doesn't resolve.") from None
    return value


def set_pointer(document: Any, pointer: str, new: Any) -> None:
    """Set the value at a JSON pointer, in place.

    Raises:
        SpecError: If the pointer doesn't resolve to an existing value.
    """
    parts = _parts(pointer)
    if not parts:
        raise SpecError("Can't replace the whole spec through a JSON pointer.")
    parent = get_pointer(document, _join(parts[:-1]))
    last = parts[-1]
    get_pointer(document, pointer)  # it must exist
    if isinstance(parent, list):
        parent[int(last)] = new
    else:
        parent[last] = new


def _sources(found: dict[str, str], pointer: str, source: Any) -> None:
    if isinstance(source, str):
        if source.lower().endswith(_FLEXPSE_FILE_SUFFIXES):
            found[pointer] = source
    elif isinstance(source, list):
        for i, item in enumerate(source):
            _sources(found, f"{pointer}/{i}", item)
    elif isinstance(source, Mapping):
        for key, item in source.items():
            _sources(found, f"{pointer}/{_escape(str(key))}", item)


def _source_refs(found: dict[str, str], pointer: str, value: Any) -> None:
    if isinstance(value, Mapping):
        if set(value) == {"$source"} and isinstance(value["$source"], str):
            found[f"{pointer}/{_escape('$source')}"] = value["$source"]
            return
        for key, item in value.items():
            _source_refs(found, f"{pointer}/{_escape(str(key))}", item)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _source_refs(found, f"{pointer}/{i}", item)


def _parts(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise SpecError(f"JSON pointer {pointer!r} must start with '/'.")
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]


def _join(parts: list[str]) -> str:
    return "".join(f"/{_escape(p)}" for p in parts)


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")
