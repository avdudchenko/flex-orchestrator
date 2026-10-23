"""The flex-pse build hook: flat spec + overrides -> built Pyomo model."""

import copy
from collections.abc import Mapping
from typing import Any

import pyomo.environ as pyo
from flexops import build_model

from flex_orchestrator.errors import SpecError


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

    A path whose first part names a ``costing`` element and whose next part is
    one of its fields (``"costing.energy_prices.electrical"``) edits that
    field instead: that's how a price is swept.

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
    elements = copy.deepcopy(spec["elements"])
    costing = {e["name"]: e for e in elements if e.get("kind") == "costing"}
    fixes = []
    for path, value in overrides.items():
        head, _, rest = path.partition(".")
        if head in costing and rest.partition(".")[0] in costing[head]:
            _set_field(costing[head], rest.split("."), value, path)
            continue
        element = {"kind": "fix", "name": f"override:{path}", "variable": path}
        if isinstance(value, Mapping) and set(value) == {"value", "units"}:
            element.update(value)
        else:
            element["value"] = value
        fixes.append(element)
    return {**spec, "elements": [*elements, *fixes]}


def configure(spec: dict, path: str, value: Any) -> dict:
    """Return a copy of ``spec`` with one configuration option set.

    ``path`` is an element's name followed by the option: the element is the
    one whose name is the longest leading part of ``path``, so this works for
    any unit or block, however deeply named (``"plant.train_1.ro.recovery"``).
    A unit's options are its ``construction_options``; any other element's are
    its own fields (``"costing.energy_prices.electrical"``,
    ``"properties.options.default_temperature"``).

    Dict-valued options can be set whole or one key at a time: the value
    ``{"value": 200, "units": "m^3/hr"}`` at ``"plant.feed.max_withdrawal"``
    replaces the option, and ``200`` at ``"plant.feed.max_withdrawal.value"``
    changes only its number. Missing dicts on the way are created, so an option
    the spec leaves at its default can be set too. The option isn't checked
    here; flex-pse validates the spec when it's stored or built.

    Args:
        spec: A ``FlowsheetSpec`` as JSON. Not modified.
        path: The element's name, then the option's dotted path within it.
        value: The new value: a number, a string, a list or a dict.

    Returns:
        The new spec.

    Raises:
        SpecError: If no element's name starts ``path``, ``path`` names only
            an element, or a part of the path on the way isn't a dict.

    Example:
        >>> configure(spec, "plant.feed.max_withdrawal.value", 200)  # doctest: +SKIP
    """
    elements = copy.deepcopy(spec["elements"])
    named = {e["name"]: e for e in elements if "name" in e}
    parts = path.split(".")
    for cut in range(len(parts), 0, -1):
        element = named.get(".".join(parts[:cut]))
        if element is not None:
            break
    else:
        raise SpecError(
            f"Configuration option {path!r} doesn't start with an element's "
            f"name. Elements: {sorted(named)}."
        )
    if cut == len(parts):
        raise SpecError(
            f"Configuration option {path!r} names the element itself; add the "
            "option, e.g. 'plant.feed.max_withdrawal'."
        )
    if element.get("kind") == "unit":
        options = element.setdefault("construction_options", {})
    else:
        options = element
    _set_field(options, parts[cut:], value, path)
    return {**spec, "elements": elements}


def _set_field(target: dict, parts: list[str], value: Any, path: str) -> None:
    """Set ``target[parts[0]][parts[1]]...``, creating missing dicts on the way."""
    *parents, last = parts
    for part in parents:
        child = target.setdefault(part, {})
        if not isinstance(child, dict):
            raise SpecError(
                f"Can't set {path!r}: {part!r} is {child!r}, not a dict of options."
            )
        target = child
    target[last] = value
