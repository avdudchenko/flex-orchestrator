"""Exceptions raised by flex-orchestrator.

Every error subclasses :class:`FlexOrchestratorError`, so callers can catch the
base class for any orchestrator failure or a concrete subclass for one category.
Messages say what went wrong and how to fix it.
"""


class FlexOrchestratorError(Exception):
    """Base class for all flex-orchestrator errors."""


class HookError(FlexOrchestratorError):
    """A build/solve hook can't be resolved or broke its contract (PLAN.md D1).

    Raised for a malformed or unimportable import string, an unknown registered
    hook name, a hook slot that isn't set, or a solve hook that returns
    something other than a :class:`~flex_orchestrator.hooks.SolveOutcome`.
    """


class SpecError(FlexOrchestratorError):
    """A model spec, override or surrogate target doesn't match the model.

    Raised for an override path that names no component (or one that can't be
    set), and for a surrogate swap aimed at a unit the spec or model doesn't
    have.
    """


class ExportError(FlexOrchestratorError):
    """Model values can't be exported (PLAN.md D3).

    Raised for a key that names nothing, ambiguous or inconsistent time sets,
    two values that would be stored under the same key, or a named component
    with non-numeric values.
    """


class UnitError(FlexOrchestratorError):
    """A unit can't be mapped to QUDT (in strict mode) or converted."""
