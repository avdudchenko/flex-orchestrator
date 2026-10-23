"""Helpers for testing studies, hooks and plugins without a database or solver."""

from flex_orchestrator.testing.hooks import FakeModel, fake_build, fake_solve
from flex_orchestrator.testing.orchestrators import (
    FAKE_HOOKS,
    memory_orchestrator,
    run_command_in_test,
)
from flex_orchestrator.testing.specs import (
    DEMO_SPEC,
    LINEAR_POWER,
    LOGIC_SPEC,
    NETWORK_SPEC,
    SURROGATE_DISPATCH_SPEC,
)

__all__ = [
    "DEMO_SPEC",
    "FAKE_HOOKS",
    "LINEAR_POWER",
    "LOGIC_SPEC",
    "NETWORK_SPEC",
    "SURROGATE_DISPATCH_SPEC",
    "FakeModel",
    "fake_build",
    "fake_solve",
    "memory_orchestrator",
    "run_command_in_test",
]
