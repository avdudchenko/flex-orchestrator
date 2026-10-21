"""The D5 storage benchmark: how long one model's results take to store (opt-in).

Every exported value is its own Acquirium stream or scalar (PLAN.md D5). The
models we run cover at most a month at 15-minute steps (31 x 96 = 2,976
points), with up to a few hundred keys, so this stores one model of that shape
with 10, 100 and 1,000 timeseries keys, plus 10 scalars. Each size is stored
twice: the first save declares every variable, the second reuses them (every
later run of a study).

Run it with ``pytest -m benchmark -s``. ``FLEXORCH_BENCH_STEPS`` changes the
points per key, and ``FLEXORCH_BENCH_MAX_S`` sets a limit on one save, which is
otherwise only reported.

The values are synthetic :class:`~flex_orchestrator.export.ModelData`, stored
with :meth:`~flex_orchestrator.orchestrator.Orchestrator.store_model_data`, so
the timing is storage, not building or exporting a Pyomo model.
"""

import os
import time
from datetime import datetime, timedelta

import pytest

from flex_orchestrator.export import ExportedSeries, ModelData

pytestmark = pytest.mark.benchmark

STEPS = int(os.environ.get("FLEXORCH_BENCH_STEPS", 31 * 96))
SCALARS = 10
MAX_S = os.environ.get("FLEXORCH_BENCH_MAX_S")


def synthetic(keys, run):
    start = datetime(2025, 1, 1)
    stamps = [start + timedelta(minutes=15 * i) for i in range(STEPS)]
    series = [
        ExportedSeries(
            f"plant.unit{k}.power",
            "timeseries",
            f"plant.unit{k}.power",
            [],
            [float(run + k + i % 96) for i in range(STEPS)],
            stamps,
            "kW",
            "KiloW",
            unit_verified=True,
        )
        for k in range(keys)
    ]
    series += [
        ExportedSeries(
            f"costing.capital_cost[unit{k}]",
            "scalar",
            "costing.capital_cost",
            [f"unit{k}"],
            [float(run * k)],
            None,
            "USD",
            None,
        )
        for k in range(SCALARS)
    ]
    return ModelData(series)


@pytest.mark.parametrize("keys", [10, 100, 1000])
def test_store_one_model(acquirium_local, study_name, keys):
    from flex_orchestrator.backend import AcquiriumBackend
    from flex_orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(AcquiriumBackend(acquirium_local))
    orch.create_study(study_name)
    timings = []
    for run in range(2):
        data = synthetic(keys, run)
        start = time.perf_counter()
        stored = orch.store_model_data(f"run{run}", data)
        timings.append(time.perf_counter() - start)
        assert len(stored.keys) == keys + SCALARS

    start = time.perf_counter()
    back = orch.model_results("run1")
    read = time.perf_counter() - start
    assert (
        back["plant.unit3.power"].values
        == synthetic(keys, 1)["plant.unit3.power"].values
    )

    first, again = timings
    print(
        f"\n{keys:5d} keys x {STEPS} points + {SCALARS} scalars: "
        f"first save {first:6.1f} s, next save {again:6.1f} s "
        f"({again / keys * 1000:.0f} ms/key, {keys * STEPS / again:,.0f} points/s), "
        f"read back {read:5.1f} s"
    )
    if MAX_S is not None:
        assert max(timings) <= float(MAX_S)
