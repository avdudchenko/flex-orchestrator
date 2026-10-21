"""Repo-wide pytest configuration: tier-marker enforcement and unit-tier guards.

Mirrors flex-pse's conftest: every test carries exactly one tier marker, a
``needs_<solver>`` marker skips (never fails) when that solver is missing, and
unit tests can't invoke a solver or open a network connection.
"""

import importlib.util

import pytest

TIER_MARKERS = {"unit", "component", "integration", "benchmark"}

HAS_FLEXPSE = importlib.util.find_spec("flexops") is not None


def pytest_collection_modifyitems(config, items):
    errors = []
    for item in items:
        tiers = TIER_MARKERS & {m.name for m in item.iter_markers()}
        if len(tiers) != 1:
            errors.append(
                f"{item.nodeid}: needs exactly one tier marker "
                f"({'/'.join(sorted(TIER_MARKERS))}), got {sorted(tiers) or 'none'}"
            )
    if errors:
        raise pytest.UsageError("\n".join(errors))

    needs = {
        marker.name[len("needs_") :]
        for item in items
        for marker in item.iter_markers()
        if marker.name.startswith("needs_")
    }
    if not needs:
        return
    available = {}
    if HAS_FLEXPSE:
        from flexcore.solvers.registry import available_solvers

        available = available_solvers()
    for item in items:
        for marker in item.iter_markers():
            if marker.name.startswith("needs_"):
                name = marker.name[len("needs_") :]
                if name not in available:
                    item.add_marker(
                        pytest.mark.skip(reason=f"solver {name} not installed")
                    )


@pytest.fixture(autouse=True)
def _no_solver_in_unit_tier(request, monkeypatch):
    """Block solver invocation and network access during unit-tier tests."""
    if "unit" not in {m.name for m in request.node.iter_markers()}:
        return

    import socket

    def _forbidden(*args, **kwargs):
        raise RuntimeError("solver invocation is forbidden in unit-tier tests")

    if HAS_FLEXPSE:
        from flexcore.solvers import facade

        monkeypatch.setattr(facade.SolverFacade, "solve", _forbidden)

    def _no_network(*args, **kwargs):
        raise RuntimeError("network access is forbidden in unit-tier tests")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network)
