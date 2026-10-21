"""The default hooks, built on flex-pse 0.2 (PLAN.md D1).

This is the only package that imports flex-pse. Nothing else imports it
directly: :data:`~flex_orchestrator.hooks.DEFAULT_HOOKS` refers to these
functions by import string, so the rest of the library works without flex-pse
installed. Specs are flex-pse flowsheet specs (schema ``0.1.0``).
"""

from flex_orchestrator.flexpse.build import build, with_overrides
from flex_orchestrator.flexpse.solve import solve
from flex_orchestrator.flexpse.specs import validate_model_spec, validate_surrogate_spec
from flex_orchestrator.flexpse.surrogate import (
    DEFAULT_RELATION,
    apply_surrogate,
    swap_surrogate,
)

__all__ = [
    "DEFAULT_RELATION",
    "apply_surrogate",
    "build",
    "solve",
    "swap_surrogate",
    "validate_model_spec",
    "validate_surrogate_spec",
    "with_overrides",
]
