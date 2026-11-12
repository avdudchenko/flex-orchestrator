"""Hooks for the Ray tests, importable by name on a worker.

``build`` makes a tiny Pyomo model; a task's overrides steer it:

- ``sleep``: seconds to sleep while building (a hung task);
- ``crash_once``: a file path; the first build that sees it missing creates it
  and kills its worker process;
- ``fail``: raise while building.

Any other override is added to ``x``. The model also records which worker
process built it (``process``) and its ``OMP_NUM_THREADS``.
"""

import os
import secrets
import time
from pathlib import Path

import pyomo.environ as pyo

from flexorchestrator.hooks import SolveOutcome

STEER = ("sleep", "crash_once", "fail")

# Drawn once per process, when a worker first imports this module. Unlike the
# pid, a later process can't get the same one: Windows reuses pids quickly.
# 52 bits, so it is exact as the float a Pyomo Var holds.
PROCESS = float(secrets.randbits(52))


def build(spec: dict, **overrides):
    if overrides.get("sleep"):
        time.sleep(overrides["sleep"])
    marker = overrides.get("crash_once")
    if marker and not Path(marker).exists():
        Path(marker).write_text("crashed")
        os._exit(1)
    if overrides.get("fail"):
        raise RuntimeError("build failed on purpose")
    m = pyo.ConcreteModel()
    extra = sum(float(v) for k, v in overrides.items() if k not in STEER)
    m.x = pyo.Var(initialize=spec["value"] + extra)
    m.process = pyo.Var(initialize=PROCESS)
    m.threads = pyo.Var(initialize=float(os.environ.get("OMP_NUM_THREADS", "0")))
    return m


def solve(model, **options) -> SolveOutcome:
    return SolveOutcome(status="ok", termination="optimal", solver="fake")


HOOKS = {
    "build": "flexorchestrator.study.tests.ray_hooks:build",
    "solve": "flexorchestrator.study.tests.ray_hooks:solve",
}


def boom(x):
    if x == 1:
        raise ValueError("job 1 failed")
    time.sleep(x)
    return x
