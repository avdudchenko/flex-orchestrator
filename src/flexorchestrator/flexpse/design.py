"""The flex-pse design study hooks: size equipment across many horizons.

A design study is one flowsheet (``base``) run over every year and representative
month, with shared capacities. :mod:`flexdesign` builds it as one merged model.
"""

from collections.abc import Mapping
from typing import Any

import pyomo.environ as pyo
from flexcore.config.design import DesignSpec
from flexcore.config.io import load_spec
from flexcore.exceptions import FlexConfigError
from flexdesign import build_design_model
from pydantic import ValidationError

from flexorchestrator.errors import SpecError
from flexorchestrator.flexpse.build import with_overrides


def _load(spec: Mapping[str, Any]) -> DesignSpec:
    """Return ``spec`` as a ``DesignSpec``, its base migrated by flex-pse's loader."""
    base = spec.get("base")
    if not isinstance(base, Mapping):
        raise SpecError(
            "A stored design spec holds its base flowsheet inline, not as "
            f"{type(base).__name__}."
        )
    try:
        return DesignSpec.model_validate({**spec, "base": load_spec(base)})
    except (FlexConfigError, ValidationError) as exc:
        raise SpecError(f"Invalid flex-pse design spec: {exc}") from exc


def validate_design_spec(spec: dict) -> dict:
    """Check a design spec with flex-pse, and normalise it.

    Args:
        spec: A ``DesignSpec`` as JSON, with ``base`` inline.

    Returns:
        The validated spec as JSON, with defaults written out and an older base
        schema migrated.

    Raises:
        SpecError: If flex-pse rejects it; the message names the bad field.
    """
    return _load(spec).model_dump(mode="json", by_alias=True)


def build_design(spec: dict, **overrides: Any) -> pyo.ConcreteModel:
    """Build the merged model of a design study.

    Args:
        spec: A ``DesignSpec`` as JSON.
        **overrides: Values to fix in every horizon, as :func:`build` takes
            them, except that each must be one value, since horizons differ in
            length.

    Returns:
        The merged model, ready to solve.

    Raises:
        SpecError: If an override names a capacity's variable, which the study
            decides (fix its size with the capacity's ``lower`` and ``upper``),
            or gives a value per time point.
    """
    sized = {c["variable"]: c["name"] for c in spec["capacities"]}
    for path, value in overrides.items():
        if path in sized:
            raise SpecError(
                f"Can't fix {path!r}: the design study sizes it as capacity "
                f"{sized[path]!r}. Set that capacity's lower and upper instead."
            )
        per_point = isinstance(value, list) or (
            isinstance(value, Mapping) and set(value) != {"value", "units"}
        )
        if per_point:
            raise SpecError(
                f"Can't fix {path!r} per time point in a design study: horizons "
                "differ in length. Use the design's changes to vary it."
            )
    base = with_overrides(spec["base"], overrides)
    return build_design_model(_load({**spec, "base": base}))
