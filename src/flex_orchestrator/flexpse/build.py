"""The flex-pse build hook: flat spec + overrides -> built Pyomo model."""

from collections.abc import Mapping
from typing import Any

import pyomo.environ as pyo
from flexops import build_model


def build(spec: dict, **overrides: Any) -> pyo.ConcreteModel:
    """Build a flex-pse model from a stored flowsheet spec.

    The overrides are added to the spec as ``fix`` elements
    (:func:`with_overrides`), then :func:`flexops.build_model` validates and
    builds it, arcs expanded.

    Relative paths in the spec (``tariff_source``, ``$source`` files) resolve
    against the working directory of the process that builds, which on Ray is
    the worker's.

    Args:
        spec: A ``FlowsheetSpec`` as JSON (schema ``0.1.0``).
        **overrides: Values to fix, keyed by variable path from the model root
            (see :func:`with_overrides`).

    Returns:
        The model, ready to solve.

    Raises:
        flexcore.exceptions.FlexConfigError: If the spec is invalid or an
            override names no variable; the message names the offending field.

    Example:
        >>> model = build(spec, **{"demo.pump.flow_in": 10.0})  # doctest: +SKIP
    """
    return build_model(with_overrides(spec, overrides))


def with_overrides(spec: dict, overrides: Mapping[str, Any]) -> dict:
    """Return a copy of ``spec`` with one ``fix`` element per override.

    Args:
        spec: A ``FlowsheetSpec`` as JSON. Not modified.
        overrides: ``{variable path: value}``. The path is a Pyomo ComponentUID
            path from the model root (``"demo.pump.flow_in"``,
            ``"demo.pump.flow_in[3]"``, ``"demo.ro[*].status"``). The value is
            anything a ``fix`` element takes: a number in the variable's own
            units, ``{"value": ..., "units": ...}``, a list with one number per
            time point, or a ``{time index: value}`` mapping.

    Returns:
        The new spec. Its ``fix`` elements are named ``"override:<path>"``.

    Example:
        >>> with_overrides(spec, {"demo.pump.flow_in": 10.0})  # doctest: +SKIP
    """
    fixes = []
    for path, value in overrides.items():
        element = {"kind": "fix", "name": f"override:{path}", "variable": path}
        if isinstance(value, Mapping) and set(value) == {"value", "units"}:
            element.update(value)
        else:
            element["value"] = value
        fixes.append(element)
    return {**spec, "elements": [*spec["elements"], *fixes]}
