# flex-orchestrator

flex-orchestrator runs [flex-pse](https://github.com/flex-pse/flex-pse)
process-model studies and stores everything they need and produce in
[Acquirium](https://github.com/DataDrivenCPS/acquirium): model specs,
operating data, surrogate fits and solved results. A study is a model spec
plus sweeps (surrogates, variables, configuration options, cases). Each solve is stored as a run you can query,
compare, export to disk, and import elsewhere. See [docs/](docs/) for the
configuration and design notes.

## Install

flex-pse isn't on PyPI yet, so use the conda environments, which install it
and the solvers:

```
conda env create -f environment.yml        # deployment: pinned upstream commits
conda env create -f environment-dev.yml    # development: editable sibling checkouts
idaes get-extensions                       # IPOPT, if you need it
```

Parallel execution needs Ray (the `ray` extra): `pip install -e ".[ray]"`.

## Connecting

`Orchestrator()` connects to one Acquirium. The arguments pick the mode:

| Mode | Arguments | Notes |
| --- | --- | --- |
| local | `data_dir=".flexorch"` (the default) | A managed Acquirium with DuckDB. No credentials. |
| Postgres | `db="postgresql://user@host/db"`, `data_dir=...` | The login comes from `FLEXORCH_DB_PASSWORD_FILE`, `FLEXORCH_DB_USER` / `FLEXORCH_DB_PASSWORD`, or libpq (`PGPASSWORD`, `~/.pgpass`). It never comes from code. |
| remote | `acquirium_url="http://acquirium:8000"` | A running Acquirium that holds its own database secret. With no store argument at all, `FLEXORCH_ACQUIRIUM_URL` picks this mode, so a container needs no code to find its Acquirium. |

### A long-lived Acquirium

A managed Acquirium lives as long as the first script that opened its
`data_dir`. One that has to keep running between scripts, for its drivers or
data processing, is started on its own:

```
flex-orch --data-dir .flexorch acquirium start [--config acquirium.toml]
flex-orch --data-dir .flexorch acquirium status
flex-orch --data-dir .flexorch acquirium stop
```

While it runs, every `Orchestrator(data_dir=".flexorch")` attaches to it and
leaves it running. `--config` serves your own `acquirium.toml`, drivers and
all. Set `FLEXORCH_START_ACQUIRIUM=0` to make scripts fail instead of starting
a short-lived one when it isn't running.

### Without Acquirium

To keep studies in a folder on disk, with no server, pass a `FileBackend`:

```python
from flexorchestrator import Orchestrator
from flexorchestrator.backend import FileBackend

with Orchestrator(FileBackend("runs")) as orch:
    ...
```

Everything works the same, Ray and export included. Each result is written
when it's stored, so a crash loses only the task in progress. On the command
line it's `flex-orch --files runs ...`. One process uses a folder at a time
(its threads can share it), and there is no Acquirium query API (`orch.acquirium`) or unit verification.
See [examples/ro_study/store_on_disk.py](examples/ro_study/store_on_disk.py).

## Quick start

Trimmed from [examples/ro_surrogates/example.py](examples/ro_surrogates/example.py),
which defines `operating_data`, `ro_model`, `UNITS`, `KEEP` and the fitted
`linear` surrogate:

```python
from flexorchestrator import Orchestrator

with Orchestrator(data_dir=".flexorch") as orch:
    orch.create_study("ro_surrogates")
    orch.store_data("ro_operation", operating_data(), units=UNITS)
    orch.store_model_spec("base", fo.emit_model(ro_model(), relative_to=HERE), base_dir=HERE)
    orch.store_surrogate_spec("ro_linear", linear.to_surrogate_spec())
    spec = orch.apply_surrogate("base", unit="plant.ro", surrogate="ro_linear")
    orch.store_model_spec("ro_linear", spec, parent="base")
    orch.solve_and_store("linear_surrogate", orch.build("ro_linear"), include=KEEP)
    for result in orch.stored_model_results():
        print(result.tag, result.scalars()["costing.aggregate_operating_cost"])
```

## Studies from a config

A study config, as a dict or a YAML/JSON file, holds the base spec, the fits,
the sweeps and what to store. Run it from Python with
`orch.execute_study(config)`, or from the command line:

```
flex-orch --data-dir .flexorch run study.yaml --study my_study [--dry-run]
flex-orch --data-dir .flexorch export my_study export/my_study
flex-orch --data-dir .flexorch import export/my_study --name copy
```

[examples/ro_study/](examples/ro_study/README.md) has one script per sweep
type. The config's JSON Schema is in [docs/schemas/](docs/schemas/).

## Embedding in an application

A service or app calls the library directly:

- **One study per caller.** Selecting a study makes it the orchestrator's, and
  also returns a handle fixed to it that shares the connection. A script keeps
  using `orch`; code serving several callers at once keeps the handle, so one
  caller's selection never reaches another's:

  ```python
  study = orch.select_study("example_a")   # or create_study / get_or_create_study
  study.user = "alice"                     # recorded with what she stores
  study.store_data("ops", frame)
  ```

- **Spec files as bytes.** `store_model_spec(tag, spec, files={"tariff.csv":
  data})` takes the referenced files' bytes instead of reading them, and never
  reads the disk; the result is the same as from disk, `spec_hash` included.
  `model_spec(tag, with_files=True).files` gives them back.
- **Checking a store.** `flex-orch verify > before.json` reports each study's
  counts, tags and value hashes, and its problems; after a restore or a move,
  `flex-orch verify --against before.json` exits 1 on any difference.
- The study config and stored files have JSON Schemas in
  [docs/schemas/](docs/schemas/), and long commands report progress and can be
  cancelled.

## Running in parallel on Ray

Set `execution.max_workers` (or pass `max_workers=` / `--workers`) above 1, and
the tasks run on Ray. Workers build and solve; the main process stores every
result, so workers never hold database credentials. Ray is local unless you
give a cluster: `execution.ray_address`, `Orchestrator(ray_address=...)`,
`FLEXORCH_RAY_ADDRESS`, or `--ray-address`. Each task has a time limit,
`execution.task_timeout_s`, which is 2 hours by default (`null` means no
limit). See [docs/decisions/0001-ray-execution.md](docs/decisions/0001-ray-execution.md).

## More

- [examples/](examples/README.md): the RO examples and a sample plugin
  ([examples/plugins/ro_tools](examples/plugins/ro_tools/README.md)).
- [benchmarking/](benchmarking/): Acquirium storage timings.
- [CHANGELOG.md](CHANGELOG.md).
