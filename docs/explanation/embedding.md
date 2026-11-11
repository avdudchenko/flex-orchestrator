# Embedding in an application

A service or app calls the library directly. flex-orchestrator doesn't run a
server or handle logins. The host application serves HTTP, authenticates its
users, and decides who may do what. These are the pieces it builds on.

## One study per caller

Selecting a study makes it the orchestrator's, and also returns a **handle**
fixed to that study. The handle shares the orchestrator's connection and
everything set up on it.

```python
study = orch.select_study("example_a")   # or create_study / get_or_create_study
study.user = "alice"                     # recorded with what she stores
study.store_data("ops", frame)
```

A script can keep using `orch`. Code that serves several callers at once keeps
the handle, so one caller's selection never moves another's. Each handle also
records its own user. Closing a handle, or leaving its `with` block, leaves the
shared connection open; only closing the orchestrator closes it.

## Pointing at the store

A container sets `FLEXORCH_ACQUIRIUM_URL`, and the code calls
`Orchestrator()` with no store arguments. See {doc}`connecting`.

## Files as bytes

`store_model_spec(tag, spec, files={"tariff.csv": data})` takes the
referenced files' bytes, as received from an upload, and never reads the
disk. See {doc}`storage`.

## Long-running work

Commands such as `execute_study` and `fit_surrogate` report progress and can
be cancelled, through a {class}`~flexorchestrator.ext.Progress` object:

```python
from flexorchestrator.ext import Progress

progress = Progress(callback=lambda snap: publish(job_id, snap))
study.execute_study(config, progress=progress)
# from another thread, e.g. a "cancel" request:
progress.cancel()
```

A host application usually runs these in a job queue and passes the
progress snapshots on to its clients.

## Schemas

The study config, and every file a study export holds, have JSON Schemas in
`docs/schemas/` (see {doc}`../reference/schemas`). A host application can
validate requests against them, or generate forms from them.
