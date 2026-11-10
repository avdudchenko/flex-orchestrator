"""Storage backends: the contract, in-memory, files, and Acquirium."""

from flexorchestrator.backend.base import (
    FileValue,
    Record,
    Row,
    Run,
    StorageBackend,
    Variable,
    align,
    qudt_key,
    recording,
    to_rows,
    to_utc,
)
from flexorchestrator.backend.files import FileBackend
from flexorchestrator.backend.memory import MemoryBackend


def __getattr__(name):
    # The Acquirium backend needs requests; import it only when asked for.
    if name == "AcquiriumBackend":
        from flexorchestrator.backend.acquirium import AcquiriumBackend

        return AcquiriumBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AcquiriumBackend",
    "FileBackend",
    "FileValue",
    "MemoryBackend",
    "Record",
    "Row",
    "Run",
    "StorageBackend",
    "Variable",
    "align",
    "qudt_key",
    "recording",
    "to_rows",
    "to_utc",
]
