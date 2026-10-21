"""The models the exporter tests walk: flex-pse models built from real specs.

``demo_model`` (repo-root conftest) is the six-hour demo plant. The others are
flex-pse's own round-trip corpus models, emitted to specs (see
:mod:`flex_orchestrator.testing.specs`). Between them they hold every structure
the exporter has to classify, including the tricky ones:

- ``logic_model``: indexed RO trains ``p.ro[0..2]`` over the set ``p.trains =
  [0, 1, 2]``, whose values are also time positions (a decoy), and
  ``startup`` / ``shutdown`` indexed over every time point but the first, by an
  anonymous Set (a real subset of time);
- ``surrogate_model``: a pump surrogate's ``coefficient_vars``, indexed by
  coefficient name only, and a battery with fixed dispatch values;
- ``network_model``: paths through a network of two plants.
"""

import copy

import pytest


def _build(spec):
    pytest.importorskip("flexops")
    from flex_orchestrator.flexpse import build

    return build(copy.deepcopy(spec))


@pytest.fixture
def logic_model():
    from flex_orchestrator.testing import LOGIC_SPEC

    return _build(LOGIC_SPEC)


@pytest.fixture
def surrogate_model():
    from flex_orchestrator.testing import SURROGATE_DISPATCH_SPEC

    return _build(SURROGATE_DISPATCH_SPEC)


@pytest.fixture
def network_model():
    from flex_orchestrator.testing import NETWORK_SPEC

    return _build(NETWORK_SPEC)
