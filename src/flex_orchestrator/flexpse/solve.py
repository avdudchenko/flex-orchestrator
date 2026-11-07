"""The flex-pse solve hook: pick a solver with ``get_solver`` and report the outcome."""

import time
from typing import Any

import pyomo.environ as pyo
from flexcore.solvers import get_solver

from flex_orchestrator.hooks import SolveOutcome


def solve(
    model: pyo.Block, *, prefer: str | None = None, **options: Any
) -> SolveOutcome:
    """Solve a model with the best available solver.

    Args:
        model: The built model.
        prefer: A solver name to try first (see
            :func:`flexcore.solvers.get_solver`). ``None`` uses the spec's
            ``solver`` field when the model was built from one.
        **options: Passed to :meth:`flexcore.solvers.SolverFacade.solve`: ``tee``,
            ``warmstart``, ``mip_gap``, ``time_limit`` and ``options``.

    Returns:
        ``"ok"`` when the termination is optimal, ``"failed"`` otherwise.

    Raises:
        flexcore.exceptions.FlexSolverError: If no installed solver can solve
            the model. :func:`~flex_orchestrator.hooks.run_solve` records this
            as an ``"error"`` outcome.

    Example:
        >>> outcome = solve(model, prefer="highs")  # doctest: +SKIP
    """
    if prefer is None:
        # build_model keeps the spec it built from on the model.
        prefer = getattr(getattr(model, "_flex_spec", None), "solver", None)
    start = time.perf_counter()
    solver = get_solver(model=model, prefer=prefer)
    results = solver.solve(model, **options)
    message = getattr(results.solver, "message", None)
    return SolveOutcome(
        status="ok" if pyo.check_optimal_termination(results) else "failed",
        termination=str(results.solver.termination_condition),
        message=str(message) if message else None,
        wall_time=time.perf_counter() - start,
        solver=solver.name,
    )
