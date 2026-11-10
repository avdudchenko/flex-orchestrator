"""The default hooks, built on flex-pse 0.3.

This is the only package that imports flex-pse. Nothing else imports it
directly: :data:`~flexorchestrator.hooks.DEFAULT_HOOKS` refers to these
functions by import string, so the rest of the library works without flex-pse
installed. Specs are flex-pse flowsheet specs (schema ``0.1.1``; older specs migrate on
load).
"""

from flexorchestrator.flexpse.build import (
    apply_values,
    build,
    configure,
    with_overrides,
)
from flexorchestrator.flexpse.design import build_design, validate_design_spec
from flexorchestrator.flexpse.export import time_sets
from flexorchestrator.flexpse.regression import get_regressor, predict_surrogate
from flexorchestrator.flexpse.solve import solve
from flexorchestrator.flexpse.specs import validate_model_spec, validate_surrogate_spec
from flexorchestrator.flexpse.surrogate import (
    DEFAULT_RELATION,
    apply_surrogate,
    swap_surrogate,
)
from flexorchestrator.flexpse.timegrid import time_grid

__all__ = [
    "DEFAULT_RELATION",
    "apply_surrogate",
    "apply_values",
    "build",
    "build_design",
    "configure",
    "get_regressor",
    "predict_surrogate",
    "solve",
    "swap_surrogate",
    "time_grid",
    "time_sets",
    "validate_design_spec",
    "validate_model_spec",
    "validate_surrogate_spec",
    "with_overrides",
]
