"""Hooks that need no model library or solver, for tests.

Both are module-level functions, so they resolve from import strings
(``"flexorchestrator.testing:fake_build"``) and run on Ray workers like real
hooks.
"""

import copy
from dataclasses import dataclass, field
from typing import Any

from flexorchestrator.hooks import SolveOutcome, SolveStatus


@dataclass
class FakeModel:
    """What :func:`fake_build` returns: the spec and overrides it was given.

    Attributes:
        spec: A deep copy of the spec.
        overrides: The overrides.
        solved: Set by :func:`fake_solve`.
    """

    spec: dict
    overrides: dict[str, Any] = field(default_factory=dict)
    solved: bool = False


def fake_build(spec: dict, **overrides: Any) -> FakeModel:
    """Build a :class:`FakeModel`.

    Args:
        spec: Any JSON spec.
        **overrides: Recorded on the model.

    Returns:
        The fake model.

    Example:
        >>> fake_build({"a": 1}, x=2).overrides
        {'x': 2}
    """
    return FakeModel(spec=copy.deepcopy(spec), overrides=dict(overrides))


def fake_solve(
    model: Any, *, status: SolveStatus = "ok", **options: Any
) -> SolveOutcome:
    """Pretend to solve, ending however ``status`` says.

    Args:
        model: Usually a :class:`FakeModel`; marked solved when it is one.
        status: ``"ok"`` (optimal), ``"failed"`` (infeasible), or ``"error"``,
            which raises the way a crashing solver would.
        **options: Ignored.

    Returns:
        The outcome.

    Raises:
        RuntimeError: If ``status`` is ``"error"``.

    Example:
        >>> fake_solve(None, status="failed").termination
        'infeasible'
    """
    if status == "error":
        raise RuntimeError("fake solver crashed")
    if isinstance(model, FakeModel):
        model.solved = True
    return SolveOutcome(
        status=status,
        termination="optimal" if status == "ok" else "infeasible",
        solver="fake",
    )
