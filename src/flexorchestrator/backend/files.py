"""A backend that keeps every study in a folder on disk, with no server.

It follows the :class:`~flexorchestrator.backend.base.StorageBackend`
contract the way Acquirium does, so the contract tests pass on it too, and
everything the orchestrator does works on it: studies, fits, sweeps (Ray
included), export and import. Each value is on disk once it's recorded, so a
crash loses only the run in progress.

The folder::

    flexorch-files.json                   # marks the folder: format and version
    studies/<name>-<hash>/                # one per backend study (s, s__runs, ...)
      study.json                          # the name and every declared variable
      runs/<run_id>/
        run.json                          # status, metadata, times, error, and
                                          # what was recorded, in order; scalar
                                          # and JSON values inline
        <n>.parquet                       # the n-th record: a timeseries
                                          # (time, value) or a table
        <n>.bin                           # the n-th record: a file's bytes

A study's directory is its name, with anything but letters, digits, ``_``,
``.`` and ``-`` made ``_``, plus a short hash of the name itself, so names that
differ only in case or punctuation never share one; ``study.json`` holds the
name exactly.
Every file is written to a temporary name and then renamed, so a crash never
leaves a half-written file. Deletion is real: the directory is renamed out of
the way and then removed.

One process writes a folder at a time: the backend reads the folder when it's
opened and then keeps its index in memory. For a portable copy of a study, use
``export_study``; this folder is the store, not an archive format.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from flexorchestrator.backend.base import (
    FileValue,
    Record,
    Role,
    Run,
    RunStatus,
    Variable,
    VariableKind,
    to_utc,
)
from flexorchestrator.errors import StorageError

FORMAT = "flexorch-files"
"""str: The ``format`` in a folder's marker file."""
VERSION = 1
"""int: The folder layout's version."""
MARKER = "flexorch-files.json"
"""str: The marker file at the folder's root."""

_TRASH = ".deleting-"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]")
_TIMESERIES = {"time": pl.Datetime("us", "UTC"), "value": pl.Float64}


@dataclass
class _Run:
    study: str
    run: Run
    seq: int
    records: dict[str, dict[str, Any]] = field(default_factory=dict)


class FileBackend:
    """Studies, variables and runs kept in a folder (see the module docstring).

    One process uses a folder at a time; within it, the backend is safe to
    share between threads.

    Args:
        path: The folder. It's created if it doesn't exist; an existing folder
            must be empty or one this backend wrote.

    Attributes:
        path: The folder.
        resolve_unit: Always ``None``: there is no unit vocabulary, so the
            exporter's QUDT keys stay unverified and aren't declared.

    Raises:
        StorageError: If ``path`` holds something else, or a newer layout.

    Example:
        >>> import tempfile
        >>> backend = FileBackend(tempfile.mkdtemp())
        >>> backend.define_study("s")
        >>> FileBackend(backend.path).has_study("s")  # opened again
        True
    """

    resolve_unit = None

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Open the folder, creating it if needed, and read its index."""
        self.path = Path(path)
        self._lock = threading.RLock()
        self._dirs: dict[str, Path] = {}
        self._variables: dict[str, dict[str, Variable]] = {}
        self._runs: dict[str, _Run] = {}
        self._seq = 0  # the order runs started in, across reopenings
        self._open()

    def define_study(self, name: str) -> None:
        """Create the study if it doesn't exist."""
        with self._lock:
            if name in self._variables:
                return
            folder = self.path / "studies" / _dirname(name)
            (folder / "runs").mkdir(parents=True, exist_ok=True)
            self._dirs[name] = folder
            self._variables[name] = {}
            self._write_study(name)

    def has_study(self, name: str) -> bool:
        """Return whether the study exists."""
        return name in self._variables

    def studies(self) -> list[str]:
        """Return the names of every study, sorted."""
        with self._lock:  # another thread may be adding one
            return sorted(self._variables)

    def variable(self, study: str, label: str) -> Variable | None:
        """Return the declaration of ``label``, or ``None``."""
        return self._study(study).get(label)

    def declare(
        self,
        study: str,
        label: str,
        *,
        role: Role,
        kind: VariableKind,
        unit: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Variable:
        """Declare a variable, or return it if declared the same way.

        Raises:
            StorageError: If ``label`` is declared differently, a JSON, table
                or file variable is given a unit, or the metadata isn't JSON.
        """
        if kind in ("json", "table", "file") and unit is not None:
            raise StorageError(f"A {kind} variable ({label!r}) can't have a unit.")
        # Like Acquirium, None-valued metadata isn't kept.
        kept = _json({k: v for k, v in (metadata or {}).items() if v is not None})
        wanted = Variable(label, role, kind, unit, kept)
        with self._lock:
            variables = self._study(study)
            found = variables.get(label)
            if found is None:
                variables[label] = found = wanted
                self._write_study(study)
        if found != wanted:
            raise StorageError(
                f"Variable {label!r} in study {study!r} was already declared "
                "differently."
            )
        return found

    def start_run(self, study: str, metadata: Mapping[str, Any]) -> str:
        """Start a run and return its id.

        Raises:
            StorageError: If the study doesn't exist or the metadata isn't JSON.
        """
        with self._lock:
            self._study(study)
            run_id = str(uuid.uuid4())
            run = Run(run_id, "running", _json(dict(metadata)), datetime.now(UTC))
            self._seq += 1
            self._runs[run_id] = entry = _Run(study, run, self._seq)
            self._run_dir(entry).mkdir()
            self._write_run(entry)
        return run_id

    def record(self, run_id: str, label: str, value: Any) -> None:
        """Record a value in a running run.

        Raises:
            StorageError: If the run isn't running, ``label`` isn't declared, or
                the value doesn't fit the kind: a timeseries row without a
                value, a table that isn't a polars DataFrame, a file that isn't
                a FileValue, or a value that isn't JSON (NaN included).
        """
        with self._lock:
            entry = self._running(run_id)
            variable = self.variable(entry.study, label)
            if variable is None:
                raise StorageError(f"Variable {label!r} isn't declared.")
            found = entry.records.get(label)
            n = found["n"] if found else len(entry.records)
            item: dict[str, Any] = {"label": label, "kind": variable.kind, "n": n}
            target = self._run_dir(entry) / f"{n}.parquet"
            if variable.kind == "timeseries":
                rows = sorted(((to_utc(when), v) for when, v in value), key=_first)
                if any(v is None for _, v in rows):
                    raise StorageError(
                        f"Timeseries {label!r} has a row without a value."
                    )
                _json([v for _, v in rows])
                times, values = zip(*rows, strict=True) if rows else ((), ())
                frame = pl.DataFrame(
                    {"time": list(times), "value": list(values)},
                    schema=_TIMESERIES,
                    strict=False,
                )
                if frame["value"].null_count():
                    raise StorageError(
                        f"Timeseries {label!r} has a value that isn't a number."
                    )
                _atomic(target, frame.write_parquet)
            elif variable.kind == "table":
                if not isinstance(value, pl.DataFrame):
                    raise StorageError(f"Table {label!r} records a polars DataFrame.")
                _atomic(target, value.write_parquet)
            elif variable.kind == "file":
                if not isinstance(value, FileValue):
                    raise StorageError(f"File {label!r} records a FileValue.")
                item["name"] = value.name
                _atomic(target.with_suffix(".bin"), lambda p: p.write_bytes(value.data))
            else:
                item["value"] = _json(value)
            entry.records[label] = item
            self._write_run(entry)

    def finish_run(self, run_id: str) -> None:
        """Mark a running run ``succeeded``."""
        self._end(run_id, "succeeded", None)

    def fail_run(self, run_id: str, error: BaseException) -> None:
        """Mark a running run ``failed``."""
        self._end(
            run_id, "failed", {"type": type(error).__name__, "message": str(error)}
        )

    def runs(
        self,
        study: str,
        *,
        status: RunStatus | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[Run]:
        """Return the study's runs, oldest first."""
        self._study(study)
        wanted = _json(dict(metadata or {}))
        with self._lock:  # another thread may be starting or deleting runs
            found = [
                entry
                for entry in self._runs.values()
                if entry.study == study
                and (status is None or entry.run.status == status)
                and all(entry.run.metadata.get(k) == v for k, v in wanted.items())
            ]
        return [entry.run for entry in sorted(found, key=lambda e: e.seq)]

    def variables(self, study: str) -> list[Variable]:
        """Return every variable declared in the study."""
        with self._lock:
            return list(self._study(study).values())

    def delete_runs(self, study: str, run_ids: Sequence[str]) -> None:
        """Delete finished runs and everything they recorded."""
        with self._lock:
            for run_id in run_ids:
                entry = self._runs.get(run_id)
                if entry is None or entry.study != study:
                    raise StorageError(f"Study {study!r} has no run {run_id!r}.")
                if entry.run.status == "running":
                    raise StorageError(f"Run {run_id!r} is still running.")
            for run_id in dict.fromkeys(run_ids):
                _remove(self._run_dir(self._runs[run_id]))
                del self._runs[run_id]

    def delete_study(self, name: str) -> None:
        """Delete a study, its declarations and every run."""
        with self._lock:
            self._study(name)
            runs = [run_id for run_id, e in self._runs.items() if e.study == name]
            self.delete_runs(name, runs)
            _remove(self._dirs.pop(name))
            del self._variables[name]

    def records(
        self, study: str, run_id: str, labels: Sequence[str] | None = None
    ) -> list[Record]:
        """Return what a run recorded, in the order it was recorded."""
        entry = self._runs.get(run_id)
        if entry is None or entry.study != study:
            raise StorageError(f"Study {study!r} has no run {run_id!r}.")
        folder = self._run_dir(entry)
        return [
            _read(folder, item)
            for item in sorted(entry.records.values(), key=lambda i: i["n"])
            if labels is None or item["label"] in labels
        ]

    def _open(self) -> None:
        marker = self.path / MARKER
        if marker.exists():
            found = _load(marker)
            if found.get("format") != FORMAT or found.get("version", 0) > VERSION:
                raise StorageError(
                    f"{self.path} is a {found.get('format')!r} folder, version "
                    f"{found.get('version')!r}; this backend reads {FORMAT!r} up "
                    f"to version {VERSION}."
                )
        elif self.path.exists() and any(self.path.iterdir()):
            raise StorageError(
                f"{self.path} isn't empty and has no {MARKER}, so it isn't a "
                "file store; pick an empty or new folder."
            )
        else:
            self.path.mkdir(parents=True, exist_ok=True)
            _write_json(marker, {"format": FORMAT, "version": VERSION})
        root = self.path / "studies"
        for folder in sorted(root.iterdir()) if root.exists() else []:
            if folder.name.startswith(_TRASH):
                shutil.rmtree(folder, ignore_errors=True)  # a deletion cut short
            elif (folder / "study.json").exists():
                self._load_study(folder)

    def _load_study(self, folder: Path) -> None:
        study = _load(folder / "study.json")
        name = study["name"]
        self._dirs[name] = folder
        self._variables[name] = {
            v["label"]: Variable(
                v["label"], v["role"], v["kind"], v["unit"], v["metadata"]
            )
            for v in study["variables"]
        }
        for run_dir in sorted((folder / "runs").iterdir()):
            if run_dir.name.startswith(_TRASH):
                shutil.rmtree(run_dir, ignore_errors=True)
                continue
            if not (run_dir / "run.json").exists():
                continue  # started but cut short before run.json was written
            item = _load(run_dir / "run.json")
            started = item["started_at"]
            run = Run(
                item["run_id"],
                item["status"],
                item["metadata"],
                datetime.fromisoformat(started) if started else None,
                item["error"],
            )
            self._runs[run.run_id] = _Run(
                name, run, item["seq"], {r["label"]: r for r in item["records"]}
            )
            self._seq = max(self._seq, item["seq"])

    def _study(self, name: str) -> dict[str, Variable]:
        try:
            return self._variables[name]
        except KeyError:
            raise StorageError(f"There is no study {name!r}.") from None

    def _running(self, run_id: str) -> _Run:
        entry = self._runs.get(run_id)
        if entry is None or entry.run.status != "running":
            raise StorageError(f"Run {run_id!r} isn't running.")
        return entry

    def _end(self, run_id: str, status: RunStatus, error: Any) -> None:
        with self._lock:
            entry = self._running(run_id)
            run = entry.run
            entry.run = Run(run.run_id, status, run.metadata, run.started_at, error)
            self._write_run(entry)

    def _run_dir(self, entry: _Run) -> Path:
        return self._dirs[entry.study] / "runs" / entry.run.run_id

    def _write_study(self, name: str) -> None:
        variables = [
            {
                "label": v.label,
                "role": v.role,
                "kind": v.kind,
                "unit": v.unit,
                "metadata": v.metadata,
            }
            for v in self._variables[name].values()
        ]
        _write_json(
            self._dirs[name] / "study.json", {"name": name, "variables": variables}
        )

    def _write_run(self, entry: _Run) -> None:
        run = entry.run
        _write_json(
            self._run_dir(entry) / "run.json",
            {
                "run_id": run.run_id,
                "status": run.status,
                "seq": entry.seq,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "metadata": run.metadata,
                "error": run.error,
                "records": list(entry.records.values()),
            },
        )


def replace(source: Path, target: Path, attempts: int = 6) -> None:
    """Rename ``source`` to ``target``, retrying while Windows holds a file.

    On Windows a file just written can be held for a moment by an antivirus or
    the search indexer, and renaming it (or its folder) fails until it's let
    go. This retries for about 3 seconds in all.

    Raises:
        PermissionError: If it's still held after the last attempt.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * 2**attempt)


def _dirname(name: str) -> str:
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{_UNSAFE.sub('_', name)[:80]}-{digest}"


def _first(row: tuple[datetime, Any]) -> datetime:
    return row[0]


def _json(value: Any) -> Any:
    """Return ``value`` as it reads back from JSON (tuples become lists)."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise StorageError(f"Can't store {value!r}: {exc}.") from None


def _atomic(target: Path, write: Any) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        write(temporary)
        replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(target: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False)
    _atomic(target, lambda p: p.write_text(text + "\n", encoding="utf-8"))


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError(f"Can't read {path}: {exc}.") from None


def _remove(folder: Path) -> None:
    trash = folder.with_name(f"{_TRASH}{uuid.uuid4().hex}")
    replace(folder, trash)
    shutil.rmtree(trash, ignore_errors=True)  # what's left goes on the next open


def _read(folder: Path, item: Mapping[str, Any]) -> Record:
    label, kind, n = item["label"], item["kind"], item["n"]
    if kind == "timeseries":
        frame = pl.read_parquet(folder / f"{n}.parquet")
        rows = [
            (when.astimezone(UTC), value)
            for when, value in zip(frame["time"], frame["value"], strict=True)
        ]
        return Record(label, kind, rows=rows)
    if kind == "table":
        return Record(label, kind, pl.read_parquet(folder / f"{n}.parquet"))
    if kind == "file":
        data = (folder / f"{n}.bin").read_bytes()
        return Record(label, kind, FileValue(item["name"], data))
    return Record(label, kind, copy.deepcopy(item["value"]))
