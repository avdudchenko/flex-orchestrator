"""The nested sweep run twice, in-process and on Ray, timed.

``sweep_nested.py``'s study over a month instead of two days, so each solve
takes a few seconds, with more withdrawal limits: 2 surrogates x 12 limits =
24 solves. It runs once with ``max_workers=1``, which builds and solves every
task in this process one after another, and once with half of this machine's
cores, which builds and solves them on local Ray at the same time. Either way
the main process stores each result, one at a time.

Both runs store the same results. The Ray time includes starting Ray and its
workers, a few seconds that only pay off when there's enough to solve: over
``sweep_nested.py``'s two days, Ray is slower. It uses a store of its own in
a temporary folder, deleted at the end.

    python examples/ro_study/compare_ray.py
"""

import os
import tempfile
import time

import flexops as fo
from common import MODEL, new_study, ro_model
from sweep_nested import STUDY

from flex_orchestrator import Orchestrator

WORKERS = max(2, (os.cpu_count() or 2) // 2)
LIMITS = list(range(200, 320, 10))  # 12 withdrawal limits, in m³/hr

# The base spec over a month, the longest horizon a time block allows.
MONTH = fo.emit_model(ro_model(), relative_to=MODEL).model_dump(mode="json")
for element in MONTH["elements"]:
    if element.get("kind") == "time":
        element["end_date"] = "2025-08-08"

study = {
    **STUDY,
    "base_model_spec": "month",
    "sweeps": [STUDY["sweeps"][0], {**STUDY["sweeps"][1], "values": LIMITS}],
    "export": {"include": []},  # store only the scalars
}


def timed(orch: Orchestrator, workers: int) -> tuple[float, dict[str, float]]:
    """Run the study on ``workers``; return the seconds and each tag's cost."""
    new_study(f"ro_ray_{workers}", orch)
    orch.store_model_spec("month", MONTH, base_dir=MODEL)
    t0 = time.perf_counter()
    run = orch.execute_study(study, max_workers=workers)
    seconds = time.perf_counter() - t0
    assert run.counts() == {"succeeded": len(run.tasks)}, run.counts()
    costs = {
        r.tag: r.scalars()["costing.aggregate_operating_cost"]
        for r in orch.stored_model_results()
    }
    return seconds, costs


if __name__ == "__main__":
    with (
        tempfile.TemporaryDirectory() as data_dir,
        Orchestrator(data_dir=data_dir) as orch,
    ):
        serial, serial_costs = timed(orch, 1)
        parallel, ray_costs = timed(orch, WORKERS)
    assert serial_costs == ray_costs  # the same results either way

    print(f"\n{len(serial_costs)} solves, {os.cpu_count()} cores\n")
    print("| run | workers | time s |")
    print("| --- | ------- | ------ |")
    print(f"| in-process | 1 | {serial:6.1f} |")
    print(f"| Ray | {WORKERS} | {parallel:6.1f} |")
    print(f"\nRay was {serial / parallel:.1f}x as fast.")
