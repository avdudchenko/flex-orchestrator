# 0001: Running studies on Ray

- **Status:** accepted
- **Date:** 2026-10-23

## Decision

A study with `max_workers > 1` runs its tasks as Ray tasks, and Ray is the
only parallel backend. The task is split into three steps:

1. **`prepare`**, in the main process: check the tag, store the derived spec,
   and read the spec and its files.
2. **`execute`**, on a worker: build, solve and export. The worker gets only
   plain data and hook import strings.
3. **`finish`**, in the main process: map the units and store the result.

A `ray.wait` loop in the main process keeps `max_workers` tasks in flight,
prepares the next tasks while the workers solve, stores each result as it
arrives, and enforces timeouts, retries and cancellation. Workers never hold
credentials and never touch the store. The stored results are the same as
with `max_workers=1`. This is tested on every run, and the benchmark below
compares every stored value.

## Benchmark

`src/flexorchestrator/study/tests/test_benchmark_ray.py`, run with
`pytest -m benchmark -k ray -s`.

- **Model:** the RO plant (feed → RO → tank → product) over a month at
  15-minute steps (2,976 points), priced by a time-of-use tariff with demand
  charges, and solved with HiGHS.
- **Tasks:** 3 fitted surrogates of the RO's power × 10 feed withdrawal
  limits = 30 tasks, stored in a local Acquirium.
- **Stored:** the plant's variables and the aggregate costs.
- **Machine:** Windows 11, 20 logical CPUs, local Ray 2.59.

| | `max_workers=1` | `max_workers=8` |
| --- | --- | --- |
| Wall time for 30 tasks | 423 s | 75 s (**5.6×**) |
| Build, mean per task | 5.0 s | 5.7 s |
| Solve, mean per task | 7.6 s | 9.6 s |
| Export, mean per task | 0.35 s | 0.35 s |
| Store (main process), mean per task | 0.55 s | 0.95 s |
| Worker memory (RSS), mean / max | | 421 / 514 MB |

| Overhead | |
| --- | --- |
| Starting local Ray (once per process) | 6.0 s |
| Ray's cost for a task that does nothing (64 tasks on 8 workers, warm) | 0.03 s per task |
| Write rate in the main process | about 1 result/s, which overlaps the solves |

Findings:

- **Ray's own overhead doesn't matter here.** A few hundredths of a second per
  task is nothing next to 13 s of build and solve. The first task on each new
  worker pays a few seconds of process start-up and imports, because the
  `runtime_env` (one thread per solver library) gives the orchestrator its own
  workers.
- **The main process was the limit, not Ray.** The first version prepared a
  task only when a slot freed up, and read the same base and surrogate specs
  again for every task, costing about 1.4 s of Acquirium reads per task. That
  gave 4.1×. Two changes got it to 5.6×:
  - a study run reads each spec once (`Orchestrator._caching_specs`);
  - the loop prepares up to `max_workers` tasks ahead while the workers
    solve.

  The serial run got faster from the same cache, from 486 s to 423 s.
- **Parallel solves are slower each.** Mean solve time rises from 7.6 s to
  9.6 s, and build from 5.0 s to 5.7 s, with 8 at once. That's memory
  bandwidth and turbo clocks on one laptop CPU. With 8 workers the speedup
  ceiling is about 6.5× here.
- **Building is about 40% of the work** (5 s of 13 s), mostly flex-pse
  building the tariff's cost terms. A pool of Ray actors that keep built
  models (deferred) would only help tasks that share a structure, such as
  a price sweep. Configuration and surrogate sweeps change the model, so each
  needs its own build anyway. We keep it deferred.
- **Memory** is about 0.4–0.5 GB per worker for a month at 15-minute steps.
  `execution.resources.memory` can reserve it, and `execution.max_calls` can
  recycle workers if a solver leaks.
- **Acquirium's Ray.** Acquirium's server runs in its own process and imports
  Ray for its drivers. The orchestrator's `ray.init(address="local",
  namespace="flexorch")` always starts its own instance, never one that is
  already running, so the two never share a Ray. The benchmark ran with the
  Acquirium server up the whole time; the orchestrator's Ray was in namespace
  `flexorch`.

## Choices made while building it

- **Timeouts count from submission**, which includes starting a worker. A
  task past `task_timeout_s` gets `ray.cancel(force=True)` (this works on
  Windows) and is stored as a failed result, `termination: timeout`, so the
  tag can be run again. It isn't retried, because a hung solve would hang
  again. The default is two hours, so a hung solve can't hold a worker
  forever; `task_timeout_s: null` removes the limit.
- **One retry budget.** Ray's `max_retries` is 0. `execution.retries` covers a
  dead worker, a raising build and a raising solve alike, in the main loop.
- **Units are mapped in the main process.** The worker's mapper has no QUDT
  resolver, because the resolver is Acquirium. `finish` maps every series
  again with the orchestrator's mapper, so units match an in-process run.
- **Hooks must be importable by name.** A lambda, a closure, or a function
  defined in the script being run is refused before anything starts. Workers
  can import modules next to the driver script, because Ray puts the driver's
  directory on their path.
- **Fits.** `fit_surrogate` variants run on Ray the same way: data is
  prepared once in the main process and `ray.put` once per selection, and the
  main process stores the fits in order.

## Not done yet

- **2 nodes, and Linux.** Ray doesn't support multi-node clusters on
  Windows, and this machine has no Docker. The per-tenant cluster templates
  (`deploy/ray/`) are written but not run. The exit criterion "the example
  runs against a 2-node per-tenant Ray cluster" waits for a Linux host with
  Docker. Do the same benchmark there.
