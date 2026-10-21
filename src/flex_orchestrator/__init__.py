"""flex-orchestrator: run, store and query flex-pse studies on Acquirium."""

from importlib.metadata import version as _dist_version

from flex_orchestrator.errors import FlexOrchestratorError, HookError, SpecError
from flex_orchestrator.hooks import (
    DEFAULT_HOOKS,
    ApplySurrogateFn,
    BuildFn,
    HookRegistry,
    HookSet,
    SolveFn,
    SolveOutcome,
    SwapSurrogateFn,
    run_solve,
)
from flex_orchestrator.specs import SpecRecord, canonical_json, spec_hash, to_json_spec

__version__ = _dist_version("flex-orchestrator")

__all__ = [
    "DEFAULT_HOOKS",
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
    "canonical_json",
    "run_solve",
    "spec_hash",
    "to_json_spec",
]
