"""How long does Acquirium take to store a model's timeseries?

A "model" here is N keys (10, 20, 100), each a timeseries of 10,000 values.
It is stored three ways, each in a fresh local Acquirium:

- experiment: declare each key as a timeseries variable of a study, start a
  run, ``record`` each key, finish (flex-orchestrator's ``streams=`` keys).
- streams: plain Acquirium streams. Register a data source and the streams,
  then write every key in one ``insert_timeseries_batch`` call.
- table: the whole model as one table in a study run, a column per key
  (flex-orchestrator's ``model_values`` snapshot, stored as Parquet).

Setup (declaring variables, registering streams) is timed apart from the
write, because a study declares its variables once and reuses them for every
run that follows. Every method reads one key back to check it was stored.

Run it with ``python benchmarking/store_timeseries.py``.
"""

import random
import tempfile
import time
from datetime import UTC, datetime, timedelta

import acquirium as aq
import polars as pl

KEYS = [10, 20, 100]
VALUES = 10_000
START = datetime(2026, 1, 1, tzinfo=UTC)
STAMPS = [START + timedelta(minutes=15 * i) for i in range(VALUES)]


def model(keys):
    """Return ``{key: [(time, value), ...]}`` for ``keys`` keys."""
    return {f"key_{k}": [(t, random.random()) for t in STAMPS] for k in range(keys)}


def experiment(ac, data):
    """Store ``data`` as one run of a study; return (setup, write) seconds."""
    t0 = time.perf_counter()
    study = ac.study.define("bench")
    variables = {key: study.output(key).timeseries() for key in data}
    t1 = time.perf_counter()
    run = study.start({"benchmark": True})
    streams = {key: variables[key].record(rows) for key, rows in data.items()}
    run.finish()
    t2 = time.perf_counter()
    assert streams["key_0"].dataframe().height == VALUES
    return t1 - t0, t2 - t1


def streams(ac, data):
    """Store ``data`` as plain streams of one source; return (setup, write) seconds."""
    t0 = time.perf_counter()
    ac.register_datasource("bench")
    ac.register_streams(
        [
            {"source_id": "bench", "ref_name": key, "value_kind": "numeric"}
            for key in data
        ]
    )
    t1 = time.perf_counter()
    ac.insert_timeseries_batch("bench", data)
    t2 = time.perf_counter()
    assert ac.stream("bench", "key_0").dataframe().height == VALUES
    return t1 - t0, t2 - t1


def table(ac, data):
    """Store ``data`` as one table in a study run; return (setup, write) seconds."""
    t0 = time.perf_counter()
    study = ac.study.define("bench")
    values = study.output("model_values").table()
    t1 = time.perf_counter()
    run = study.start({"benchmark": True})
    frame = pl.DataFrame(
        {"time": STAMPS, **{key: [v for _, v in rows] for key, rows in data.items()}}
    )
    values.record(frame)
    run.finish()
    t2 = time.perf_counter()
    stored = run.observations(values).latest().dataframe()
    assert stored["key_0"].len() == VALUES
    return t1 - t0, t2 - t1


def timed(method, data):
    """Run ``method`` in a fresh local Acquirium."""
    with tempfile.TemporaryDirectory() as data_dir:
        ac = aq.init(data_dir=data_dir)
        try:
            return method(ac, data)
        finally:
            aq.shutdown()


if __name__ == "__main__":
    rows = []
    for keys in KEYS:
        data = model(keys)
        for method in (experiment, streams, table):
            setup, write = timed(method, data)
            rows.append(
                (
                    keys,
                    method.__name__,
                    f"{setup:.2f}",
                    f"{write:.2f}",
                    f"{1000 * write / keys:.1f}",
                    f"{keys * VALUES / write:,.0f}",
                )
            )
    header = ("keys", "method", "setup s", "write s", "write ms/key", "values/s")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(6)]
    print(f"\n{VALUES:,} values per key; setup and write s are totals per model.\n")
    for row in [header, ["-" * w for w in widths], *rows]:
        print(
            "| "
            + " | ".join(f"{c!s:>{w}}" for c, w in zip(row, widths, strict=True))
            + " |"
        )
