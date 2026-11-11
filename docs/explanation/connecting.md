# Connecting to a store

An {class}`~flexorchestrator.Orchestrator` works on one store. Most of the
time that is an Acquirium, and the orchestrator connects to it through
{func}`~flexorchestrator.connection.connect`. The arguments you pass pick one
of three modes:

| Mode | Arguments | Notes |
| --- | --- | --- |
| local | `data_dir=".flexorch"` (the default) | A managed Acquirium with DuckDB in `data_dir`. No credentials. |
| Postgres | `db="postgresql://user@host/db"`, `data_dir=...` | A managed Acquirium whose timeseries and experiment ledger live in Postgres (TimescaleDB). The graph and artifact files stay in `data_dir`, which must persist. |
| remote | `acquirium_url="http://acquirium:8000"` | An Acquirium that is already running and holds its own database secret. |

```python
from flexorchestrator import Orchestrator

orch = Orchestrator()                                    # local, in .flexorch/
orch = Orchestrator(db="postgresql://flex@db/flex", data_dir="/srv/flexorch")
orch = Orchestrator(acquirium_url="http://acquirium:8000")
```

The command line takes the same choices as options: `--data-dir`, `--db` or
`--acquirium-url`.

## Choosing the store outside code

`FLEXORCH_ACQUIRIUM_URL` picks the remote mode for the whole process. A
container or host application sets it, and code that calls `Orchestrator()`
finds its Acquirium without any changes.

While the variable is set, the store isn't chosen in code. Passing `data_dir`,
`db` or a different `acquirium_url` raises `ValueError`, so a script can't
quietly write to a store other than the one the deployment chose. Passing the
same URL is allowed. Unset the variable to choose the store in code again.

## Database credentials

Credentials never come from code. In Postgres mode the login is taken from,
in order:

1. `FLEXORCH_DB_PASSWORD_FILE`;
2. `FLEXORCH_DB_USER` and `FLEXORCH_DB_PASSWORD`;
3. libpq's own `PGUSER`, `PGPASSWORD` and `~/.pgpass`.

The password is never written to disk or logged. The managed server gets it
only in its process environment, and run metadata records only the redacted
`user@host:port/db`.

## A long-lived Acquirium

A managed Acquirium lives as long as the first process that opened its
`data_dir`. If it has to keep running between scripts, for its drivers or its
data processing, start it on its own:

```
flex-orch --data-dir .flexorch acquirium start [--config acquirium.toml]
flex-orch --data-dir .flexorch acquirium status
flex-orch --data-dir .flexorch acquirium stop
```

While it runs, every `Orchestrator(data_dir=".flexorch")` attaches to it and
leaves it running. `--config` serves your own `acquirium.toml`, drivers and
all. Set `FLEXORCH_START_ACQUIRIUM=0` to make scripts fail, instead of
starting a short-lived server, when it isn't running.

## Without Acquirium

To keep studies in a folder on disk, with no server, pass a
{class}`~flexorchestrator.backend.FileBackend`:

```python
from flexorchestrator import Orchestrator
from flexorchestrator.backend import FileBackend

with Orchestrator(FileBackend("runs")) as orch:
    ...
```

Everything else works the same, including Ray and export. Each result is
written when it's stored, so a crash loses only the task in progress. On the
command line, use `flex-orch --files runs ...`.

The limits:

- one process uses a folder at a time, though its threads can share it;
- there is no Acquirium query API (`orch.acquirium`);
- units aren't verified against Acquirium's unit registry.

For unit tests there is also an in-memory
{class}`~flexorchestrator.backend.MemoryBackend`.
