"""flex-orchestrator: run, store and query flex-pse studies on Acquirium."""

from importlib.metadata import version as _dist_version

from flex_orchestrator.errors import (
    ExportError,
    FlexOrchestratorError,
    HookError,
    SpecError,
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


def __getattr__(name):
    # The exporter needs pyomo; import it only when it is asked for.
    if name == "export_model_data":
        from flex_orchestrator.export import export_model_data

        return export_model_data
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_HOOKS",
    "ExportError",
    "ApplySurrogateFn",
    "BuildFn",
    "FlexOrchestratorError",
    "HookError",
    "HookRegistry",
    "HookSet",
    "SolveFn",
    "SolveOutcome",
    "SpecError",
    "SpecRecord",
    "SwapSurrogateFn",
    "TimeSetsFn",
    "UnitError",
    "canonical_json",
    "export_model_data",
    "run_solve",
    "spec_hash",
    "to_json_spec",
]
