"""The flex-pse ``time_sets`` hook: which Sets of a built model are time."""

import pyomo.environ as pyo
from flexcore.exceptions import FlexConfigError
from flexops.core.time_block import find_time_block

_TIME_SUBSET_VARS = ("startup", "shutdown")
"""Vars flex-pse indexes over part of the horizon. ``add_startup_shutdown``
documents ``startup`` and ``shutdown`` as "indexed over ``t >= 1``", built from
a plain list, so their index Set has no link to ``time_index``."""


def time_sets(model: pyo.Block) -> list[pyo.Set]:
    """Return a flex-pse model's time Sets, for the exporter.

    The TimeBlock's ``time_index`` comes first. After it come the index Sets of
    every unit's ``startup`` / ``shutdown`` Vars, found on the units themselves
    and so registered by identity. A Set that merely holds time-like integers,
    such as a ``set`` element ``[0, 1, 2]`` indexing RO trains, is never
    included.

    Args:
        model: A built flex-pse model.

    A merged design model has one TimeBlock per horizon: each horizon's Sets
    follow in turn, its ``time_index`` first.

    Returns:
        The time Sets, main one first, or ``[]`` if the model doesn't have
        exactly one TimeBlock (a user's own model, say). The exporter then finds
        time itself, and reports several TimeBlocks as ambiguous.

    Example:
        >>> export_model_data(model, time_sets=time_sets(model))  # doctest: +SKIP
    """
    if hasattr(model, "_design_spec"):
        return [s for horizon in model.horizon.values() for s in time_sets(horizon)]
    try:
        time_index = find_time_block(model).time_index
    except FlexConfigError:
        return []
    found = [time_index]
    times = set(time_index)
    for block in model.block_data_objects(active=True, descend_into=True):
        for name in _TIME_SUBSET_VARS:
            var = block.component(name)
            if var is None or var.ctype is not pyo.Var or not var.is_indexed():
                continue
            index_set = var.index_set()
            if all(index_set is not s for s in found) and set(index_set) <= times:
                found.append(index_set)
    return found
