"""Re-create an exported study in a store (PLAN.md D17).

Every checksum and the schema version are checked before anything is
written. The study is then re-created through the normal store paths, so the
D9 uniqueness rules and the unit declarations apply: fits first (so the
surrogate specs can name their new fit runs), then specs (parents before
children), datasets and results. Tags, spec hashes and each result's hooks are
kept; each item records where it came from in ``flexorch_imported_from``
(the study, its original run id and metadata, who exported it and when).
"""

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import ValidationError

from flex_orchestrator import spec_store
from flex_orchestrator.archive.export import CHECKSUMS, MANIFEST_FILE, SPEC_DIRS
from flex_orchestrator.archive.load import decode_values, read_json
from flex_orchestrator.archive.models import (
    DatasetFile,
    FitFile,
    ResultFile,
    SpecFile,
    StudyManifest,
)
from flex_orchestrator.errors import (
    ArchiveError,
    StudyExistsError,
    TagExistsError,
    TagNotFoundError,
)
from flex_orchestrator.hooks import SolveOutcome
from flex_orchestrator.orchestrator import (
    DATA_KIND,
    FIT_KIND,
    LEDGERS,
    MODEL_SPEC,
    RESULT_KIND,
    _Written,
)
from flex_orchestrator.specs import SpecRecord

IMPORTED_FROM = "flexorch_imported_from"
"""str: The run metadata key recording an imported item's origin."""


@dataclass
class ImportReport:
    """What :func:`import_study` did.

    Attributes:
        study: The study it imported into.
        counts: Items imported per part.
        skipped: ``kind:tag`` of items already stored (``on_existing: skip``).
        run_ids: Original run id -> new run id.
    """

    study: str
    counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    run_ids: dict[str, str] = field(default_factory=dict)


def import_study(
    orch: Any,
    path: str | os.PathLike[str],
    *,
    name: str | None = None,
    on_existing: Literal["error", "skip"] = "error",
) -> ImportReport:
    """Import an exported study and select it.

    Args:
        orch: The orchestrator to import into.
        path: The export's folder.
        name: The new study's name; the exported study's by default.
        on_existing: If the study exists: ``error``, or ``skip`` the items it
            already has and import the rest into it.

    Returns:
        What was imported.

    Raises:
        ArchiveError: If a checksum doesn't match, a file is missing or
            unlisted, or a file's schema isn't ``flexorch-export/1``.
        StudyExistsError: If the study exists and ``on_existing`` is
            ``error``.
    """
    folder = Path(path)
    manifest = _manifest(folder)
    verify_checksums(folder, manifest)
    study = name or manifest.study
    if orch.backend.has_study(study):
        if on_existing != "skip":
            raise StudyExistsError(
                f"Study {study!r} exists; import under another name=, or pass "
                "on_existing='skip' to add what it doesn't have."
            )
        orch.select_study(study)
    else:
        orch.create_study(study)
    report = ImportReport(study, counts=dict.fromkeys(manifest.counts, 0))
    origin = {
        "study": manifest.study,
        "exported_at": manifest.exported_at,
        "exported_by": manifest.exported_by,
    }
    files = manifest.files
    for file in _files(folder, files, "fits/", FitFile):
        _import(
            orch,
            report,
            "fits",
            FIT_KIND,
            file.tag,
            file.run_id,
            origin,
            file,
            lambda f: _fit(orch, f, report),
        )
    for file in _specs_in_order(folder, files):
        _import(
            orch,
            report,
            "specs",
            file.kind,
            file.tag,
            file.run_id,
            origin,
            file,
            lambda f: _spec(orch, folder, f, report),
        )
    for file in _files(folder, files, "data/", DatasetFile):
        _import(
            orch,
            report,
            "data",
            DATA_KIND,
            file.name,
            file.run_id,
            origin,
            file,
            lambda f: _dataset(orch, f),
        )
    for file in _files(folder, files, "results/", ResultFile):
        _import(
            orch,
            report,
            "results",
            RESULT_KIND,
            file.tag,
            file.run_id,
            origin,
            file,
            lambda f: _result(orch, f),
        )
    return report


def verify_checksums(folder: Path, manifest: StudyManifest) -> None:
    """Check every file against ``checksums.sha256``, and the list against the manifest.

    Raises:
        ArchiveError: If a file is missing, unlisted, or its hash differs.
    """
    try:
        lines = (folder / CHECKSUMS).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ArchiveError(f"{folder} has no {CHECKSUMS}: {exc}") from exc
    sums = {}
    for line in lines:
        digest, _, relative = line.partition("  ")
        sums[relative] = digest
    expected = {*manifest.files, MANIFEST_FILE}
    if set(sums) != expected:
        extra, missing = sorted(set(sums) - expected), sorted(expected - set(sums))
        raise ArchiveError(
            f"{CHECKSUMS} doesn't list the export's files (extra {extra}, missing "
            f"{missing})."
        )
    for relative, digest in sorted(sums.items()):
        path = folder / relative
        if not path.is_file():
            raise ArchiveError(f"{relative} is missing from {folder}.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ArchiveError(f"{relative} doesn't match its checksum; it changed.")


def _manifest(folder: Path) -> StudyManifest:
    try:
        return StudyManifest.model_validate(read_json(folder / MANIFEST_FILE))
    except ValidationError as exc:
        raise ArchiveError(f"{folder / MANIFEST_FILE} is invalid: {exc}") from exc


def _files(folder: Path, files: list[str], prefix: str, model: Any) -> Iterator[Any]:
    for relative in files:
        if relative.startswith(prefix) and relative.endswith((".json", ".json.gz")):
            try:
                yield model.model_validate(read_json(folder / relative))
            except ValidationError as exc:
                raise ArchiveError(f"{relative} is invalid: {exc}") from exc


def _specs_in_order(folder: Path, files: list[str]) -> list[SpecFile]:
    """Spec files with every parent before its children, else in file order."""
    found = []
    for directory in SPEC_DIRS.values():
        found += list(_files(folder, files, directory + "/", SpecFile))
    by_key = {(f.kind, f.tag): f for f in found}
    ordered, seen = [], set()

    def visit(file: SpecFile, trail: tuple = ()) -> None:
        key = (file.kind, file.tag)
        if key in seen or key in trail:
            return
        parent = by_key.get((file.kind, file.parent_tag))
        if parent is not None:
            visit(parent, (*trail, key))
        seen.add(key)
        ordered.append(file)

    for file in found:
        visit(file)
    return ordered


def _import(
    orch: Any,
    report: ImportReport,
    part: str,
    kind: str,
    tag: str,
    run_id: str,
    origin: dict[str, Any],
    file: Any,
    store: Any,
) -> None:
    provenance = {
        **origin,
        "run_id": run_id,
        "metadata": getattr(file, "metadata", {}),
    }
    hooks = getattr(file, "hooks", None)
    forced = {IMPORTED_FROM: provenance}
    if hooks:
        forced["flexorch_hooks"] = hooks
    try:
        with orch._with_metadata(forced, wins=True):
            new_id = store(file)
    except TagExistsError:
        # Only an existing study (on_existing="skip") can already hold the tag.
        report.skipped.append(f"{kind}:{tag}")
        return
    report.counts[part] = report.counts.get(part, 0) + 1
    report.run_ids[run_id] = new_id


def _has(orch: Any, kind: str, tag: str) -> bool:
    try:
        orch._latest(tag, kind)
    except TagNotFoundError:
        return False
    return True


def _fit(orch: Any, file: FitFile, report: ImportReport) -> str:
    from flex_orchestrator.fitting import store as fits

    records: dict[str, Any] = {
        fits.FIT_PARAMS: file.fit_params,
        fits.TRAINING_DATA: file.training_data,
        fits.FIT_RESULT: file.fit_result,
    }
    if file.series is not None:
        records[fits.FIT_SERIES] = fits.series_table(
            pd.DatetimeIndex(file.series.time_index),
            file.series.actual,
            file.series.fitted,
        )
    keep = {
        k: file.metadata[k] for k in ("regressor", "data_hash") if k in file.metadata
    }
    return fits.write(orch, file.tag, keep, records, False)


def _spec(orch: Any, folder: Path, file: SpecFile, report: ImportReport) -> str:
    record = SpecRecord(
        kind=file.kind,
        tag=file.tag,
        spec=file.spec,
        spec_format=file.spec_format,
        spec_hash=file.spec_hash,
        parent_tag=file.parent_tag,
        assets=file.assets,
    )
    record.verify()
    data = {
        ref.name: (folder / "specs" / ref.name).read_bytes()
        for ref in record.assets.values()
    }
    extra = {}
    old_fit = file.metadata.get("fit_run_id")
    if old_fit is not None:
        extra["fit_run_id"] = report.run_ids.get(old_fit, old_fit)
    for key in ("data_hash",):
        if key in file.metadata:
            extra[key] = file.metadata[key]
    ledger = orch._ledger(orch.study, LEDGERS[file.kind])

    def write() -> _Written:
        metadata = {**orch._metadata(file.tag, file.kind), **record.metadata()}
        return _Written(spec_store.write(orch.backend, ledger, record, data, metadata))

    with orch._with_metadata(extra):
        return orch._guarded(file.kind, file.tag, False, write).run_id


def _dataset(orch: Any, file: DatasetFile) -> str:
    frame = pd.DataFrame(
        {name: pd.array(c.values, dtype="float64") for name, c in file.columns.items()},
        index=pd.DatetimeIndex(
            [datetime.fromisoformat(t) for t in file.time_index], name="time"
        ),
    )
    units = {name: c.unit for name, c in file.columns.items() if c.unit is not None}
    return orch.store_data(file.name, frame, units=units).run_id


def _result(orch: Any, file: ResultFile) -> str:
    data = decode_values(file)
    outcome = None
    if file.solve_outcome is not None:
        outcome = SolveOutcome.model_validate(
            {k: v for k, v in file.solve_outcome.items() if k != "ok"}
        )
    spec = None
    ref = file.model_spec_ref
    if ref is not None and _has(orch, MODEL_SPEC, ref["tag"]):
        if orch.model_spec(ref["tag"]).spec_hash != ref["spec_hash"]:
            raise ArchiveError(
                f"Result {file.tag!r} was built from spec {ref['tag']!r} with hash "
                f"{ref['spec_hash']}, but the spec stored here has another hash."
            )
        spec = ref["tag"]
    stored = orch.store_model_data(
        file.tag,
        data,
        outcome,
        streams=file.streams or None,
        spec=spec,
        overrides=file.overrides if spec is not None else None,
    )
    return stored.run_id


__all__ = ["IMPORTED_FROM", "ImportReport", "import_study", "verify_checksums"]
