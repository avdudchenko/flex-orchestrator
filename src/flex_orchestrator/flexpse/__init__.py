"""The default hooks, built on flex-pse 0.3 (PLAN.md D1).

This is the only package that imports flex-pse. Nothing else imports it
directly: :data:`~flex_orchestrator.hooks.DEFAULT_HOOKS` refers to these
functions by import string, so the rest of the library works without flex-pse
installed. Specs are flex-pse flowsheet specs (schema ``0.1.1``; older specs migrate on
load).
"""

from flex_orchestrator.flexpse.build import (
    apply_values,
    build,
    configure,
    with_overrides,
)
from flex_orchestrator.flexpse.export import time_sets
from flex_orchestrator.flexpse.regression import get_regressor, predict_surrogate
from flex_orchestrator.flexpse.solve import solve
from flex_orchestrator.flexpse.specs import validate_model_spec, validate_surrogate_spec
from flex_orchestrator.flexpse.surrogate import (
    DEFAULT_RELATION,
    apply_surrogate,
    swap_surrogate,
)
from flex_orchestrator.flexpse.timegrid import time_grid

__all__ = [
    "DEFAULT_RELATION",
    "apply_surrogate",
    "apply_values",
    "build",
    "configure",
    "get_regressor",
    "predict_surrogate",
    "solve",
    "swap_surrogate",
    "time_grid",
    "time_sets",
    "validate_model_spec",
    "validate_surrogate_spec",
    "with_overrides",
]
