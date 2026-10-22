"""flex-orchestrator: run, store and query flex-pse studies on Acquirium."""

from importlib.metadata import version as _dist_version

from flex_orchestrator.errors import (
    AmbiguousColumnError,
    ExportError,
    FlexOrchestratorError,
    HookError,
    SpecError,
    StorageError,
    StudyExistsError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
    UnitError,
)
from flex_orchestrator.hooks import (
    DEFAULT_HOOKS,
    ApplySurrogateFn,
    BuildFn,
    HookRegistry,
    HookSet,
    SolveFn,
    SolveOutcome,
    SwapSurrogateFn,
    TimeSetsFn,
    run_solve,
)
from flex_orchestrator.specs import SpecRecord, canonical_json, spec_hash, to_json_spec

__version__ = _dist_version("flex-orchestrator")


_LAZY = {
    "export_model_data": "flex_orchestrator.export",
    "Orchestrator": "flex_orchestrator.orchestrator",
    "ResultHandle": "flex_orchestrator.orchestrator",
    "StoredResult": "flex_orchestrator.orchestrator",
}


def __getattr__(name):
    # These need pyomo; import them only when they are asked for.
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AmbiguousColumnError",
    "DEFAULT_HOOKS",
    "ExportError",
    "ApplySurrogateFn",
    "BuildFn",
    "FlexOrchestratorError",
    "HookError",
    "HookRegistry",
    "HookSet",
    "Orchestrator",
    "ResultHandle",
    "SolveFn",
    "SolveOutcome",
    "SpecError",
    "SpecRecord",
    "StorageError",
    "StoredResult",
    "StudyExistsError",
    "StudyNotFoundError",
    "SwapSurrogateFn",
    "TagExistsError",
    "TagNotFoundError",
    "TimeSetsFn",
    "UnitError",
    "canonical_json",
    "export_model_data",
    "run_solve",
    "spec_hash",
    "to_json_spec",
]
