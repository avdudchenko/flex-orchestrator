"""The M5 Ray benchmark: 30 tasks in-process and on 8 Ray workers (opt-in).

The model is the RO plant (feed -> RO -> tank -> product) over a month at
15-minute steps (2,976 points), priced by a time-of-use tariff with demand
charges. Three surrogates of the RO's power are fitted from stored operating
data (an average intensity and two linear fits) and swept against ten feed
withdrawal limits: 3 x 10 = 30 build-solve-store tasks, stored in a local
Acquirium.

The same study runs with ``max_workers=1`` and with ``max_workers=8``, into
two studies, and the stored results must be the same. It reports the speedup,
each step's time (build, solve, export, store), the worker memory, the Ray
start-up time and the overhead Ray adds to a task that does nothing, and
checks that this process's Ray is its own, not Acquirium's.

Run it with ``pytest -m benchmark -s -k ray``. ``FLEXORCH_BENCH_OUT`` names a
JSON file for the numbers; ``FLEXORCH_BENCH_MIN_SPEEDUP`` (default 5) is the
speedup it must reach on a machine with at least 8 CPUs.
"""

import json
import os
import shutil
import statistics
import time
from importlib.resources import files

import numpy as np
import pandas as pd
import pytest

pytestmark = [pytest.mark.benchmark, pytest.mark.needs_highs, pytest.mark.timeout(7200)]

ray = pytest.importorskip("ray")
pytest.importorskip("flexops")
pytest.importorskip("flexparameterize")

TARIFF = files("flex_orchestrator.testing") / "data" / "tariff_tou_demo.csv"
WORKERS = 8
WITHDRAWALS = list(range(185, 285, 10))  # m^3/hr; below ~180 a month can't be met
MIN_SPEEDUP = float(os.environ.get("FLEXORCH_BENCH_MIN_SPEEDUP", 5))


def month_spec():
    """The RO plant over July 2025 at 15-minute steps."""
    p, unit = "plant", {"kind": "unit"}
    flow = "m^3/hr"
    return {
        "schema_version": "0.1.1",
        "name": "ro_month",
        "elements": [
            {
                "kind": "time",
                "start_date": "2025-07-01",
                "end_date": "2025-08-01",
                "time_step": "15 min",
            },
            {
                "kind": "property_package",
                "name": "properties",
                "property_class": "SimpleAqueousFlow",
            },
            {"kind": "costing", "name": "costing", "tariff_source": "tariff.csv"},
            {"kind": "plant", "name": p},
            {
                **unit,
                "name": f"{p}.feed",
                "unit_model_class": "Feed",
                "construction_options": {
                    "max_withdrawal": {"value": 300, "units": flow}
                },
                "connections": [{"port": "outlet_a", "to": f"{p}.ro.inlet"}],
            },
            {
                **unit,
                "name": f"{p}.ro",
                "unit_model_class": "ReverseOsmosis",
                "construction_options": {
                    "recovery": 0.5,
                    "energy_intensity": {"value": 3.0, "units": "kWh/m^3"},
                },
                "connections": [
                    {"port": "outlet_a", "to": f"{p}.tank.inlet"},
                    {"port": "outlet_b", "to": f"{p}.brine.inlet_a"},
                ],
            },
            {
                **unit,
                "name": f"{p}.tank",
                "unit_model_class": "Tank",
                "costing_package": None,
                "construction_options": {
                    "max_volume": {"value": 1500.0, "units": "m^3"},
                    "initial_volume": {"value": 750.0, "units": "m^3"},
                },
                "connections": [{"port": "outlet", "to": f"{p}.product.inlet_a"}],
            },
            {
                **unit,
                "name": f"{p}.product",
                "unit_model_class": "Product",
                "construction_options": {
                    "min_demand": {"value": 90.0, "units": flow},
                    "max_demand": {"value": 90.0, "units": flow},
                },
            },
            {**unit, "name": f"{p}.brine", "unit_model_class": "Product"},
            {"kind": "objective", "name": "objective"},
        ],
    }


def operating_data():
    """Four weeks of hourly RO data on a parabolic specific-energy curve."""
    rng = np.random.default_rng(0)
    time_ = pd.date_range("2025-06-01", periods=28 * 24, freq="h", tz="UTC")
    permeate = rng.uniform(30, 150, len(time_))
    sec = 2.5 + 4e-4 * (permeate - 90) ** 2
    power = sec * permeate * rng.normal(1, 0.02, len(time_))
    return pd.DataFrame({"permeate": permeate, "power_electrical": power}, time_)


SURROGATES = ["fixed_intensity", "linear", "linear_week4"]
FITS = [
    {
        "tag": "{name}",
        "X": {"dataset": "ro_operation", "columns": ["permeate"]},
        "y": {"dataset": "ro_operation", "columns": ["power_electrical"]},
        "variants": [
            {"name": "fixed_intensity", "regressor": "constant_intensity"},
            {"name": "linear", "regressor": "multilinear"},
            {
                "name": "linear_week4",
                "regressor": "multilinear",
                "window": {"start": "2025-06-22T00:00:00Z"},
            },
        ],
    }
]


def study(workers):
    return {
        "base_model_spec": "base",
        "sweeps": [
            {"type": "surrogate", "unit": "plant.ro", "surrogates": SURROGATES},
            {
                "type": "configuration_option",
                "name": "withdrawal",
                "path": "plant.feed.max_withdrawal.value",
                "values": WITHDRAWALS,
            },
        ],
        "export": {
            "include": ["plant.*", "costing.aggregate_*", "costing.total_cost"],
            "exclude": ["*_ref", "*_state.*"],
        },
        "output_name": "{surrogate}_{withdrawal}",
        "execution": {"max_workers": workers},
    }


def setup(orch, name, inputs):
    orch.create_study(name)
    orch.store_data(
        "ro_operation",
        operating_data(),
        units={"permeate": "m^3/hr", "power_electrical": "kW"},
    )
    orch.store_model_spec("base", month_spec(), base_dir=inputs)
    orch.fit_surrogate(**FITS[0])


def snapshot(orch):
    """Every stored value, by tag and key, to compare the two runs."""
    return {
        r.tag: {s.key: (s.values, s.unit_qudt, s.timestamps) for s in r.data()}
        for r in orch.stored_model_results()
    }


def summary(run):
    steps = {"build": [], "solve": [], "export": [], "store": []}
    for r in run.results:
        for step in steps:
            steps[step].append(r.seconds.get(step, 0.0))
    return {
        step: {"mean": statistics.mean(v), "max": max(v), "total": sum(v)}
        for step, v in steps.items()
    }


def test_ray_benchmark(tmp_path):
    from flex_orchestrator import Orchestrator
    from flex_orchestrator.study.tests.ray_hooks import HOOKS
    from flex_orchestrator.testing import memory_orchestrator

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    shutil.copy(TARIFF, inputs / "tariff.csv")
    report = {"tasks": len(SURROGATES) * len(WITHDRAWALS), "workers": WORKERS}
    report["cpus"] = os.cpu_count()

    with Orchestrator(data_dir=tmp_path / "store", environ={}) as orch:
        setup(orch, "serial", inputs)
        started = time.perf_counter()
        serial = orch.execute_study(study(1))
        report["serial_s"] = time.perf_counter() - started
        assert serial.counts() == {"succeeded": report["tasks"]}
        serial_values = snapshot(orch)

        # Ray's start-up, timed on its own so the comparison is steady state.
        assert not ray.is_initialized()
        started = time.perf_counter()
        from flex_orchestrator.parallel import connect

        connect()
        report["ray_start_s"] = time.perf_counter() - started
        context = ray.get_runtime_context()
        report["ray_namespace"] = context.namespace
        report["ray_cpus"] = ray.cluster_resources().get("CPU")

        # The overhead of a task that does nothing: 64 of them on 8 workers.
        noop = memory_orchestrator(study="noop")
        noop.store_model_spec("base", {"value": 1.0})
        noop_cfg = {
            "base_model_spec": "base",
            "hooks": HOOKS,
            "sweeps": [{"type": "variable", "path": "v", "values": list(range(64))}],
            "output_name": "n{v}",
        }
        noop.execute_study(noop_cfg, max_workers=WORKERS)  # warm the workers
        noop.delete_study("noop")
        noop.create_study("noop")
        noop.store_model_spec("base", {"value": 1.0})
        started = time.perf_counter()
        noop.execute_study(noop_cfg, max_workers=WORKERS)
        report["noop_task_s"] = (time.perf_counter() - started) * WORKERS / 64

        setup(orch, "parallel", inputs)
        started = time.perf_counter()
        parallel = orch.execute_study(study(WORKERS))
        report["parallel_s"] = time.perf_counter() - started
        assert parallel.counts() == {"succeeded": report["tasks"]}
        assert snapshot(orch) == serial_values  # the same stored results
        report["same_results"] = True

        # Acquirium's server is its own process; this process's Ray is ours.
        report["acquirium_alive"] = orch.acquirium is not None
        report["ray_in_this_process"] = ray.is_initialized()

    report["speedup"] = report["serial_s"] / report["parallel_s"]
    report["serial_steps"] = summary(serial)
    report["parallel_steps"] = summary(parallel)
    report["store_rate_per_s"] = 1 / report["parallel_steps"]["store"]["mean"]
    report["worker_rss_mb"] = {
        "mean": statistics.mean(r.rss_mb for r in parallel.results),
        "max": max(r.rss_mb for r in parallel.results),
    }
    print(json.dumps(report, indent=2))
    out = os.environ.get("FLEXORCH_BENCH_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    if (os.cpu_count() or 1) >= WORKERS:
        assert report["speedup"] >= MIN_SPEEDUP
