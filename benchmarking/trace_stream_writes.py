"""Where does a stream write spend its time?

Same model as ``store_timeseries.py`` (100 keys × 10,000 values), in a fresh
local Acquirium.

streams: ``insert_timeseries_batch`` sends every key as JSON in one request.
Its steps are timed one by one:

- client encode: build the request body (pydantic ``model_dump`` + JSON);
- server parse: what the server does to the body before it writes (JSON +
  pydantic validation of every ``(datetime, value)`` pair), repeated here;
- server total: POST the already-encoded body, i.e. parse plus database write;
- database write: the server total minus the parse;
- arrow: the same data through ``insert_timeseries_arrow`` (no JSON, no
  pydantic), for comparison.

experiment: ``variable.record(rows)`` makes three calls per key. Each is timed
by wrapping the client method it uses.

Run it with ``python benchmarking/trace_stream_writes.py``.
"""

import json
import random
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import acquirium as aq
import pyarrow as pa
import requests
from acquirium.internals.models import StreamInsert
from pydantic import TypeAdapter

KEYS = 100
VALUES = 10_000
START = datetime(2026, 1, 1, tzinfo=UTC)
STAMPS = [START + timedelta(minutes=15 * i) for i in range(VALUES)]
DATA = {f"key_{k}": [(t, random.random()) for t in STAMPS] for k in range(KEYS)}


def register(ac, source):
    """Register ``source`` and a plain stream per key; return the seconds taken."""
    t0 = time.perf_counter()
    ac.register_datasource(source)
    ac.register_streams(
        [
            {"source_id": source, "ref_name": key, "value_kind": "numeric"}
            for key in DATA
        ]
    )
    return time.perf_counter() - t0


def clock(fn, *args, **kwargs):
    """Return the seconds ``fn(*args, **kwargs)`` takes."""
    t0 = time.perf_counter()
    fn(*args, **kwargs)
    return time.perf_counter() - t0


def streams(ac):
    """Return ``{step: seconds}`` for one write of every key."""
    steps = {"register_streams (every key, one call)": register(ac, "batch")}
    steps["insert_timeseries_batch (all of it)"] = clock(
        ac.insert_timeseries_batch, "batch", DATA
    )

    t0 = time.perf_counter()
    payload = [
        StreamInsert(source_id="raw", ref_name=key, values=rows).model_dump(mode="json")
        for key, rows in DATA.items()
    ]
    body = json.dumps(payload)
    steps["  client encode"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    TypeAdapter(list[StreamInsert]).validate_python(json.loads(body))
    parse = time.perf_counter() - t0
    steps["  server parse (JSON + pydantic)"] = parse

    register(ac, "raw")
    t0 = time.perf_counter()
    response = requests.post(
        f"{ac.client.base_url}/insert_timeseries",
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=600,
    )
    response.raise_for_status()
    total = time.perf_counter() - t0
    steps["  server total (POST of encoded body)"] = total
    steps["  database write (total - parse)"] = total - parse

    register(ac, "arrow")
    table = pa.table(
        {
            "ts": [t for rows in DATA.values() for t, _ in rows],
            "ref_name": [key for key, rows in DATA.items() for _ in rows],
            "value": [v for rows in DATA.values() for _, v in rows],
        }
    )
    steps["insert_timeseries_arrow (all of it)"] = clock(
        ac.insert_timeseries_arrow, "arrow", table
    )
    return steps


def experiment(ac):
    """Return ``{step: seconds}`` for one run recording every key."""
    study = ac.study.define("bench")
    variables = {key: study.output(key).timeseries() for key in DATA}
    run = study.start({"benchmark": True})

    spent, calls = defaultdict(float), defaultdict(list)
    for owner, name in [
        (ac, "register_streams"),
        (ac, "insert_timeseries"),
        (study.client, "observe_experiment"),
    ]:
        original = getattr(owner, name)

        def timed(*args, _fn=original, _name=name, **kwargs):
            t0 = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                calls[_name].append(time.perf_counter() - t0)
                spent[_name] += calls[_name][-1]

        setattr(owner, name, timed)

    t0 = time.perf_counter()
    for key, rows in DATA.items():
        variables[key].record(rows)
    total = time.perf_counter() - t0
    run.finish()

    steps = {"variable.record (all of it)": total}
    steps["  register_streams (run stream)"] = spent["register_streams"]
    registers = calls["register_streams"]
    steps["    of which, mean of the first 10 keys"] = sum(registers[:10]) / 10 * KEYS
    steps["    of which, mean of the last 10 keys"] = sum(registers[-10:]) / 10 * KEYS
    steps["  insert_timeseries (samples)"] = spent["insert_timeseries"]
    steps["  observe_experiment (link to run)"] = spent["observe_experiment"]
    steps["  client-side rest"] = total - sum(spent.values())
    return steps


def show(title, steps):
    """Print ``steps`` as a table."""
    print(f"\n{title}: {KEYS} keys × {VALUES:,} values\n")
    width = max(len(name) for name in steps)
    print(f"| {'step':<{width}} | total s | ms/key |")
    print(f"| {'-' * width} | ------- | ------ |")
    for name, seconds in steps.items():
        print(f"| {name:<{width}} | {seconds:7.2f} | {1000 * seconds / KEYS:6.1f} |")


if __name__ == "__main__":
    for method in (streams, experiment):
        with tempfile.TemporaryDirectory() as data_dir:
            ac = aq.init(data_dir=data_dir)
            try:
                show(method.__name__, method(ac))
            finally:
                aq.shutdown()
