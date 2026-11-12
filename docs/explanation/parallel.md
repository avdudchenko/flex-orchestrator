# Running in parallel on Ray

Set `execution.max_workers` above 1 and a study's tasks, or a fit's variants,
run on Ray. You can also pass `max_workers=` in Python or `--workers` on the
command line.

```yaml
execution:
  max_workers: 8
  task_timeout_s: 3600
  retries: 1
  on_error: continue
```

## Who does what

Workers build and solve. The main process stores every result:

1. **prepare**, in the main process: check the tag, store the derived spec, and
   read the spec and its files.
2. **execute**, on a worker: build, solve and export. The worker gets only
   plain data and hook import strings.
3. **finish**, in the main process: map the units and store the result.

Workers never get a model, a connection or a credential, and never touch the
store. The stored results are the same as when the tasks run one after
another in-process. The reasons for this split are in
{doc}`../decisions/0001-ray-execution`.

## Where Ray runs

Ray is local unless you give a cluster. Any of these sets one:

- `execution.ray_address` in the config;
- `Orchestrator(ray_address=...)`;
- `FLEXORCH_RAY_ADDRESS`;
- `--ray-address` on the command line.

A local Ray is started once per process, in the `flexorch` namespace, and
reused. A Ray that the caller has already connected to is used as it is. A
cluster's workers need flex-orchestrator and the hooks' modules installed.

Each worker runs one thread per solver library (`OMP_NUM_THREADS=1` and the
like), so `max_workers` solves don't oversubscribe the CPUs.

## Limits and failures

| Setting | Default | Meaning |
| --- | --- | --- |
| `task_timeout_s` | 7200 (2 h) | A task that runs over is killed and stored as a failed result (`termination: timeout`). It isn't retried. `null` means no limit. The limit isn't enforced in-process. |
| `retries` | 0 | How many times a task is run again when building, exporting or solving raises, or its worker dies. A task whose spec or result can't be stored isn't retried. |
| `on_error` | `continue` | `stop` cancels the tasks still running after one fails. |
| `max_calls` | none | Restart a worker process after this many tasks, to keep memory bounded. |
| `resources` | 1 CPU | What each task reserves on Ray (`num_cpus`, `memory`). |
