"""Hooks for the Ray tests, importable by name on a worker.

``build`` makes a tiny Pyomo model; a task's overrides steer it:

- ``sleep``: seconds to sleep while building (a hung task);
- ``crash_once``: a file path; the first build that sees it missing creates it
  and kills its worker process;
- ``fail``: raise while building.

Any other override is added to ``x``. The model also records the worker's
process id and ``OMP_NUM_THREADS``.
"""

import os
import time
from pathlib import Path

import pyomo.environ as pyo

from flex_orchestrator.hooks import SolveOutcome

STEER = ("sleep", "crash_once", "fail")


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
    m.pid = pyo.Var(initialize=os.getpid())
    m.threads = pyo.Var(initialize=float(os.environ.get("OMP_NUM_THREADS", "0")))
    return m


def solve(model, **options) -> SolveOutcome:
    return SolveOutcome(status="ok", termination="optimal", solver="fake")


HOOKS = {
    "build": "flex_orchestrator.study.tests.ray_hooks:build",
    "solve": "flex_orchestrator.study.tests.ray_hooks:solve",
}


def boom(x):
    if x == 1:
        raise ValueError("job 1 failed")
    time.sleep(x)
    return x
