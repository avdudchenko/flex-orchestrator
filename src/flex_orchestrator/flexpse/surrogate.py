"""The flex-pse surrogate hooks: in the spec before building, or on a live model.

Both add a flex-pse ``surrogate`` element: :func:`apply_surrogate` puts it in
the spec, :func:`swap_surrogate` applies it to a built model with
:func:`flexops.apply_spec`. A unit is named by its element name, which is its
path in the model (``"demo.digester"``, or ``"demo.ro[2]"`` for one member of
an indexed unit).
"""

import copy

import pyomo.environ as pyo
from flexcore.config.spec import SurrogateElement
from flexops import apply_spec
from pydantic import ValidationError

from flex_orchestrator.errors import SpecError

DEFAULT_RELATION = SurrogateElement.model_fields["relation"].default
"""str: The relation a surrogate replaces when none is given
(``"power_electrical_relation"``)."""


def apply_surrogate(
    spec: dict, unit: str, surrogate_spec: dict, relation: str | None = None
) -> dict:
    """Return a copy of ``spec`` in which ``unit`` uses ``surrogate_spec``.

    Adds a ``surrogate`` element, replacing any the spec already has for the
    same unit and relation. Storing the edited spec lets the run be replayed
    without the live model (PLAN.md D2).

    Args:
        spec: A ``FlowsheetSpec`` as JSON. Not modified.
        unit: The unit's element name, e.g. ``"demo.digester"``.
        surrogate_spec: A ``SurrogateSpec`` as JSON (``surrogate_type``,
            ``data``, optional ``provenance``).
        relation: The unit's relation to replace, e.g. ``"biogas_relation"``.
            ``None`` means flex-pse's default, :data:`DEFAULT_RELATION`.

    Returns:
        The new spec.

    Raises:
        SpecError: If ``unit`` isn't a unit of the spec, or ``surrogate_spec``
            is invalid.

    Example:
        >>> spec = apply_surrogate(  # doctest: +SKIP
        ...     spec, "demo.digester", arima_spec, relation="biogas_relation"
        ... )
    """
    element = _surrogate_element(unit, surrogate_spec, relation)
    units = {e["name"] for e in spec["elements"] if e.get("kind") == "unit"}
    if unit.partition("[")[0] not in units:
        raise SpecError(f"The spec has no unit {unit!r}. Units: {sorted(units)}.")
    kept = [
        e
        for e in spec["elements"]
        if not (
            e.get("kind") == "surrogate"
            and e.get("unit") == unit
            and e.get("relation", DEFAULT_RELATION) == element["relation"]
        )
    ]
    return {**copy.deepcopy(spec), "elements": [*copy.deepcopy(kept), element]}


def swap_surrogate(
    model: pyo.Block, unit: str, surrogate_spec: dict, relation: str | None = None
) -> None:
    """Swap ``surrogate_spec`` into a built model, in place.

    The old relation is deactivated, not deleted, and the surrogate reuses the
    unit's variables, so arcs are kept.

    Args:
        model: The built model.
        unit: The unit's element name, e.g. ``"demo.digester"``.
        surrogate_spec: A ``SurrogateSpec`` as JSON.
        relation: The relation to replace; ``None`` means
            :data:`DEFAULT_RELATION`.

    Raises:
        SpecError: If ``surrogate_spec`` is invalid.
        flexcore.exceptions.FlexConfigError: If the model has no such unit or
            relation.

    Example:
        >>> swap_surrogate(  # doctest: +SKIP
        ...     model, "demo.digester", arima_spec, relation="biogas_relation"
        ... )
    """
    apply_spec(model, [_surrogate_element(unit, surrogate_spec, relation)])


def _surrogate_element(unit: str, surrogate_spec: dict, relation: str | None) -> dict:
    """Validate a ``surrogate`` element for ``unit`` and return it as JSON."""
    fields = {"kind": "surrogate", "unit": unit, **surrogate_spec}
    if relation is not None:
        fields["relation"] = relation
    try:
        element = SurrogateElement.model_validate(fields)
    except ValidationError as exc:
        raise SpecError(f"Invalid surrogate spec for {unit!r}: {exc}") from exc
    return element.model_dump(mode="json", exclude_none=True)
