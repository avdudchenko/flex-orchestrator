"""Write a study, or one result, to a folder of JSON files (PLAN.md D17).

The export is written into a temporary folder next to the target and renamed
into place at the end, so a crash never leaves a half-written export. Files
are written one at a time, so memory use stays bounded. Every file is listed
in ``study.json`` and has its sha256 in ``checksums.sha256``, which
:func:`~flex_orchestrator.archive.import_study` checks. Credentials never get
in: the connection recorded in run metadata is already redacted (D11).
"""

import gzip
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flex_orchestrator import data as datasets
from flex_orchestrator import results, spec_store
from flex_orchestrator.archive.models import (
    DatasetColumn,
    DatasetFile,
    FitFile,
    FitSeries,
    ResultFile,
    ResultScalar,
    ResultSeries,
    SpecFile,
    StudyManifest,
    TimeIndex,
)
from flex_orchestrator.backend.base import to_utc
from flex_orchestrator.backend.files import replace
from flex_orchestrator.data import DATA_KIND, MANIFEST
from flex_orchestrator.errors import ArchiveError
from flex_orchestrator.export import ModelData
from flex_orchestrator.orchestrator import (
    _VERSION_KEYS,
    LEDGERS,
    MODEL_SPEC,
    MODEL_SPEC_REF,
    OVERRIDES,
    SURROGATE_SPEC,
    _user,
    _version_of,
)

PARTS = ("specs", "data", "fits", "results")
"""tuple[str, ...]: The parts an export can hold, in the order they're imported."""
CHECKSUMS = "checksums.sha256"
MANIFEST_FILE = "study.json"
SPEC_DIRS = {MODEL_SPEC: "specs/models", SURROGATE_SPEC: "specs/surrogates"}


@dataclass
class ExportReport:
    """What :func:`export_study` wrote.

    Attributes:
        path: The export's folder.
        counts: Files per part.
        files: Every file, relative to the folder.
    """

    path: Path
    counts: dict[str, int]
    files: list[str] = field(default_factory=list)


def export_study(
    orch: Any,
    path: str | os.PathLike[str],
    *,
    parts: Iterable[str] = PARTS,
    tags: Iterable[str] | None = None,
    compress: bool = False,
    overwrite: bool = False,
) -> ExportReport:
    """Export the orchestrator's selected study to ``path``.

    Args:
        orch: The orchestrator.
        path: The folder to create.
        parts: Which of ``specs``, ``data``, ``fits`` and ``results``.
        tags: Only items with these tags (dataset names count as tags).
        compress: Write ``*.json.gz`` instead of ``*.json`` (``study.json``
            and the spec's referenced files stay as they are).
        overwrite: Replace a folder that isn't empty.

    Returns:
        What was written.

    Raises:
        ArchiveError: If ``path`` isn't empty (without ``overwrite``) or a
            part is unknown.
    """
    parts = list(parts)
    unknown = sorted(set(parts) - set(PARTS))
    if unknown:
        raise ArchiveError(f"Unknown export parts {unknown}; the parts are {PARTS}.")
    target = Path(path)
    if target.exists() and not overwrite:
        if not target.is_dir() or any(target.iterdir()):
            raise ArchiveError(
                f"{target} exists and isn't an empty folder; pass overwrite=True "
                "to replace it."
            )
    wanted = None if tags is None else set(tags)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.parent / f".{target.name}.partial-{uuid.uuid4().hex[:8]}"
    partial.mkdir()
    try:
        writer = _Writer(partial, compress)
        counts = dict.fromkeys(PARTS, 0)
        if "specs" in parts:
            for file, assets in spec_files(orch, wanted):
                writer.json(f"{SPEC_DIRS[file.kind]}/{file.tag}.json", file.dump())
                for name, data in assets.items():
                    writer.raw(f"specs/{name}", data)
                counts["specs"] += 1
        if "data" in parts:
            for file in dataset_files(orch, wanted):
                writer.json(f"data/{file.name}.json", file.dump())
                counts["data"] += 1
        if "fits" in parts:
            for file in fit_files(orch, wanted):
                writer.json(f"fits/{file.tag}.json", file.dump())
                counts["fits"] += 1
        if "results" in parts:
            for file in result_files(orch, wanted):
                writer.json(f"results/{file.tag}.json", file.dump())
                counts["results"] += 1
        manifest = StudyManifest(
            study=orch.study,
            exported_at=datetime.now(UTC).isoformat(),
            exported_by=_user(),
            versions=_versions(),
            counts=counts,
            options={
                "parts": parts,
                "tags": None if wanted is None else sorted(wanted),
                "compress": compress,
            },
            files=sorted(writer.files),
        )
        writer.json(MANIFEST_FILE, manifest.dump(), compress=False)
        writer.checksums()
        if target.exists():
            shutil.rmtree(target)
        replace(partial, target)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return ExportReport(target, counts, sorted(writer.files))


def export_result(orch: Any, tag: str, path: str | os.PathLike[str]) -> Path:
    """Export one stored result to a JSON file (``.json.gz`` compresses it).

    Args:
        orch: The orchestrator, with the result's study selected.
        tag: The result's tag.
        path: The file to write; a ``.gz`` suffix gzips it. Its folder is
            created if needed.

    Returns:
        The file written.

    Raises:
        TagNotFoundError: If no result has this tag.
    """
    [file] = result_files(orch, {tag}, require=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _encode(file.dump())
    path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == ".gz" else data)
    return path


def spec_files(
    orch: Any, wanted: set[str] | None
) -> Iterator[tuple[SpecFile, dict[str, bytes]]]:
    """Yield each stored spec as a :class:`SpecFile` with its files, oldest first."""
    ledger = orch._ledger(orch.study, LEDGERS[MODEL_SPEC])
    for kind in (MODEL_SPEC, SURROGATE_SPEC):
        for run in orch.backend.runs(
            ledger, status="succeeded", metadata={"flexorch_kind": kind}
        ):
            if wanted is not None and run.metadata["flexorch_tag"] not in wanted:
                continue
            record, files = spec_store.read(orch.backend, ledger, run.run_id)
            yield (
                SpecFile(
                    tag=record.tag,
                    kind=record.kind,
                    spec_format=record.spec_format,
                    spec_hash=record.spec_hash,
                    parent_tag=record.parent_tag,
                    created_at=_iso(run.started_at),
                    created_by=run.metadata.get("flexorch_user"),
                    run_id=run.run_id,
                    versions=_run_versions(run.metadata),
                    metadata=dict(run.metadata),
                    assets=record.assets,
                    spec=record.spec,
                ),
                files,
            )


def dataset_files(orch: Any, wanted: set[str] | None) -> Iterator[DatasetFile]:
    """Yield each stored dataset as a :class:`DatasetFile`, oldest first."""
    ledger = orch._ledger(orch.study, LEDGERS[DATA_KIND])
    for run in orch.backend.runs(
        ledger, status="succeeded", metadata={"flexorch_kind": DATA_KIND}
    ):
        name = run.metadata["flexorch_tag"]
        if wanted is not None and name not in wanted:
            continue
        [manifest] = orch.backend.records(ledger, run.run_id, labels=[MANIFEST])
        timestamps, values = datasets.read(orch.backend, ledger, run.run_id, name, None)
        units = manifest.value.get("units", {})
        keys = manifest.value.get("qudt_units", {})
        yield DatasetFile(
            name=name,
            run_id=run.run_id,
            created_at=_iso(run.started_at),
            created_by=run.metadata.get("flexorch_user"),
            time_index=[_iso(when) for when in timestamps],
            columns={
                column: DatasetColumn(
                    unit=units.get(column), qudt_unit=keys.get(column), values=v
                )
                for column, v in values.items()
            },
            metadata=dict(run.metadata),
            manifest=dict(manifest.value),
        )


def fit_files(orch: Any, wanted: set[str] | None) -> Iterator[FitFile]:
    """Yield each stored fit run as a :class:`FitFile`, oldest first."""
    from flex_orchestrator.fitting import store as fits

    for fit in fits.read_all(orch):
        if wanted is not None and fit["tag"] not in wanted:
            continue
        series = None
        table = fit.get(fits.FIT_SERIES)
        if table is not None:
            series = FitSeries(
                time_index=[_iso(when) for when in table["time"].to_list()],
                actual=table["actual"].to_list(),
                fitted=table["fitted"].to_list(),
                residual=table["residual"].to_list(),
            )
        yield FitFile(
            tag=fit["tag"],
            run_id=fit["run_id"],
            started_at=_iso(fit["started_at"]),
            metadata=fit["metadata"],
            fit_params=fit[fits.FIT_PARAMS],
            training_data=fit[fits.TRAINING_DATA],
            fit_result=fit[fits.FIT_RESULT],
            series=series,
        )


def result_files(
    orch: Any, wanted: set[str] | None, *, require: bool = False
) -> Iterator[ResultFile]:
    """Yield each stored result as a :class:`ResultFile`, oldest first.

    Args:
        orch: The orchestrator.
        wanted: Only these tags.
        require: Raise ``TagNotFoundError`` for a wanted tag with no result.
    """
    handles = (
        [orch.result(tag) for tag in sorted(wanted)]
        if require
        else orch.stored_model_results(wanted)
    )
    runs = orch._runs
    native = {
        v.label
        for v in orch.backend.variables(runs)
        if v.role == "output" and v.kind in ("timeseries", "scalar")
    }
    for handle in handles:
        data = handle.data()
        streams = [k for k in data.keys() if k in native]
        labels = [MODEL_SPEC_REF, OVERRIDES, *streams]
        found = {
            r.label: r.value for r in orch.backend.records(runs, handle.run_id, labels)
        }
        outcome = handle._snapshot()[1]
        time_index, timeseries, scalars, stale = encode_values(data)
        metadata = dict(handle.metadata)
        yield ResultFile(
            tag=handle.tag,
            run_id=handle.run_id,
            started_at=_iso(handle.started_at),
            model_spec_ref=found.get(MODEL_SPEC_REF),
            overrides=found.get(OVERRIDES) or {},
            hooks=metadata.get("flexorch_hooks") or {},
            solve_outcome=outcome,
            metadata=metadata,
            streams=[k for k in streams if k in found],
            keys=data.keys(),
            time_index=TimeIndex(timestamps=time_index),
            timeseries=timeseries,
            scalars=scalars,
            stale=stale,
        )


def encode_values(
    data: ModelData,
) -> tuple[list[str], dict[str, ResultSeries], dict[str, ResultScalar], list[str]]:
    """Return a result's values as ``(time_index, timeseries, scalars, stale)``.

    The timestamps are stored once; each series has a value per timestamp
    (``null`` where it has none) and, if it covers only part of time, the
    positions it covers.
    """
    times, rows_of = results.layout(data)
    timeseries, scalars = {}, {}
    for series in data:
        common = {
            "unit": series.unit_qudt,
            "pyomo_unit": series.unit_pyomo,
            "unit_verified": series.unit_verified,
            "component": series.component,
            "index": list(series.index),
        }
        if series.kind == "scalar":
            scalars[series.key] = ResultScalar(**common, value=series.values[0])
            continue
        rows = rows_of[series.key]
        timeseries[series.key] = ResultSeries(
            **common, values=results.spread(rows, series.values, len(times)), rows=rows
        )
    return [_iso(t) for t in times], timeseries, scalars, data.stale()


class _Writer:
    """Writes export files under a folder, remembering each one."""

    def __init__(self, root: Path, compress: bool) -> None:
        self.root, self.compress = root, compress
        self.files: list[str] = []
        self._written: set[str] = set()

    def json(self, relative: str, payload: Any, compress: bool | None = None) -> None:
        data = _encode(payload)
        if self.compress if compress is None else compress:
            relative += ".gz"
            data = gzip.compress(data, mtime=0)
        self.raw(relative, data)

    def raw(self, relative: str, data: bytes) -> None:
        if relative in self._written:
            return
        self._written.add(relative)
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if relative != MANIFEST_FILE:
            self.files.append(relative)

    def checksums(self) -> None:
        lines = [
            f"{hashlib.sha256((self.root / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted([*self.files, MANIFEST_FILE])
        ]
        (self.root / CHECKSUMS).write_text("".join(lines), encoding="utf-8")


def _encode(payload: Any) -> bytes:
    # Python writes floats in their shortest round-tripping form, so values
    # read back exactly; NaN and infinity aren't JSON and are refused.
    return (
        json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()


def _iso(when: Any) -> str | None:
    if when is None:
        return None
    if isinstance(when, str):
        return when
    return to_utc(when).isoformat()


def _versions() -> dict[str, str | None]:
    return {dist: _version_of(dist) for dist in _VERSION_KEYS}


def _run_versions(metadata: dict[str, Any]) -> dict[str, str | None]:
    return {dist: metadata.get(key) for dist, key in _VERSION_KEYS.items()}
