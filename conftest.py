"""Repo-wide pytest configuration: tier-marker enforcement and unit-tier guards.

Mirrors flex-pse's conftest: every test carries exactly one tier marker, a
``needs_<solver>`` marker skips (never fails) when that solver is missing, and
unit tests can't invoke a solver or open a network connection.
"""

import copy
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


@pytest.fixture
def demo_spec():
    """A fresh copy of the shared flex-pse demo flowsheet spec."""
    from flex_orchestrator.testing import DEMO_SPEC

    return copy.deepcopy(DEMO_SPEC)


@pytest.fixture
def demo_model(demo_spec):
    """The flex-pse demo, built (not solved) with the default build hook."""
    pytest.importorskip("flexops")
    from flex_orchestrator.flexpse import build

    return build(demo_spec)


def _build(spec):
    pytest.importorskip("flexops")
    from flex_orchestrator.flexpse import build

    return build(copy.deepcopy(spec))


@pytest.fixture
def logic_model():
    """flex-pse's indexed RO trains ``p.ro[0..2]`` over ``p.trains = [0, 1, 2]``.

    ``p.trains`` holds valid time positions but isn't time (a decoy), and each
    train's ``startup`` / ``shutdown`` is indexed over every time point but the
    first, by an anonymous Set (a real subset of time).
    """
    from flex_orchestrator.testing import LOGIC_SPEC

    return _build(LOGIC_SPEC)


@pytest.fixture
def surrogate_model():
    """A pump surrogate's ``coefficient_vars`` and a battery with fixed dispatch."""
    from flex_orchestrator.testing import SURROGATE_DISPATCH_SPEC

    return _build(SURROGATE_DISPATCH_SPEC)


@pytest.fixture
def network_model():
    """Paths through a network of two plants."""
    from flex_orchestrator.testing import NETWORK_SPEC

    return _build(NETWORK_SPEC)


@pytest.fixture
def memory_backend():
    """An empty in-memory backend."""
    from flex_orchestrator.backend import MemoryBackend

    return MemoryBackend()


@pytest.fixture(scope="session")
def acquirium_local(tmp_path_factory):
    """A local Acquirium (DuckDB) for the whole session; tests use fresh studies.

    Starting the server takes about half a minute, so it is started once.
    """
    aq = pytest.importorskip("acquirium")
    client = aq.init(
        data_dir=tmp_path_factory.mktemp("acquirium"), exact_only=True, timeout=300
    )
    yield client
    aq.shutdown()


@pytest.fixture(
    params=[
        pytest.param("memory", marks=pytest.mark.unit),
        pytest.param("acquirium", marks=pytest.mark.component),
    ]
)
def backend(request):
    """Each backend in turn: memory (unit tier) and Acquirium (component tier)."""
    if request.param == "memory":
        return request.getfixturevalue("memory_backend")
    from flex_orchestrator.backend import AcquiriumBackend

    return AcquiriumBackend(request.getfixturevalue("acquirium_local"))


@pytest.fixture
def study_name():
    """A study name no other test uses (the Acquirium server is shared)."""
    import uuid

    return f"study-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def orch(backend, study_name):
    """An orchestrator on each backend, with a fresh study selected."""
    from flex_orchestrator.orchestrator import Orchestrator

    orchestrator = Orchestrator(backend)
    orchestrator.create_study(study_name)
    return orchestrator
