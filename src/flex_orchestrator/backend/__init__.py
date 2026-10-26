"""Storage backends (PLAN.md D10): the contract, in-memory, files, and Acquirium."""

from flex_orchestrator.backend.base import (
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
from flex_orchestrator.backend.files import FileBackend
from flex_orchestrator.backend.memory import MemoryBackend


def __getattr__(name):
    # The Acquirium backend needs requests; import it only when asked for.
    if name == "AcquiriumBackend":
        from flex_orchestrator.backend.acquirium import AcquiriumBackend

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
