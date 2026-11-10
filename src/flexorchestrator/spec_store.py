"""Specs as runs of ``<s>::specs``.

Each stored spec is one run. Its inputs are the ``spec_record`` JSON (the
:class:`~flexorchestrator.specs.SpecRecord`: the spec with every referenced
path replaced by its stored name, its format, hash, parent and assets) and one
``file`` input per referenced file, labelled with the asset's stored name. The
run metadata is the record's (``flexorch_kind``, ``flexorch_tag``,
``spec_hash``, ``spec_format``, ``parent_tag``) plus the run provenance.

Acquirium keeps a file's bytes once per content hash, so the same tariff
referenced by many specs is stored once.
"""

from collections.abc import Mapping
from typing import Any

from flexorchestrator.backend.base import FileValue, StorageBackend, recording
from flexorchestrator.errors import SpecError
from flexorchestrator.specs import SpecRecord

SPEC_RECORD = "spec_record"
"""str: The label of a spec run's JSON input holding the :class:`SpecRecord`."""


def write(
    backend: StorageBackend,
    ledger: str,
    record: SpecRecord,
    files: Mapping[str, bytes],
    metadata: Mapping[str, Any],
) -> str:
    """Store a spec and its files as one run, and return the run id.

    Args:
        backend: The store.
        ledger: The ``<s>::specs`` study.
        record: The spec, already holding its assets' stored names.
        files: ``{stored name: bytes}`` for every asset.
        metadata: The run metadata.
    """
    backend.declare(ledger, SPEC_RECORD, role="input", kind="json")
    for name in files:
        backend.declare(ledger, name, role="input", kind="file")
    with recording(backend, ledger, metadata) as run_id:
        backend.record(run_id, SPEC_RECORD, record.model_dump(mode="json"))
        for name, data in files.items():
            backend.record(run_id, name, FileValue(name.rpartition("/")[2], data))
    return run_id


def read(
    backend: StorageBackend, ledger: str, run_id: str
) -> tuple[SpecRecord, dict[str, bytes]]:
    """Read a stored spec and its files back, checking the hash.

    Args:
        backend: The store.
        ledger: The ``<s>::specs`` study.
        run_id: The spec's run.

    Returns:
        The record and ``{stored name: bytes}``.

    Raises:
        SpecError: If the record is missing, or the spec or a file doesn't
            match its hash.
    """
    found = backend.records(ledger, run_id)
    stored = next((r.value for r in found if r.label == SPEC_RECORD), None)
    if stored is None:
        raise SpecError(f"Spec run {run_id} has no {SPEC_RECORD}.")
    record = SpecRecord.model_validate(stored)
    record.verify()
    files = {r.label: r.value.data for r in found if r.kind == "file"}
    return record, files
