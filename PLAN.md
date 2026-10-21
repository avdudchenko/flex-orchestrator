# flex-orchestrator — Development Plan

Goal: one Python tool that connects **flex-pse** (or a user's own model code) with **Acquirium**, which stores studies,
data, specs, model results and units. It should support four ways of working:

1. **Interactive scripts.** Store data, specs and results while you work.
2. **Coming back later.** Reopen a study, query results as polars or pandas, rebuild a stored model and re-solve it.
3. **Config-driven studies.** One JSON/YAML file describes a sweep, and `execute_study` runs all of it, in parallel
   with Ray.
4. **HTTP service.** A FastAPI server lets a UI and other tools submit studies and fetch results as JSON.

**Development is split into two phases:**
- **Phase 1, the core library (M0–M6).** Use cases 1–3 as a Python library, plus a deployable single-tenant stack with
  Postgres backups. There is no HTTP in this phase.
- **Phase 2, HTTP and integrations (M7–M8).** HTTP routes, token auth, and integration with the multi-user control
  plane and UI.

The core library must never import the server code, so Phase 2 is a thin layer added on top.

**Deployment model (D14):** fewer than 100 users, organised into **tenants (groups/projects)**. **Each tenant gets its
own isolated stack**:
- an orchestrator and an Acquirium;
- its own database on a shared Postgres cluster;
- its own data volume, network and Ray.

Users and groups must never be able to reach another tenant's data, configs or compute.

This document is the plan only. No orchestrator code has been written yet.

---

## 1. Starting point (what exists upstream)

| Piece | Where | Status / notes |
|---|---|---|
| Study / experiment ledger | Acquirium PR #78 (`experiment-interface`, `a74b3bc`): `ac.study.define/get/all`, `study.input/output/log(...)`, `.json() .scalar(unit=) .text() .file() .timeseries(unit=, observed=)`, `study.start()/finish()/fail()`, `study.experiments.where(status=, **metadata)` | Not merged yet, so we pin to the commit. Runs are append-only and can't be changed. Works on DuckDB (local) or Timescale/Postgres. |
| Variable declarations | `ExperimentStore.declare` (`Experiments.py:204`) | Within a study, a label's role, kind and metadata (**including unit**) are fixed. Re-declaring it with a different shape raises an error. |
| Study creation | `ExperimentStore.define` (`Experiments.py:193`) | **Silently idempotent by name**: it returns the existing study instead of failing. The orchestrator has to guard against duplicates itself. |
| Deletion | — | **There is no way to delete a study, run or observation.** `Manager.delete_timeseries` (`manager.py:809`) deliberately raises an error. The only reset is `recreate=True`, which wipes the whole database. |
| Graph metadata | `ac.insert_metadata(uri, values)` | Stored **only in Oxigraph**, not in Postgres (see the state table below). So it is *not* used for anything that must survive a Postgres-only restore. |
| Querying | `ac.query()` / `ac.explore()` builder, `ac.attr.*` expressions, `find_entity`, `find_all_data`, `sparql`, `Q.data()/.dataframe()`, `study.experiments.where(...).frame()` | Returns **polars**. |
| Units | QUDT ontology bundled; `client.resolve_unit("gal/min")` (`client.py:801`), `/resolve_conversion`, `/conversion_factors` | Turns free-form unit strings into QUDT units. |
| FlexPSE ↔ Acquirium example | `acquirium/examples/flexpse/record_experiment.py` | Reference pattern (it reads values by hand with `pyo.value` and `pyo.units.convert`). |
| Model spec | flex-pse 0.2 flowsheet spec: `FlowsheetSpec` (schema `0.1.0`), a flat list of `elements` whose `name` is the component's model path; `flexcore.config.io.load_spec` | The default spec format. Older `ModelConfig` documents (schema `0.0.x`) are not used. |
| Build / solve | `flexops.build_model(spec)` (expands arcs itself); `flexcore.solvers.get_solver(model=m, prefer=spec.solver).solve(m)`; `flexops.apply_spec(m, elements)` applies `fix` / `unfix` / `dispatch` / `surrogate` elements to a live model | These are the **defaults only**. Users can supply their own (D1). |
| Surrogate spec | `SurrogateSpec`, and the `surrogate` element (`unit`, `relation`, `surrogate_type`, `data`) | In 0.2 **two types work end to end**: `multilinear` (`MultilinearSurrogate` replaces the relation) and `constant_intensity` (no class; the build fixes the unit's intensity parameter). `quadratic`, `exponential`, `arima` and `neural_network` are reserved stubs that raise `NotImplementedError`. |
| Regressors | `flexparameterize.regression`: `LinearRegressor().fit(X, y, input_units=, output_units=)` gives `multilinear`; `ConstantIntensityRegressor().fit(X, y).to_surrogate_spec(input_units=, output_units=)` gives `constant_intensity`; `get_regressor(SurrogateType)`; `to_fit_result() -> FitResult` (coefficients, r2/rmse, n_samples, data window) | Exist in 0.2. Neither has `predict`. The ARIMA regressor is only on the `arima_integration` branch (upstream item U8). |
| `emit_model(m)` (built model → spec) | `flexops.emit_model` | Exists in 0.2 (was upstream item U1). Not needed by D2, but usable for provenance. |
| Model data → export | only `FlexCosting.report_cost() -> CostReport` | **Missing.** The orchestrator builds its own exporter (D3). |
| Multi-tenancy / auth | — | **Acquirium has neither.** Its client takes only `server_url`, `server_port` and `use_ssl` (`Client/acquirium.py:49`). There is one server process per data store. Tenancy has to be provided **around** Acquirium (D14). |
| Database credentials | `pg_dsn` in `[server]` of `acquirium.toml`, or the `PG_DSN` env var (`Server/config.py:43`, `manager.py:126`). Passed unchanged to `psycopg.connect(dsn)` (`timescale_store.py:67`). | The credentials belong to the **Acquirium server process**. HTTP clients never see them. |

### Where Acquirium keeps its state (this decides what a backup must cover)

| Data | Where it lives with the Postgres backend | Covered by a Postgres backup? |
|---|---|---|
| Timeseries values, stream table, logs | Postgres | ✅ |
| Study, variable, run and observation tables | Postgres (same tables as on DuckDB) | ✅ |
| Experiment metadata in the graph | Oxigraph in `data_dir` | ⚠️ can be rebuilt from SQL (`sync_metadata_graph`, `Experiments.py:129`) |
| Plant models, `insert_metadata`, stream/point descriptions | **Oxigraph only** | ❌ needs a `data_dir` snapshot or `/export_graph` |
| Experiment file uploads | **`<data_dir>/experiment_artifacts`** (`manager.py:159`) | ❌ needs a `data_dir` snapshot |

**A full backup of a tenant means its Postgres database plus a snapshot of its Acquirium `data_dir`, taken
together** (D14).

### How Acquirium handles database credentials (checked in `a74b3bc`)
- **Two backends.**
  - `timeseries_backend = "duckdb"` (the default) is a local file and needs no credentials.
  - `"timescale"` is PostgreSQL with the TimescaleDB extension. It needs a libpq DSN.
- **Only the server connects to Postgres.** `Manager` reads `pg_dsn` from the config, or else the `PG_DSN` env var.
- **An environment variable overrides the config file.** `LoadedConfig.apply_server_env` uses `setdefault`, so a
  `PG_DSN` in the environment wins over `pg_dsn` in the toml.
  - `aq.init()` removes inherited `ACQUIRIUM_*` env vars but **passes `PG_DSN` through** (`runtime.py:198-205`).
  - libpq also reads `PGPASSWORD` and `~/.pgpass`.
- **Privileges needed by the database role** (`timescale_store.py:72-219`):
  - `CREATE EXTENSION IF NOT EXISTS timescaledb`, unless an admin has already created it;
  - `CREATE TABLE`/`VIEW`, `create_hypertable`, `ALTER TABLE`;
  - `DROP ... CASCADE` when `recreate=true`.
- **One server per database/graph pair.** Oxigraph is single-process. A second server on the same database with a
  different `data_dir` would come out inconsistent.
- **Where credentials could leak today:** the compose files hard-code `acquirium/acquirium`, and
  `acquirium.docker.toml` puts the password in the DSN. The store logs only the host (`Storage/__init__.py:42`).
  `server.log` and error messages still need checking (U5).

### How the pseudocode maps to what exists
- `or` is a reserved word in Python, so the examples use `orch`.
- The pseudocode's `ArimaSurrogate` loop has no 0.2 equivalent yet (U8). Until then the same workflow is planned and
  tested with `multilinear` surrogates. The orchestrator does not wrap flex-pse fitters (`LinearRegressor().fit(...)`).
  Users bring any object that
  produces a spec.
- A surrogate goes in as a `surrogate` element naming the unit and the relation. The default relation is
  `"power_electrical_relation"`; the digester's is `"biogas_relation"`, so it must be named.
- `store_model_results(tag)` has no model argument, so the model is passed explicitly (D4).
- `get_data('biogas_volume')` reads one column, but the data was stored as the dataset `'biog_gas_data'`
  (D6 covers lookup by column).
- `Orchestrator(db, username, password)`: in the library, the database credentials come from the environment or a
  secret file, not from arguments typed into scripts (D11). User login is handled by the control plane and tokens in
  Phase 2 (D15).

---

## 2. Key design decisions

### Phase 1: core library

**D1 — Users can bring their own build and solve functions.** The orchestrator manages storage and runs. It never
requires flex-pse to build or solve. Two small protocols in `flex_orchestrator.hooks`:

```python
class BuildFn(Protocol):
    def __call__(self, spec: dict, **overrides) -> Any: ...            # returns a Pyomo model (or anything ExportFn understands)

class SolveFn(Protocol):
    def __call__(self, model: Any, **options) -> SolveOutcome: ...     # SolveOutcome(ok, status, termination, message, wall_time, log)
```

- Optional hooks: `apply_surrogate(spec, unit, surrogate_spec, relation=None) -> spec` (edits the spec) and
  `swap_surrogate(model, unit, surrogate_spec, relation=None)` (edits a live model). `relation=None` means the hook's
  default.
- The defaults live in `flex_orchestrator.flexpse` and target flex-pse 0.2 (**built**):
  - `build` adds each override as a flex-pse `fix` element (`"override:<path>"`), then calls `flexops.build_model`.
    An override path is a variable path from the model root (`"demo.pump.flow_in"`, `"...flow_in[3]"`, `"...[*]"`),
    and its value is a number or `{"value", "units"}`. Price sweeps edit the `costing` element instead (M4).
  - `solve` calls `get_solver` (preferring the spec's `solver`) and maps the termination to `SolveOutcome`.
  - `apply_surrogate` adds a `surrogate` element, replacing one for the same unit and relation;
    `swap_surrogate` applies the same element to a live model with `flexops.apply_spec`.
- Hooks can be passed in three ways:
  1. As callables to `execute_study(cfg, build=my_build, solve=my_solve)` or `Orchestrator(..., build=, solve=)`.
  2. Registered by name with `orch.register_hooks("my_model", build=..., solve=...)`.
  3. As **import strings** in a study config, `build: "mypkg.models:build"`. Ray workers need this form (D13), and so
     does the Phase 2 HTTP API.
- A user who writes their own `ExportFn` can store models that aren't Pyomo at all.

**D2 — Specs are stored as data.**
- A model spec is any JSON-serialisable dict. For flex-pse it is a flowsheet spec (`FlowsheetSpec`, schema `0.1.0`)
  as JSON, validated when flex-pse is installed.
- A surrogate spec is any JSON dict. For flex-pse it is a `SurrogateSpec` dump.
- Both are stored as `.json()` input observations. A surrogate swap is a spec transformation
  (`apply_surrogate`) followed by `build`, so stored specs replay without needing `emit_model`.
- **Built** in `flex_orchestrator.specs` (no pyomo or flex-pse imports). Writing to Acquirium comes with the M1
  backend:
  - `to_json_spec` turns a dict or pydantic model into plain JSON and rejects anything JSON can't hold, including
    NaN and infinity.
  - **Normalisation:** a spec whose format has a validator is stored in its validated form, with defaults written
    out. For flex-pse that means `load_spec(...).model_dump(mode="json")` and `SurrogateSpec...model_dump`. So a dict
    and the equivalent pydantic model store, and hash, the same JSON, and a later change to a flex-pse default can't
    change what an old spec means. Without flex-pse installed, specs are stored as given.
  - `spec_hash` is the sha256 of canonical JSON: sorted keys, no whitespace, and integral floats written as ints, so
    `1` and `1.0` hash the same. The canonical form is **frozen** (a pinned-hash test guards it), because stored hashes
    drive resume. `run_hash(spec_hash, overrides)` identifies one run.
  - `spec_format` is detected from the shape: `flex-pse.flowsheet/<schema_version>`, `flex-pse.surrogate`, or `json`.
    Users can name their own format. Validators are registered per format family, and run only when that library is
    installed.
  - `SpecRecord(kind, tag, spec, spec_format, spec_hash, parent_tag)` is what a store call writes. Its `metadata()`
    gives the D10 run metadata, and `verify()` re-checks the hash when a spec is read back (D17 import). A flowsheet
    can't be stored as a surrogate spec, or the other way round.
  - **Tags** are 1–128 characters from `[A-Za-z0-9_.-]`, starting with a letter or digit, because they become
    Acquirium labels and the D17 export's file names.
  - Rendering templates such as `output_name` belongs to `StudyConfig` (M4), not `specs.py`.

**D3 — The orchestrator's own model data exporter, with units.** `export_model_data(model, keys=None, include=None,
exclude=None, time_sets=None, datetime_index=None)` returns a `ModelData` object: a list of `ExportedSeries(key, kind,
index, values, unit_pyomo, unit_qudt, component, raw_index)`. It works in four steps:

1. **Walk the model.**
   - Start from the requested `keys` (exact component paths, e.g. `"costing.aggregate_electrical_power"`), or match
     `include`/`exclude` glob patterns over component names. The default is every active `Var`, `Expression` and
     mutable `Param`.
   - Use `component_data_objects` with stable `name` paths, relative to the model root.
2. **Classify each component by checking which Set it is indexed by.**
   - **Finding the time set.** In flex-pse, `time_block.time_index` is an ordered integer `pyo.Set` holding `0..N-1`.
     The timestamps live separately in `time_block.datetime_index`, a pandas `DatetimeIndex` (`time_block.py:255-283`).
     Because the time set is just integers, the check **must compare the Set object itself, not its values**: any
     other integer Set of the same length would match by value.
     - The time set is found by locating the model's `TimeBlock`, or given as `time_sets=[...]` together with a
       `datetime_index=` mapping for models that don't come from flex-pse.
     - A component counts as time-indexed only if its `index_set()` **is** a registered time set, or one of the
       factors in `index_set().subsets()` is.
     - Time positions `i` become timestamps through `datetime_index[i]`.
     - A declared subset of time, such as a set of all points but the last, is treated as time only if it was added to
       `time_sets` explicitly. Otherwise it falls under the custom-index rule below.
   - **Classification:**
     - **Indexed by time only** → one **timeseries**, with key = the component path
       (e.g. `plant.digester.biogas_volume`).
     - **Indexed by time × a custom set** (e.g. `(t, unit)`) → one **timeseries per custom index value**, e.g.
       `costing.unit_power[pump1]`. Only the time position is turned into timestamps.
     - **Not indexed** → a **scalar**, with key = the component path.
     - **Indexed by a custom set only** → **one scalar per index value**, keyed by the component path plus the index,
       e.g. `costing.capital_cost[pump1]`, `unit.coeff[1,2]`, `tariff.rate[peak]`.
       - Keys are formatted from Pyomo's own `ComponentData.name`, so they match `model.pprint()`.
       - The parent component and the raw index are stored in the variable metadata
         (`{component: "costing.capital_cost", index: ["pump1"]}`), so `orch.variables.where(component=...)` finds all
         elements of one component.
   - **Limits and edge cases:**
     - Index values that aren't strings or numbers are converted with `str()`. If two of them produce the same key,
       the export raises an error.
     - `max_scalars_per_component` (default 10,000): above that limit it warns and skips the component, unless that
       component was named explicitly in `keys`.
     - A component with no value (stale or uninitialised) is recorded as `None` with a `stale` flag, not left out.
3. **Pull out the units.** `pyo.units.get_units(component)` gives the Pyomo unit. A `UnitMapper` translates it to a
   QUDT key, in this order:
   - a built-in table (`m**3/hr` → `M3-PER-HR`, `kW` → `KiloW`, `USD` → `USD`, …);
   - Acquirium's `resolve_unit`;
   - user overrides (`unit_map={...}`).
   Results are cached for each session. If a unit can't be resolved, strict mode raises an error. Otherwise the value
   is stored with `unit=None` and the raw Pyomo unit is kept in the variable metadata, with a warning.
4. **Declare and record in Acquirium.**
   - timeseries → `study.output(key).timeseries(unit=qudt, pyomo_unit=..., component=..., index=[...])`
   - scalar → `.scalar(unit=qudt, component=..., index=[...])`, one variable per element of a custom-indexed component
   - `.json()` is used only for specs and solve outcomes, never for model values.
   - **Unit consistency:** a label's unit can't change once declared. If a later run produces the same key in a
     different unit with the same dimensions, the values are converted to the unit already declared. If the
     dimensions don't match, it raises an error.

The exporter has no Acquirium dependency (it produces `ModelData`). The writer is a second step, so the exporter can
be tested without a database.

**D4 — Explicit results API.**
- `orch.store_model_results(tag, model, outcome=None, keys=None, include=None, exclude=None)`
- `orch.solve_and_store(tag, model)`
- The orchestrator never relies on an implied "current model".

**D5 — No parquet; everything is stored natively in Acquirium.**
- Every exported value becomes an Acquirium output variable (timeseries or scalar).
- `store_data` input becomes Acquirium timeseries streams.
- Specs become `.json()`.

This keeps everything queryable, tied to units, and covered by the Postgres backup. The cost is more streams per run,
so M2 includes a benchmark of about 1,000 keys × 8,760 steps × 10 runs. If it is too slow, tighten the default key
selection rather than adding a separate file store.

**D6 — Data API.**
- `store_data(name, df, units={col: unit})` accepts a polars or pandas DataFrame. The index (pandas) or a
  `time_column` (polars) must be datetimes.
- It registers the datasource `flexorch/<study>/<name>`, creates one stream per column with its units, and records a
  run in `<study>::data` with a `.json()` manifest.
- `get_data(name, columns=None, start=None, end=None, frame=...)` reads it back.
- `get_data(column=...)` finds a single column across datasets, and raises an error if the name is ambiguous.

**D7 — You choose polars or pandas.**
- Every read accepts `frame="polars" | "pandas"`.
- The default is set per orchestrator, `Orchestrator(..., frame="pandas")`.
- Results are built in polars and converted with `polars.DataFrame.to_pandas()`, with a DatetimeIndex restored.
- Shorthand methods: `.to_pandas()` / `.to_polars()`.

**D8 — Direct query access to Acquirium.**
- `orch.query()` returns the native Acquirium `Query` builder, **scoped to the current study**.
- `orch.query(scope="tenant")` searches the tenant's whole Acquirium. Because of D14, that is still only the tenant's
  own data.
- `orch.attr`; `orch.runs.where(**metadata)` / `orch.variables.where(...)` with `frame=` output.
- `orch.timeseries(ref_uris, start=, end=, frame=)`, and `orch.sparql(q)` (read-only).
- `orch.acquirium`: the raw client, for anything the orchestrator doesn't wrap.
- Isolation comes from the deployment (D14), not from filtering queries. So raw query access is safe to expose:
  there is no other tenant's data in the store to leak.

**D9 — No duplicates, no overwrites.** Acquirium allows duplicates, so the orchestrator enforces its own rules.
- **Studies.**
  - `create_study(name)` checks `ac.study.get(name)` first, and raises `StudyExistsError` if the study exists.
  - `select_study(name)` raises `StudyNotFoundError` if it doesn't exist.
  - `get_or_create_study(name)` is the explicit way to say "either is fine".
- **Tags.**
  - Within a study, every stored item has a unique `(kind, tag)`, saved in run metadata as
    `{flexorch_kind, flexorch_tag}`.
  - Storing an existing tag (status `succeeded` or `running`, not tombstoned) raises `TagExistsError`.
  - A `failed` run doesn't count as existing, so you can retry it.
- **Specs.** A content hash (`spec_hash`, sha256 of canonical JSON) is stored with each spec. `execute_study` uses
  the hash plus the overrides to skip runs that already succeeded (resume).
- **Races.** Check-then-write isn't atomic in Acquirium. Mitigations:
  - one writing process per tenant: the main process in D13, and in Phase 2 the tenant's orchestrator service;
  - an in-process lock for each study;
  - **a check after every write**: if two live runs carry the same tag, the later one is tombstoned as `duplicate`
    and the call fails.

**Deletion (placeholder until Acquirium supports it, upstream item U3):**
- `orch.delete(kind, tag)` / `orch.delete_study(name)` do a **soft delete**. Each deletion is recorded as a run in a
  `<study>::tombstones` ledger study (`target`, `reason`, `deleted_at`, `deleted_by`). This ledger lives in the
  **SQL tables, so it is covered by the Postgres backup**, unlike graph-only `insert_metadata` flags.
- Every read leaves out tombstoned items by default. `include_deleted=True` shows them.
- `store_*(tag, ..., replace=True)` tombstones the old item, then writes the new one. `replace` defaults to `False`,
  so overwriting is never accidental.
- `orch.purge(...)` is defined now but **raises `NotImplementedError`**, with a pointer to U3.
- `deletion.mode = "soft" | "disabled"` lets a deployment turn deletion off completely.

**D10 — How an orchestrator study is laid out in Acquirium.**

| Acquirium Study | One run = | Variables |
|---|---|---|
| `<s>::data` | one `store_data` call | `manifest` (`.json`) plus one `.timeseries(unit=)` per column |
| `<s>::specs` | one `store_model_spec` / `store_surrogate_spec` call | `spec` (`.json`); metadata `{flexorch_kind, flexorch_tag, spec_hash, parent_tag, spec_format}` |
| `<s>::runs` | one model solve | inputs `model_spec_ref` (`.json`), `overrides` (`.json`); outputs = exported model keys (D3), `solve_outcome` (`.json`); log `solver events` |
| `<s>::fits` | one `fit_surrogate` variant (D19) | inputs `selectors`, `resolved_refs`, `prep`, `regressor`, `params` (`.json`); outputs `fit_result`, `diagnostics` (`.json`), optional `fitted`/`actual`/`residual` (`.timeseries`); metadata `{flexorch_tag, data_hash, spec_tag}` |
| `<s>::tombstones` | one deletion / duplicate marker | `target` (`.json`), `reason` |

Every run's metadata also records the orchestrator, flex-pse and Acquirium versions, the user (from the environment
in Phase 1, from the token in Phase 2), and the hook names, so a result can be reproduced.

**D11 — Connection modes and database credentials.** The library connects to exactly one Acquirium, which in
deployment means one tenant. There are three modes:

```python
# 1. local: managed Acquirium + DuckDB in data_dir. No credentials. Laptops, tests, quick work.
orch = Orchestrator()                                         # data_dir=".flexorch"

# 2. postgres: the library starts and owns a local Acquirium backed by Postgres. Developers and single-user servers.
orch = Orchestrator(db="postgresql://db-host:5432/tenant_a",  # no password in the URL
                    data_dir="/srv/flexorch/acquirium")        # graph + artifacts; must persist

# 3. remote: connect to a running Acquirium. This is the tenant stack (D14), where Acquirium owns its DB secret.
orch = Orchestrator(acquirium_url="http://acquirium:8000")
```

- **No username or password in script code.** In mode 2, the database credentials are taken in this order:
  1. `FLEXORCH_DB_PASSWORD_FILE` (a secret file);
  2. `FLEXORCH_DB_USER` / `FLEXORCH_DB_PASSWORD`;
  3. libpq's `PGUSER` / `PGPASSWORD` / `~/.pgpass`.
  An explicit `db_credentials=DBCredentials(user, SecretStr(pw))` argument exists only for developers and tests.
- **Mode 2 mechanics:**
  - build the DSN with `psycopg.conninfo.make_conninfo`;
  - write a toml with `timeseries_backend = "timescale"` and **no `pg_dsn`**;
  - pass the DSN **only through the child process's environment** as `PG_DSN`, then call `aq.init(config=...)`;
  - never write the password to disk, never log it, and never record it in metadata (only a redacted
    `user@host/db`).
- **Checks before starting (mode 2):**
  - authentication, with a clear `DBAuthError`;
  - the timescaledb extension is present;
  - the role can create tables;
  - a lock record in the database refuses a second managed server that uses a different `data_dir`.
- **Never `recreate`.** The orchestrator never sets it, and rejects configs that do.
- **Mode 3** needs no database credentials at all. In Phase 1 the network around the tenant stack is what protects
  Acquirium (D14). Phase 2 adds token auth at the orchestrator (D15).

**D17 — Study export to a folder (and import back).** `orch.export_study(path)` writes a self-contained folder that
**mirrors the study's structure**: specs, data and model results, all as **JSON**. Index structure, units and
provenance are explicit, so the folder can be read without Acquirium or the orchestrator. It also works as a portable
copy of one study for sharing, archiving or moving between tenants, on top of the D14 database backups.

```
example_a/                                  # one folder per study
  study.json                                # manifest: schema version, study name, exported_at, exported_by, tenant,
                                            #   orchestrator/flex-pse/acquirium versions, counts, options used
  data/
    biogas_data.json                        # one file per dataset (D6)
  specs/
    models/base_model.json                  # one file per model spec tag (D2)
    models/model_with_surrogate_biogas_h1.json
    surrogates/biogas_h1.json               # one file per surrogate spec tag
  fits/
    biogas_h1.json                          # one file per fit run (D19): regressor, params, diagnostics, data provenance
  results/
    model_with_surrogate_biogas_h1.json       # one file per result tag (D3/D4)
  tombstones.json                           # only with include_deleted=True
  checksums.sha256                          # sha256 of every file, checked on import
```

*File formats* are versioned (`"schema": "flexorch-export/1"`). Each is defined as a pydantic model, and the JSON
Schemas are published in `docs/schemas/`.
- **Spec file:**
  `{tag, kind, spec_format, spec_hash, parent_tag, created_at, created_by, run_id, versions, spec: {...}}`.
- **Dataset file:**
  `{name, time_index: [ISO-8601 with timezone, ...], columns: {col: {unit, values: [...]}}, manifest}`.
- **Result file:** each value keeps the index structure and units that D3 extracted.

```json
{
  "schema": "flexorch-export/1",
  "tag": "model_with_surrogate_biogas_h1",
  "run_id": "…", "status": "succeeded", "started_at": "…", "finished_at": "…",
  "model_spec_ref": {"tag": "model_with_surrogate_biogas_h1", "spec_hash": "…"},
  "overrides": {"costing.electricity_price": 0.1},
  "hooks": {"build": "flex_orchestrator.flexpse:build", "solve": "flex_orchestrator.flexpse:solve"},
  "solve_outcome": {"ok": true, "status": "ok", "termination": "optimal", "wall_time": 12.3},
  "time_index": {"set": "time_block.time_index", "timestamps": ["2026-01-01T00:00:00+00:00", "…"]},
  "timeseries": {
    "plant.digester.biogas_volume": {"unit": "M3-PER-DAY", "pyomo_unit": "m**3/day",
                                     "component": "plant.digester.biogas_volume", "index": [], "values": [ … ]},
    "costing.unit_power[pump1]":    {"unit": "KiloW", "pyomo_unit": "kW",
                                     "component": "costing.unit_power", "index": ["pump1"], "values": [ … ]}
  },
  "scalars": {
    "costing.total_capital_cost":   {"unit": "USD", "component": "costing.total_capital_cost", "index": [], "value": 1.2e6},
    "costing.capital_cost[pump1]":  {"unit": "USD", "component": "costing.capital_cost", "index": ["pump1"], "value": 3.4e5}
  }
}
```

- Each result stores its timestamps **once** (`time_index`), and every series lines up with them. `component` plus
  `index` rebuild the original Pyomo indexing, and `unit` (QUDT) plus `pyomo_unit` keep both unit systems.
- Floats are written so they read back exactly. Missing or stale values are written as `null`, and stale values are
  listed in `stale: [...]`. Raw indices that aren't strings are kept as JSON arrays of their original types.

*API:*

```python
report = orch.export_study("exports/example_a", parts=("specs", "data", "results"),
                           tags=None, include_deleted=False, compress=False)   # compress → *.json.gz
orch.export_result("model_with_surrogate_biogas_h1", "exports/r1.json")          # a single file
res = flex_orchestrator.archive.load_result("exports/example_a/results/…json")  # no DB needed → ResultHandle
res.to_frame("pandas"); res.units

orch.import_study("exports/example_a", name=None, on_existing="error")          # checks schema + checksums, re-creates
```

- **Export** reads from the active study, streams one file at a time (memory use stays bounded for large studies),
  and writes into a temporary folder that is renamed into place at the end, so a crash never leaves a half-written
  export.
  - It refuses to write into a folder that isn't empty, unless `overwrite=True`.
  - It never includes credentials, DSNs or secrets. A test checks this.
- **Import** checks the schema version and every checksum first. It then re-creates the study through the normal
  store APIs, so the D9 uniqueness rules and the unit declarations apply. Specs come first, then data, then results,
  keeping the original tags, `spec_hash` and run metadata. The original `run_id` and `exported_by` are recorded as
  `imported_from` provenance.
- **Isolation (D14):** export and import run inside one tenant's stack. Moving a study to another tenant is an
  explicit two-step operation by someone with access to both: export from A, then import into B. No route reaches
  across tenants.
- CLI: `flex-orch export <study> <path>` and `flex-orch import <path>`. Phase 2 routes are in §4.

**D18 — Extension API: commands, plugins, subclassing, custom routes.** Users can extend the orchestrator without
forking it. **Commands are the main extension point.** They live in the core library (Phase 1, no FastAPI), and
Phase 2 exposes them over HTTP automatically. Custom FastAPI routes come in Phase 2 and inherit auth and tenant checks
by default.

*Commands* (`flex_orchestrator.ext`):

```python
from pydantic import BaseModel
from flex_orchestrator.ext import command, Progress

class PriceSweepParams(BaseModel):
    prices: list[float]
    base_tag: str

@command(name="price_sweep", role="editor", long_running=True)
def price_sweep(orch: Orchestrator, params: PriceSweepParams, progress: Progress) -> dict:
    ...                                                   # any orchestrator API, may submit Ray tasks
    return {"stored": [...]}
```

- One definition is callable from everywhere:
  - Python: `orch.run_command("price_sweep", prices=[0.1, 0.2], base_tag="base")`;
  - CLI: `flex-orch cmd price_sweep --prices 0.1 0.2 --base-tag base` (options generated from the params model);
  - HTTP (Phase 2): `POST /commands/price_sweep` returns `202 {job_id}`. The body is checked against the params
    model, and the OpenAPI schema is generated from it.
- What commands get for free:
  - checking parameters against the pydantic model;
  - a `Progress` object that reports progress and handles cancellation;
  - recording each call as a run (inputs, outcome, user) in `<tenant>::commands`, so every call has a record;
  - `role` enforcement in Phase 2;
  - `long_running=True` sends the call through the job queue.
- **`execute_study` is itself a built-in command.** There is one execution and job path for built-in and custom
  work, not two.
- Command names are unique. A collision with a built-in or another plugin fails at startup.

*Plugins* bundle extensions:

```python
plugin = Plugin(
    name="digester_tools",
    api_version=">=1,<2",                    # PLUGIN_API_VERSION range it supports
    commands=[price_sweep],
    hooks={"digester_build": "digester_tools.models:build"},   # named D1 hooks
    orchestrator_cls=DigesterOrchestrator,   # optional subclass (below)
    routers=[router],                        # Phase 2 only: fastapi.APIRouter instances
    on_startup=..., on_shutdown=...,
)
```

- **How plugins are found:**
  - the `flex_orchestrator.plugins` entry-point group in the plugin package's `pyproject.toml`
    (`[project.entry-points."flex_orchestrator.plugins"] digester_tools = "digester_tools:plugin"`);
  - or passed directly, `Orchestrator(plugins=[...])` / `create_app(plugins=[...])`.
- **Only listed plugins load.** A plugin loads only if it appears in `FLEXORCH_PLUGINS=digester_tools,...`, or is
  passed explicitly. Being installed isn't enough, so each tenant can enable a different set.
- **Version check:** if a plugin's `api_version` range doesn't include `PLUGIN_API_VERSION`, it fails at startup with
  a clear error.
- `routers` are accepted only when `server/` is loaded, so the core library still never imports FastAPI.

*Subclassing the orchestrator* is supported, with a deliberately small stable surface:
- These methods are stable to override:
  - `build_hooks()` returns the default hooks;
  - `default_export_selection()`;
  - `before_store(item) / after_store(item)`, which can reject an item or add metadata to it;
  - `on_task_result(result)`;
  - `on_study_created(study)`.
- **Lifecycle events** (`study_created`, `spec_stored`, `result_stored`, `job_finished`, `command_finished`) are a
  lighter alternative: `@orch.on("result_stored")` or `Plugin(on_events={...})`.
- Everything not documented as stable is internal (prefixed with `_`, and not part of the semver promise).
- The server builds its orchestrator from `Plugin.orchestrator_cls`, falling back to `Orchestrator`. If two enabled
  plugins provide subclasses, startup fails.

*Custom FastAPI routes (Phase 2):*
- Build apps only through **`create_app(settings, plugins)`**. There is no module-level `app` created at import time.
  `flex-orch serve` and uvicorn use `--factory`.
- Plugin routers are mounted at **`/ext/<plugin>/…`**, with the auth, tenant and audit dependencies applied **to the
  whole router** (`app.include_router(router, prefix=..., dependencies=[Depends(authenticated_tenant)])`). **A plugin
  route can't be unauthenticated by accident.** Core prefixes (`/studies`, `/jobs`, `/commands`, `/query`, `/health`,
  `/ready`, `/ext`) are reserved, and a collision fails at startup.
- **Request dependencies** in `flex_orchestrator.server.deps`, the same ones the core routes use:
  - `get_orchestrator` (the tenant's orchestrator, or the subclass);
  - `current_principal` (user, tenant, role);
  - `require_role("viewer" | "editor" | "owner")`;
  - `get_jobs` (submit or track commands from a custom route);
  - `get_settings`.
- Plugin routes appear in the OpenAPI schema under a tag per plugin, so the generated UI client (D16) picks them up.

*Isolation (D14):*
- Plugins are code **installed in the tenant's image**. They run only inside that tenant's stack and Ray, like user
  hooks.
- Tenants that need custom plugins get an image variant, or a pip-install layer at provisioning. Which plugins are
  enabled is set per tenant through `FLEXORCH_PLUGINS`.
- The isolation suite also runs with a deliberately hostile sample plugin enabled.

*Supporting plugin authors:*
- `flex_orchestrator.testing` provides:
  - `memory_orchestrator()`;
  - `run_command_in_test(...)`;
  - (Phase 2) `make_test_app(plugins=[...])` and `token_for(role, tenant)`.
- A complete sample plugin lives in `examples/plugins/digester_tools/`, with its own pyproject and tests. It runs in
  CI.
- `PLUGIN_API_VERSION` follows semver, separately from the package version, and changes are recorded in the
  changelog.

**D19 — Regression as a command (`fit_surrogate`).** Fitting surrogates from data stored in Acquirium is a
**built-in D18 command**. So it is available in Python (`orch.fit_surrogate(...)` / `orch.run_command("fit_surrogate",
...)`), from the CLI (`flex-orch cmd fit_surrogate`), and over HTTP (`POST /commands/fit_surrogate`, Phase 2) with
**the same JSON parameters**. It is built on flex-pse's generic regression interface,
`flexparameterize.regression`:
- the `Regressor` protocol (`fit(X, y)`, `to_fit_result()`, `to_surrogate_spec(**kw)`);
- `get_regressor(SurrogateType)`, which in 0.2 maps `constant_intensity` and `multilinear` to their classes and raises
  `NotImplementedError` for the reserved types.

So the orchestrator never needs to know which kind of regressor it is running.

**Surrogate coverage follows the pinned flex-pse.** D19 is built and tested against the surrogate types the pinned
flex-pse implements end to end, which in 0.2 means `multilinear` and `constant_intensity`. Nothing in `fitting/` is
specific to a type. A newly implemented type (ARIMA from U8 first, then quadratic, exponential, neural network) is
added when the pin moves: bump the SHA, add the type to the test matrix and the docs, and add any `prep` it needs
(a time-series regressor such as ARIMA needs a regular, gap-free index). A reserved type gives a clear error naming
the installed flex-pse version.

*1. Data selectors* (JSON, so they work over HTTP). X and y each take one selector, or a list of them, which are
merged column-wise. Every selector resolves to a list of Acquirium stream references plus a window:

| Selector | Source |
|---|---|
| `{"dataset": "biogas_data", "columns": [...]}` | a dataset in the active study (D6) |
| `{"study": "example_b", "dataset": "...", "columns": [...]}` | a dataset in **another study of the same tenant** |
| `{"result": "<result tag>", "keys": [...]}` | exported model results (D3), e.g. fitting on simulated data |
| `{"ref_uris": ["urn:…"]}` | specific Acquirium streams in the tenant's general store |
| `{"resolve": "digester biogas volume"}` | Acquirium text resolution (`ac.resolve`). The command fails if the match is ambiguous. |
| `{"sparql": "SELECT ?ref WHERE { … }"}` | a **read-only** SPARQL query that returns stream references (UPDATE is rejected) |

- Shared options: `start`, `end`, `units` (target QUDT units, converted through Acquirium), `rename` (column names).
- Python callers can also pass a DataFrame or an Acquirium `Query` directly. The HTTP API accepts only the JSON forms.
- Selectors reach only the tenant's own Acquirium. Under D14 there is nothing else to reach.

*2. Preparation* (`prep`). The 0.2 regressors drop rows with nulls (and `constant_intensity` also drops zero-flow
rows), but X and y must still share one index, and time-series regressors added later need a regular, gap-free
DatetimeIndex. So the command prepares the data first:
- `align`: `inner` or `outer` join on timestamps; timezones normalised to UTC.
- `resample`: `{freq, agg}`.
- `gaps`: `error` (the default), `interpolate` (with a `limit`), or `longest_segment`.
- `max_rows`: a guard against running out of memory (default 1e6).
- Unit conversion to each selector's `units`.

Every preparation step is recorded with the fit.

*3. Fitting:*
- `regressor` is either a `SurrogateType` name, resolved by the flex-pse hook
  `flex_orchestrator.flexpse:get_regressor`, or an **import string** to any class that follows the `Regressor`
  protocol (user code or a D18 plugin).
- `params` are the constructor arguments (none for the 0.2 regressors; ARIMA's `order` once U8 lands).
- `spec_options` are passed to `to_surrogate_spec`.
- **Column names and units.** The surrogate's variable names are the column names, so they must be the unit's
  variable names (`flow_in_feed`, `biogas_volume`); `rename` on a selector maps them. The flex-pse hook passes each
  column's units to the regressor: to `fit(input_units=, output_units=)` for `multilinear`, and to
  `to_surrogate_spec(input_units=, output_units=)` for `constant_intensity`. Selector units are QUDT, and surrogate
  specs need Pyomo unit strings, so the hook converts them with the D3 unit table in reverse (`M3-PER-HR` to
  `m^3/hr`). A unit with no Pyomo equivalent is an error.
- `variants` holds several parameter sets, and `tag` is a template rendered for each, e.g. `"biogas_{name}"`. A
  variant can override `params`, `regressor`, `X`, `y`, `window` and `prep`, so one call can fit, say, the same
  relation on two seasons of data, or `multilinear` against `constant_intensity`.
  - The data is loaded and prepared **once** in the main process.
  - With `max_workers > 1`, the variants fit in parallel on the tenant's Ray (data shared with `ray.put`, D13).
  - The main process writes all results.
- `validation`: `{"holdout": "7D"}` gives holdout metrics (RMSE, MAE, MAPE). This is used only if the regressor has
  `predict`, which the base protocol doesn't require and neither 0.2 regressor has. For the 0.2 types the flex-pse
  hook computes the holdout predictions itself, from the fitted `multilinear` coefficients (intercept plus the sum of
  coefficient times input) or the `constant_intensity` coefficient (coefficient times input).

*4. What gets stored:*
- **A surrogate spec** for each variant under its tag in `<s>::specs`. The D9 rules and `on_existing` apply.
- **A fit run** in `<s>::fits`, recording:
  - `to_fit_result()` and `fit_diagnostics` (where available), the regressor and its parameters;
  - **the training-data provenance**: the selectors as sent, the stream references they resolved to, the window,
    the preparation steps, the row count, and a sha256 of the prepared data;
  - optional `fitted` / `actual` / `residual` timeseries outputs with units, for plotting.
- The spec's metadata links back to its fit run (`fit_run_id`). D17 exports include `fits/<tag>.json`.

*5. Fitting and sweeping in one `StudyConfig`:* an optional `fits:` section runs before `sweeps`, so a surrogate sweep
can refer to tags fitted in the same run. The original pseudocode workflow (fit orders 1–3, then swap each into the
model and solve) becomes one config, or one `POST /commands/execute_study` request. With 0.2 the surrogates are
`multilinear`; the ARIMA order sweep becomes a `variants` list once U8 lands, with no other change.

```python
orch.fit_surrogate(
    tag="biogas_{name}", regressor="multilinear",
    X={"dataset": "biogas_data", "columns": ["feed_volume"], "rename": {"feed_volume": "flow_in_feed"}},
    y={"resolve": "digester biogas volume", "units": "M3-PER-DAY", "rename": {"*": "biogas_volume"}},
    prep={"resample": {"freq": "1h", "agg": "mean"}, "gaps": "interpolate", "limit": 3},
    variants=[
        {"name": "h1", "window": {"start": "2025-01-01", "end": "2025-06-30"}},
        {"name": "h2", "window": {"start": "2025-07-01", "end": "2025-12-31"}},
    ],
)
```

```http
POST /tenant-a/commands/fit_surrogate            (editor token; body = the same parameters + "study")
→ 202 {"job_id": "…"}
GET  /tenant-a/jobs/{id} → {"status": "succeeded", "result": {"tags": ["biogas_h1","biogas_h2"], "fit_runs": [...], "diagnostics": {...}}}
```

**D12 — Python 3.12**, distribution `flex-orchestrator`, import `flex_orchestrator`, src layout, hatchling, pydantic v2.

**D20 — Code style: the same as flex-pse, enforced from the first commit.** **black** formats and **ruff** lints,
with the same rules and versions as flex-pse (`D:\github\flex-pse\pyproject.toml`, `.pre-commit-config.yaml`). Code
that moves between the two repos then needs no reformatting.

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "D"]   # flex-pse's set, plus "D" (see below)
pydocstyle.convention = "google"

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["D"]
"examples/**" = ["D"]
"docs/**" = ["D"]

[tool.black]
line-length = 88
target-version = ["py312"]                  # flex-pse uses py311; D12 fixes 3.12 here
```

- **One deliberate difference:** flex-pse sets `pydocstyle.convention = "google"` but doesn't select the `D` rules,
  so its docstrings aren't actually checked. Here `D` is selected for `src/`, because §8 relies on ruff to enforce
  docstrings on the public API.
- **Formatter:** black, not `ruff format`, to match flex-pse exactly. Ruff's `I` rule sorts imports, so isort is not
  needed.
- **Versions:** the same pre-commit revs as flex-pse (black `26.5.1`, ruff-pre-commit `v0.15.20`), with matching
  versions pinned in `environment-dev.yml` so the editor, pre-commit and CI agree. They're bumped together with
  flex-pse, in a PR that does nothing but bump them and reformat.
- **Enforcement:**
  - **pre-commit:** black, ruff and import-linter on every commit.
  - **pre-push:** `ruff check . && black --check . && lint-imports && pytest -q`, the same as flex-pse.
  - **CI:** the `lint` job runs the same three commands, and every other job depends on it.
- **Editor:** a committed `.vscode/settings.json` makes black format on save and ruff fix on save, using the
  `flex-orch-dev` interpreter.
- **Deliberately not used:** a type checker such as mypy or pyright, because flex-pse doesn't use one. It can be
  revisited after 0.1.0.

**D13 — Parallel execution: Ray only.** Acquirium already installs Ray (2.59), so the orchestrator supports Ray and
nothing else: no executor abstraction, no second backend.
- `max_workers=1` (the default) runs tasks in an ordinary loop in the same process, for debugging and unit tests.
- `max_workers > 1` goes through Ray.

```python
orch.execute_study(cfg, max_workers=8)                                  # Ray inside this machine / tenant container
orch.execute_study(cfg, ray_address="ray://tenant-a-ray-head:10001",    # the tenant's own Ray cluster (D14)
                   resources={"num_cpus": 1, "memory": 4e9})
```

```yaml
execution: {max_workers: 8, ray_address: null, task_timeout_s: 1800, retries: 1, on_error: continue}
```

- **One task function.**
  - `run_task(TaskSpec) -> TaskResult` is an importable, top-level function wrapped in `ray.remote`.
  - A `TaskSpec` is plain data: the spec, the overrides, the hook import strings, the export selection and the solver
    options.
  - **Pyomo models never cross a process boundary.**
- **The main process writes.** Workers return `TaskResult`s through Ray's object store, and the main process writes
  them with a `ray.wait` loop. This works in every connection mode, keeps D9 in one process, and means **workers
  never hold database or Acquirium credentials**.
- **Timeouts and failures.**
  - `ray.cancel(force=True)` at `task_timeout_s`; `max_retries` for crashed workers.
  - A failure only affects its own task (`experiment.fail`). Resume works through `spec_hash`.
- **Memory and threads.**
  - `max_calls` restarts workers after N tasks, to keep memory bounded.
  - `runtime_env` sets `OMP/MKL/OPENBLAS_NUM_THREADS=1`.
- **Progress and cancellation.** The `ray.wait` loop sends progress callbacks (the CLI progress bar now, Phase 2 job
  status later). Cancelling cancels every outstanding task.
- **Deferred:** reusing built models through a Ray actor pool, if the M5 benchmark shows build time dominates.
- **Isolation (D14): Ray is per tenant, never shared across tenants.** Ray workers run user hook code, so a shared
  cluster would let one tenant's code see another tenant's memory, files and network.
  - The default is local Ray inside the tenant's container, `ray.init(address="local", namespace="flexorch",
    include_dashboard=False)`.
  - Larger tenants can get their own Ray cluster (KubeRay `RayCluster` per tenant) on the tenant network.
- **Coexisting with Acquirium's Ray.** Acquirium runs in its own container in the tenant stack, so the two never
  share a Ray instance. In local mode, the separate namespace and explicit `ray.init` handle it. M5 checks this.

**D14 — Tenancy, isolation and backups (option A: one stack per tenant).**

*Tenant = a group/project.* Users belong to one or more tenants with a role (viewer, editor, owner). A solo user is a
tenant with one member. With fewer than 100 users, that is roughly 10–50 tenant stacks, which is easy to manage.

*Per-tenant stack:*

```
             ┌───────────────── tenant-a network (internal) ─────────────────┐
gateway ───► │ flex-orchestrator (lib / Phase 2 API) ──► acquirium ──► PG db tenant_a (role tenant_a)
 (Phase 2)   │        │ local Ray (or per-tenant Ray cluster)   │                (shared PG cluster)
             │        └── volume: orchestrator scratch          └── volume: acquirium data_dir
             └───────────────────────────────────────────────────────────────┘
```

*Isolation guarantees (every one is tested, §7):*

| Layer | Guarantee | How |
|---|---|---|
| Database | One tenant's role can't connect to, read or write another tenant's database | One database and one role per tenant. `REVOKE ALL ON DATABASE tenant_x FROM PUBLIC`; `GRANT CONNECT` to `tenant_x` only. The tenant role is non-superuser, with no `CREATEDB` or `CREATEROLE`. The timescaledb extension is created by the provisioning admin. |
| DB secret | Only the tenant's Acquirium container holds the tenant's database password | A random password per tenant, generated at provisioning and mounted as a secret file. Users and the orchestrator never see it. Rotated by re-provisioning. |
| Graph / files | Each tenant's Oxigraph, artifacts and configs are on its own volume | One volume per tenant, mounted only into that tenant's containers. |
| Network | No route between tenants. Acquirium is unreachable from outside its tenant | One internal network per tenant (a Docker network, or a k8s namespace with a default-deny `NetworkPolicy`). Only the gateway can reach the orchestrator, and nothing outside can reach Acquirium or the database. The database is reachable only from tenant Acquirium containers, using their own credentials. |
| Compute | User hook code runs only on that tenant's compute | Ray per tenant (D13). Containers run as non-root, read-only root filesystem, no Docker socket (**note:** Acquirium's compose mounts `docker.sock`; the tenant stack must not). |
| Configs / specs / results | Everything lives in the tenant's own Acquirium | The orchestrator keeps no state shared across tenants. |
| Identity (Phase 2) | A request is served only if its token is valid for this tenant | D15: the orchestrator checks the tenant claim and role on every request. |

*Backups (Postgres is the main reason we use it):*
- **Cluster level:** pgBackRest or WAL-G with continuous WAL archiving, which gives point-in-time recovery for every
  tenant database. Backups are encrypted and stored off the host.
- **Tenant level:** the Acquirium `data_dir` volume is snapshotted on the same schedule. For a consistent pair, Oxigraph
  is quiesced or exported (`/export_graph`) at snapshot time, and the WAL position is recorded alongside it.
- **A nightly `pg_dump` for each tenant**, so one tenant can be restored or moved on its own.
- **Restoring one tenant:** restore its database (from PITR or the dump) and its volume to the same point. Start
  Acquirium and run `sync_metadata_graph`. Run the orchestrator's `verify` command (study counts, tag uniqueness,
  sample reads).
- **The restore is rehearsed:** a scripted restore drill into a scratch tenant is an exit criterion (M6) and is run
  every quarter.

*Lifecycle:*
- Provisioning (create the database, role, secret, volume and network, start the containers) and deprovisioning
  (final backup, then drop) are scripted in `deploy/tenant/`. The Phase 2 control plane calls the same scripts.
- Idle tenant stacks can be stopped with their volumes kept, and started again on demand.

### Phase 2: HTTP and integrations

**D15 — HTTP API and auth (no passwords in the orchestrator).**
- **Users, passwords, SSO and memberships live in the control plane**, using an OIDC identity provider (Keycloak,
  possibly in front of Stanford SSO, or a hosted one). The orchestrator stores **no users and no passwords**.
- **Every request carries a JWT.** It comes from the frontend login or from a personal access token for scripts
  (`flex-orch login`, the device-code flow). The tenant orchestrator checks:
  - the signature against the identity provider's public keys (JWKS);
  - `iss`, `aud` and `exp`;
  - a `tenant` claim equal to its own `FLEXORCH_TENANT`;
  - the `role` against the route: viewer for GET, editor for store/execute, owner for delete.
- The gateway routes `/<tenant>/…` to that tenant's stack, and the orchestrator checks the token **again**
  (defence in depth). It never trusts headers set by the gateway alone.
- The library client for a deployed tenant is `Orchestrator(url="https://flex.example.org/tenant-a",
  token=...)`, or the token is read from `FLEXORCH_TOKEN`.
- When the orchestrator runs with no control plane (a single lab server), it falls back to static hashed API tokens in
  `FLEXORCH_API_TOKENS`. There is still no password store.

**D16 — Control plane contract.** The multi-user control plane and UI are **separate projects**. This repo provides
what they need:
- a container image;
- `deploy/tenant/` provisioning and backup scripts;
- an env contract (`FLEXORCH_TENANT`, `FLEXORCH_OIDC_ISSUER`, `FLEXORCH_OIDC_AUDIENCE`, `FLEXORCH_ACQUIRIUM_URL`,
  `FLEXORCH_RAY_ADDRESS`);
- `/health` and `/ready` endpoints;
- an OpenAPI schema, from which a UI client is generated.

---

## 3. Target architecture

```
src/flex_orchestrator/
  __init__.py            # Orchestrator, StudyConfig, export_model_data
  orchestrator.py        # facade: studies, store/get, results, query passthrough
  errors.py              # StudyExistsError, StudyNotFoundError, TagExistsError, UnitError, DBAuthError, ...
  connection.py          # D11 modes: local / postgres / remote; DSN building, preflight, server lock
  settings.py            # pydantic-settings: FLEXORCH_* env, *_FILE secrets, SecretStr, redaction
  frames.py              # polars/pandas conversion (D7)
  hooks.py               # BuildFn/SolveFn/ExportFn protocols, SolveOutcome, import-string resolution, registry
  specs.py               # spec hashing, canonical JSON, template overrides (format-agnostic)
  export/
    walker.py            # component discovery, key selection, identity-based time-set detection (pyomo only)
    units.py             # UnitMapper: pyomo → QUDT table, resolver chain, conversion
    model_data.py        # ModelData / ExportedSeries containers
  backend/
    base.py              # StorageBackend protocol: studies, runs, variables, observations, streams, query, tombstones
    acquirium.py         # D9/D10 mapping onto ac.study / timeseries
    memory.py            # in-memory backend for unit tests (implements the same contract)
  flexpse/               # optional default hooks (import guarded)
    build.py  solve.py  surrogate.py  regression.py   # regression.py: get_regressor hook over flexparameterize.regression
  study/
    config.py            # StudyConfig schema (sweeps, hooks, output_name template, export selection)
    expand.py            # sweep expansion → RunPlan list
    task.py              # TaskSpec/TaskResult + run_task (build → solve → export; top-level, importable)
    runner.py            # expand → dispatch (in-process loop or ray.remote + ray.wait) → write; timeouts, resume, progress
  archive/               # D17 study export / import (named so it doesn't clash with export/, the model-data exporter)
    schema.py            # pydantic models for flexorch-export/1 (study, spec, dataset, result) → JSON Schema
    writer.py            # export_study / export_result: streaming, temp-folder-then-rename, checksums
    reader.py            # load_result (no DB), import_study (checks schema + checksums, re-creates via store APIs)
  fitting/               # D19 fit_surrogate command (core)
    selectors.py         # JSON selectors → ref_uris + window (dataset, study, result, ref_uris, resolve, read-only sparql)
    prep.py              # align, resample, gap policy, unit conversion, max_rows, data hash
    fit.py               # regressor resolution (SurrogateType | import string), variants, validation, store spec + fit run
  ext/                   # D18 (core, no FastAPI)
    commands.py          # @command, CommandSpec, registry, run_command, Progress, command run ledger
    plugins.py           # Plugin, entry-point discovery, FLEXORCH_PLUGINS allowlist, api_version check, collisions
    events.py            # lifecycle events + subscription
  testing/               # D18 helpers for plugin authors (memory_orchestrator, run_command_in_test; Phase 2: make_test_app, token_for)
  ops/
    verify.py            # post-restore / health verification of a tenant store
  cli.py                 # flex-orch run | ls | query | export | import | verify | cmd <name>   (Phase 2 adds: serve, login)
  server/                # Phase 2 only
    app.py               # create_app(settings, plugins) factory — no module-level app
    deps.py              # get_orchestrator, current_principal, require_role, get_jobs, get_settings (public for plugins)
    extensions.py        # mounts plugin routers under /ext/<plugin> with auth dependencies, reserved prefixes, /commands/{name}
    auth.py  jobs.py  routes/
deploy/
  docker/                # image (Phase 1, M6)
  tenant/                # provision / deprovision / backup / restore scripts + compose template (M6)
  ray/                   # per-tenant Ray cluster template (M5)
  gateway/               # Phase 2 reference gateway config (M8)
```

Layering rules, enforced with import-linter:
- **Nothing outside `server/` imports `server/`, FastAPI or JWT libraries.** The core library has no HTTP in it.
  `ext/` holds plugin `routers` without touching them; only `server/extensions.py` imports and mounts them.
- `export.walker` depends only on pyomo.
- `specs`, `study.config` and `hooks` don't import pyomo, Acquirium or flex-pse.
- `flexpse/` is the only package that imports flex-pse.
- `backend/acquirium.py` is the only module that imports Acquirium.

---

## 4. Public API (target)

Phase 1, the library:

```python
from flex_orchestrator import Orchestrator, export_model_data

orch = Orchestrator(acquirium_url="http://acquirium:8000", frame="pandas")   # inside a tenant stack
# or Orchestrator() for local DuckDB, or Orchestrator(db="postgresql://host/tenant_a", data_dir=...) for dev Postgres
orch.create_study("example_a")            # raises StudyExistsError if present
orch.select_study("example_a")            # raises StudyNotFoundError if absent

orch.store_data("biogas_data", df, units={"biogas_volume": "m3/day", "flow_in_feed": "m3/day"})
X = orch.get_data("biogas_data", columns=["flow_in_feed"], frame="pandas")

orch.store_model_spec("base_model", spec_dict)                         # TagExistsError on repeat
orch.store_surrogate_spec("biogas_h1", reg.to_surrogate_spec())        # a spec fitted elsewhere, or:
orch.fit_surrogate(tag="biogas_{name}", regressor="multilinear",       # D19: fit from Acquirium data
                   X={"dataset": "biogas_data", "columns": ["flow_in_feed"]},
                   y={"dataset": "biogas_data", "columns": ["biogas_volume"]},
                   variants=[{"name": "h1", "window": H1}, {"name": "h2", "window": H2}])
spec = orch.apply_surrogate("base_model", unit="plant.digester", surrogate="biogas_h1",
                            relation="biogas_relation")
orch.store_model_spec("model_with_surrogate_biogas_h1", spec, parent="base_model")

m = my_build(spec); outcome = my_solve(m)                              # user's own build/solve
orch.store_model_results("model_with_surrogate_biogas_h1", m, outcome, include=["costing.*", "plant.digester.*"])

for r in orch.stored_model_results():                                  # lazy ResultHandles
    df = r.to_frame("polars"); r.tag; r.metadata; r.units

orch.query().where(orch.attr.unit == "KiloW").dataframe()              # native Acquirium query, study-scoped
orch.runs.where(flexorch_kind="result", status="succeeded").frame()
orch.delete("result", "model_with_surrogate_biogas_h2")                  # soft delete (tombstone)
orch.export_study("exports/example_a")                                 # D17: folder of JSON specs/data/results
orch.import_study("exports/example_a", name="example_a_copy")
orch.execute_study(cfg, build=my_build, solve=my_solve, max_workers=8) # Ray (a built-in command)

orch = Orchestrator(plugins=["digester_tools"])                        # D18: plugins, commands
orch.run_command("price_sweep", prices=[0.1, 0.2], base_tag="base_model")
```

Study config:

```yaml
base_model_spec: base_model
fits:                                    # D19: optional, runs before sweeps
  - tag: "biogas_{name}"
    regressor: multilinear               # 0.2 types: multilinear, constant_intensity (arima after U8)
    X: {dataset: biogas_data, columns: [flow_in_feed]}
    y: {dataset: biogas_data, columns: [biogas_volume]}
    prep: {resample: {freq: 1h, agg: mean}, gaps: interpolate, limit: 3}
    variants:
      - {name: h1, window: {start: 2025-01-01, end: 2025-06-30}}
      - {name: h2, window: {start: 2025-07-01, end: 2025-12-31}}
hooks:
  build: "mypkg.models:build"
  solve: "mypkg.models:solve"
  apply_surrogate: "flex_orchestrator.flexpse:apply_surrogate"
sweeps:
  - type: surrogate
    unit: plant.digester
    relation: biogas_relation
    surrogates: [biogas_h1, biogas_h2]
  - type: parameter
    path: costing.electricity_price
    values: [0.1, 0.2, 0.3]
export: {include: ["costing.*", "plant.digester.*"], strict_units: true}
output_name: "model_with_surrogate_{surrogate}_price_{costing.electricity_price}"
on_existing: error                       # error | skip (resume) | replace (tombstone + rewrite)
execution: {max_workers: 8, task_timeout_s: 1800, retries: 1, on_error: continue}
```

Phase 2, the HTTP API (every route needs a valid tenant JWT, D15):

| Method | Path | Min role | Purpose |
|---|---|---|---|
| GET/POST | `/studies`, `/studies/{s}` | viewer / editor | list / create (409 if it exists) |
| POST | `/studies/{s}/execute` | editor | submit StudyConfig → `202 {job_id}` |
| GET | `/jobs/{id}` · DELETE `/jobs/{id}` | viewer · editor | status, progress, errors · cancel |
| GET | `/studies/{s}/results` | viewer | all model results as JSON: `{tag: {metadata, units, index, columns: {key: [...]}}}` (`?tags=`, `?keys=`) |
| GET/POST | `/studies/{s}/specs/{kind}/{tag}` | viewer / editor | read / store specs (409 on duplicate) |
| POST | `/studies/{s}/data/{name}` · GET | editor · viewer | upload / read datasets |
| DELETE | `/studies/{s}/{kind}/{tag}` | owner | soft delete |
| GET | `/studies/{s}/export` · `/studies/{s}/results/{tag}/export` | viewer | the D17 export as a zip download · one result JSON |
| POST | `/studies/import` | editor | upload a D17 export zip → `202 {job_id}` |
| POST | `/query` | viewer | read-only Acquirium query (tenant-scoped by deployment) |
| POST | `/studies/{s}/surrogates/fit` | editor | alias of `POST /commands/fit_surrogate` for that study (D19); the same code path |
| GET | `/commands` | viewer | list available commands (built-in + enabled plugins) and their parameter schemas |
| POST | `/commands/{name}` | the command's `role` | run a command (D18): long-running → `202 {job_id}`, otherwise the result |
| * | `/ext/<plugin>/…` | set by the plugin (auth always applies) | plugin routers (D18) |
| GET | `/health`, `/ready` | none | liveness, plus Acquirium connectivity |

---

## 5. Milestones

Each milestone ends when its exit criterion is met, its **pytest suite passes in CI** (§7), and an example script
under `examples/` runs from start to finish.

### Phase 1: core library

#### M0 — Repo scaffold, environments, test harness (≈ 1 wk)
- `pyproject.toml` (hatchling; extras `flexpse`, `pandas`, `dev`; the `server` extra is added in M7), src layout,
  README, CHANGELOG.
- `environment-dev.yml` / `environment.yml` (**drafted**, see §6).
- pytest setup (§7): tier markers, shared fixtures, coverage configuration.
- Code style per D20:
  - `[tool.black]` / `[tool.ruff]` in `pyproject.toml`;
  - `.pre-commit-config.yaml` running black, ruff and import-linter, plus the pre-push suite (import-linter includes
    the rule that nothing imports `server/`);
  - `.vscode/settings.json`;
  - `CONTRIBUTING.md` with the "run it before submitting" rule from flex-pse.
- GitHub Actions: `lint` (`ruff check .`, `black --check .`, `lint-imports`) → unit (3.11–3.13, ubuntu + windows) →
  component, plus `docs.yml`.
- The docs skeleton (§8): `conf.py`, `index.rst` with the flex-pse toctree captions, `_ext/flexorchdoc.py` (an empty
  shell), the `installation` and `architecture_and_layering` pages, and deployment to GitHub Pages.
- **Exit:** the env builds, `pytest` collects and passes a smoke test, the docs build with `-W`, and CI passes.
  `pre-commit run --all-files` is clean, and a deliberately misformatted file fails both the hook and the CI `lint`
  job.

#### M1 — Storage backend, connections, uniqueness, tombstones (≈ 2 wks)
- The `StorageBackend` protocol, plus the memory and Acquirium backends (D10).
- `connection.py` / `settings.py` (D11):
  - the three modes, with credentials from env or secret file and `SecretStr`;
  - checks before starting, and the server lock;
  - rejecting `recreate`;
  - redaction.
- Study create, select and list with the D9 guards. Tagged stores for specs and data (`units=`, polars or pandas
  input).
- The tombstone ledger, deleted items hidden from reads, `replace=True`, and `purge` → `NotImplementedError`.
- `frames.py`.
- **Exit:**
  - Script 1 up to `store_model_spec` works in modes 1, 2 and 3.
  - A wrong password gives `DBAuthError`, and the password never appears in logs or metadata.
  - The contract, duplicate and tombstone tests pass.

#### M2 — Model data exporter, units, results (≈ 3 wks)
- `export.walker`: key and pattern selection, identity-based time-set detection, product sets split into time and
  custom factors, and `name[idx]` scalars.
- `export.units`: the mapping table, resolver fallback, overrides, strict mode, and converting to the declared unit.
- The writer (D3 step 4). `store_model_results`, `stored_model_results`, and `ResultHandle.to_frame/to_pandas/to_polars`.
- Default flex-pse hooks and `SolveOutcome`.
- The D5 benchmark.
- **Exit:** scripts 1 and 2 run with the flex-pse hooks and with a user-written build/solve pair. Units round-trip.

#### M3 — Query surface and study export/import (≈ 2 wks)
- `orch.query()` (study or tenant scope), `orch.attr`, `orch.runs` / `orch.variables`, `orch.timeseries`,
  `orch.sparql` (read-only), and `orch.acquirium`.
- `archive/` (D17):
  - the `flexorch-export/1` pydantic models, with their JSON Schemas published to `docs/schemas/`;
  - `export_study` / `export_result` (streaming, temp folder then rename, checksums, optional gzip);
  - `load_result`, and `import_study`;
  - CLI `export` / `import`.
- **Exit:**
  - The query examples run, and each query returns either frame type.
  - Script 1's study exports to a folder that matches the D17 layout and validates against the published schemas.
  - Importing it under a new name and exporting again gives files identical to the first export, apart from the
    provenance fields (run ids and timestamps).
  - `load_result` rebuilds the same frames and units with no database.

#### M4 — Declarative studies (`execute_study`), commands/plugins, regression (≈ 4 wks)
- `StudyConfig` plus its JSON schema, hook import strings, sweeps (`surrogate`, `parameter`, `spec_list`; product
  or zip), `output_name`, `on_existing`, and `export`.
- `--dry-run`, `study/task.py`, and the in-process loop (`max_workers=1`) with resume.
- CLI: `flex-orch run study.yaml [--workers N] [--ray-address ...]`.
- **Extension API, core part (D18):**
  - `ext/`: `@command`, the registry, `run_command`, `Progress`, and the command run ledger;
  - `Plugin`, entry-point discovery, the `FLEXORCH_PLUGINS` allowlist, the `api_version` check and collision checks;
  - lifecycle events, and the stable methods for subclasses;
  - `execute_study` re-implemented as a built-in command;
  - `flex-orch cmd <name>`, with options generated from the params model;
  - the `flex_orchestrator.testing` helpers;
  - the sample plugin `examples/plugins/digester_tools/`.
- **Regression command (D19):**
  - `fitting/`: the selectors (all six kinds), preparation, regressor resolution (flex-pse `SurrogateType` or an
    import string), `variants`, optional holdout validation;
  - the surrogate types of the pinned flex-pse only: `multilinear` and `constant_intensity` for 0.2;
  - storing the spec plus a fit run in `<s>::fits` with provenance and the data hash;
  - `fits:` in `StudyConfig`;
  - the `flexpse/regression.py` hook;
  - D17 export and import of `fits/`.
- **Exit:**
  - The pseudocode's fit-then-sweep workflow runs as one `StudyConfig`, with 0.2's `multilinear` surrogates. It
    fits two variants from a stored dataset, swaps each into the digester's `biogas_relation` and solves. A
    `constant_intensity` fit of a pump runs in the same config.
  - ARIMA is **not** an M4 exit criterion. It's a follow-up when U8 lands (see the upstream track).
  - Running it again with the same data gives the same `data_hash` and identical specs.
  - Script 3 runs in-process. `on_existing: skip` does nothing new on a rerun, and `on_existing: error` raises.
  - The sample plugin, installed with pip, loads only when it is in the allowlist. Its command gives the same result
    from Python and from the CLI.
  - An orchestrator subclass provided by the plugin overrides `before_store` and `default_export_selection`.

#### M5 — Ray parallel execution (≈ 1–1.5 wks)
- Ray dispatch in `runner.py`:
  - `ray.remote(run_task)`, with `num_cpus`, `memory`, `max_retries` and `max_calls`;
  - a `ray.wait` loop that enforces timeouts (`ray.cancel(force=True)`), writes results and sends progress events;
  - cancellation;
  - thread pinning through `runtime_env`;
  - `namespace="flexorch"`;
  - parallel fitting of `fit_surrogate` variants (D19), with the prepared data shared through `ray.put`.
- `deploy/ray/`: a **per-tenant** Ray cluster template.
- **Benchmark:** 30 tasks (3 surrogates × 10 prices) on one machine and on 2 nodes, on Windows and Linux. Record the
  overhead per task, build versus solve time, write rate, memory per worker, and whether Ray coexists with
  Acquirium's Ray. Write it up in `docs/decisions/0001-ray-execution.md`.
- **Exit:**
  - The same stored results with `max_workers=1` and `max_workers=8`.
  - A hung task is killed at its timeout, and the sweep continues.
  - At least 5× faster than in-process with 8 workers.
  - The example runs against a 2-node per-tenant Ray cluster.

#### M6 — Single-tenant deployment and backups (≈ 2 wks)
- **Image** (`deploy/docker/`): micromamba plus `environment.yml` plus `idaes get-extensions`. Runs as non-root.
  It is used for the orchestrator and the Ray nodes.
- **Tenant template** (`deploy/tenant/`): a compose template for one tenant stack (orchestrator container, Acquirium
  container, per-tenant network and volumes) pointing at a shared Postgres cluster. There is no `docker.sock`
  mount, and secrets come from secret files. A shared `compose.cluster.yaml` for the Postgres cluster plus its backup
  agent.
- **Provisioning scripts:** `provision_tenant.sh <name>` and `deprovision_tenant.sh <name>`:
  - create the database and role, `REVOKE ... FROM PUBLIC`, and create the extension as admin;
  - generate a random secret;
  - create the volume and network;
  - start the stack.
  Deprovisioning takes a final backup before dropping anything.
- **Backups:**
  - pgBackRest (or WAL-G) for the cluster, with WAL archiving;
  - a nightly `pg_dump` per tenant;
  - per-tenant volume snapshots, with the Oxigraph export taken at the same point;
  - `restore_tenant.sh <name> <timestamp>`, and `flex-orch verify`.
- **Exit:**
  - Two tenants provisioned on one cluster pass the **isolation suite** (§7): database, network, volume and secret.
  - **Restore drill:** tenant A is deleted, then restored to a scratch tenant at a chosen point in time, and
    `verify` passes with the same studies, tags and sample values.
  - Scripts 1–3 run inside a tenant stack, in mode 3.
  - `0.1.0` (the library) is released.

### Phase 2: HTTP and integrations

#### M7 — HTTP API, auth, and extensible app (≈ 2.5–3 wks)
- `server/`: a FastAPI app factory and the §4 routes:
  - a job queue (one runner per tenant; it runs `execute_study` on the tenant's Ray);
  - results JSON with pagination;
  - a read-only `/query`;
  - 409 on duplicates, 202 plus a job id for anything long.
- `auth.py` (D15): JWT checks against JWKS, the tenant claim and role per route; the static-token fallback.
  `Orchestrator(url=, token=)` client mode, and `flex-orch serve` / `flex-orch login`.
- **Extension API, HTTP part (D18):**
  - the `create_app(settings, plugins)` factory (uvicorn `--factory`, no module-level app);
  - the public `server/deps.py`;
  - `server/extensions.py`, which mounts plugin routers under `/ext/<plugin>` with auth, tenant and audit dependencies
    on the router, reserves the core prefixes, checks for collisions and adds an OpenAPI tag per plugin;
  - `GET /commands` and `POST /commands/{name}`, on the same job path as `/execute`;
  - `make_test_app` and `token_for` in `flex_orchestrator.testing`;
  - the sample plugin gains a router.
- **Exit:**
  - Script 4 runs against one tenant stack with a test identity provider (a Keycloak container).
  - A token for tenant B is refused by tenant A (403).
  - A viewer token can't execute, and an editor token can't delete.
  - `POST /commands/fit_surrogate` and the `/studies/{s}/surrogates/fit` alias store **the same** specs and fit
    runs as the Python call with the same parameters.
  - The sample plugin's route and command work over HTTP with an editor token. With no token, or a token for the
    wrong tenant, they return 401/403.
  - A plugin that claims a reserved prefix fails at startup.

#### M8 — Multi-tenant integration (≈ 1.5–2 wks)
- The control plane contract (D16): the env contract, health and ready endpoints, OpenAPI export, and a generated
  TypeScript/Python client for the UI. A reference gateway config (Traefik or Envoy) routing `/<tenant>/` and
  enforcing TLS.
- Progress streaming for the UI (Server-Sent Events on `/jobs/{id}/events`). CORS limited to the UI's origin.
- Starting and stopping idle tenants on demand through the provisioning scripts.
- **Exit:**
  - The full isolation suite, including the auth layer, passes against 3 tenants behind the gateway.
  - A UI-like client (a generated SDK test) runs scripts 3 and 4.
  - The docs cover how an operator adds a tenant, a user and a backup.

#### Upstream track (runs alongside both phases)
- ~~**U1 (flex-pse):** `emit_model(m)`~~ — done in flex-pse 0.2.
- ~~**U2 (flex-pse):** surrogates per relation~~ — done in 0.2 (`surrogate` elements name their relation).
- **U8 (flex-pse):** upstream the ARIMA regressor from `arima_integration` (`ArimaRegressor`, `get_regressor("arima")`,
  `to_surrogate_spec(history_length=...)`) and the `ArimaSurrogate` build onto 0.2's flowsheet spec. **Not blocking**:
  D19 ships with the 0.2 types. When a flex-pse release has it, a follow-up bumps the pin and adds ARIMA (with the
  gap-free `prep` check and ARMA fixture data) to the D19 test matrix and docs. The same goes for quadratic,
  exponential and neural-network surrogates as they get implemented.
- **U3 (Acquirium):** hard delete of studies and runs, cascading to observations, artifacts and streams; a
  `superseded` status for runs; optionally a unique-name flag on `define`. When this lands, `purge` gets implemented.
- **U4 (Acquirium):** an optional auth hook / API-key middleware, as defence in depth inside the tenant network.
- **U5 (Acquirium):** support `PG_DSN_FILE`; make sure the DSN is never logged; remove hard-coded credentials from
  the example configs.
- **U6 (Acquirium):** a "no DDL" startup mode, so the runtime role only needs read/write access.
- **U7 (Acquirium):** a consistent online backup for Oxigraph (`Store.backup()`) and the artifacts directory, e.g. a
  `POST /admin/backup` endpoint, or storing artifacts in Postgres or S3.
- Keep track of the PR #78 merge and release, then move the pin from a git SHA to a PyPI version.

Rough total: **Phase 1 ≈ 15–17 weeks; Phase 2 ≈ 4–5 weeks** (one developer).

---

## 6. Environments

| File | Env name | Purpose |
|---|---|---|
| `environment-dev.yml` | `flex-orch-dev` | flex-pse **v0.2.0** from git (upstream `flex-pse/flex-pse`, commit `b3cc361`), an editable install of the sibling checkout `../acquirium` (the PR #78 branch), this package (`-e .[dev]`), plus test and lint tooling. **Created and verified**: everything imports and `pip check` is clean. To co-develop flex-pse, swap the git line for `-e ../flex-pse[...]` locally. |
| `environment.yml` | `flex-orch` | Deployment. flex-pse and Acquirium are pinned to **exact git SHAs**. Used by the M6 image. |

```bash
conda env create -f environment-dev.yml      # or: conda env update -f environment-dev.yml --prune
conda activate flex-orch-dev
idaes get-extensions                         # IPOPT + IDAES binaries
pip install -e .[dev]                        # once M0 adds pyproject.toml
```

Database credentials for local development (mode 2):
- Commit `.env.example` with `FLEXORCH_DB_URL` / `FLEXORCH_DB_USER` / `FLEXORCH_DB_PASSWORD` left blank. `.env` is
  git-ignored. Alternatively, use `~/.pgpass`.
- `compose.dev.yaml` starts a local Timescale with a password generated into `.env` by `make dev-db`. There are no
  hard-coded defaults.
- No secrets go into the conda env files or the `pyproject.toml`.

Rules:
- Change dependency SHAs in the same PR that makes the code use the new version. Commit a `conda-lock.yml` in M6.
- Binary solvers come from conda-forge or `idaes get-extensions`, never from pip.
- CI rebuilds the deployment env every week to catch upstream drift.

---

## 7. Testing (pytest, for everything)

Every module ships with tests in the same PR. Coverage is enforced in CI: an 85% floor at M1, raised to 90% from M4
on. Tests live next to the code they test, in a `tests/` folder inside each package
(`src/flex_orchestrator/tests/`, `src/flex_orchestrator/flexpse/tests/`, ...), so a module's tests are easy to
find and review. This differs from flex-pse on purpose.

**Tiers** (each test carries exactly one marker, enforced in `conftest.py` as in flex-pse):
- `unit`: no database, no solver. Uses the memory backend, fake hooks, and Pyomo models that are built but never
  solved. Should run in seconds.
- `component`: an embedded Acquirium in `tmp_path` plus HiGHS/IPOPT on tiny models, a local Ray (`num_cpus=2`), and
  a Timescale container for the mode 2 tests. Marked `needs_highs`, `needs_ipopt`, `needs_docker` and skipped when
  those aren't available.
- `integration`: tenant stacks from `deploy/tenant/` on a shared Postgres cluster, the example scripts, a multi-node
  Ray run, the **isolation suite**, and the **restore drill**. Phase 2 adds the server, gateway and identity
  provider.
- `benchmark` (opt-in): the D5 and M5 load tests.

**Shared fixtures (the repo-root `conftest.py`, plus a `conftest.py` in a package's `tests/` for fixtures only it uses)**
- `memory_backend`, `acquirium_local` (session-scoped, with a fresh study per test), and `orch` (parametrised over
  both backends).
- `toy_pyomo_model`, covering:
  - a time-only component;
  - `(t, unit)` and `(unit, t)` product-set components;
  - a component indexed only by a custom set (string and tuple indices);
  - a **decoy `Set(range(N))` the same length as time**;
  - an unregistered subset of time;
  - one Var without units.
- `flexpse_digester_model`: built from flex-pse's `api_freeze` fixtures; skipped if flex-pse isn't installed.
- `fake_hooks`, and `frame_kind` (`polars` or `pandas`).
- `surrogate_case`: parametrised over the surrogate types the **pinned flex-pse implements** (0.2: `multilinear` and
  `constant_intensity`). Each case carries a spec, the unit and relation it targets, the regressor that fits it,
  synthetic data with known coefficients, and the expected solved value.

**Surrogate coverage rule.** Tests use only surrogate types that work in the pinned flex-pse. A tripwire test asserts
that each reserved type (`quadratic`, `exponential`, `arima`, `neural_network` in 0.2) still raises
`NotImplementedError`. When a pin bump implements one, the tripwire fails, and that PR moves the type into
`surrogate_case` (with a regressor and data) and into the docs.
- `two_tenants` (integration): provisions tenants A and B with `deploy/tenant/`, and tears them down afterwards.

**What each area tests**

| Area | Tests |
|---|---|
| Backend contract | One suite run against **both** backends: create, get and list studies; declare and record; latest-tag lookup; metadata filters; tombstone visibility; `include_deleted`. |
| Uniqueness (D9) | `create_study` on an existing study raises an error; `select_study` on a missing study raises an error; an existing tag → `TagExistsError`; a failed run can be retried; `replace=True` tombstones and rewrites; the post-write check catches a simulated race; `on_existing` modes. |
| Tombstones | a tombstoned item is hidden; `include_deleted` shows it; the tag can be reused; tombstones survive a Postgres-only restore; `purge` raises `NotImplementedError`; `deletion.mode=disabled` blocks deletion. |
| Exporter (D3) | key and glob selection; time detection compares the **Set object itself** (a lookalike `Set(range(N))` is not time); time found among the factors of `(t, unit)` and `(unit, t)`; an unregistered subset of time is treated as custom; `datetime_index` mapping; time × custom → a series per value; custom-only → a scalar per element with `name[idx]` keys and `component`/`index` metadata; key collisions raise an error; `max_scalars_per_component`; stale values flagged; inactive components skipped. |
| Units | the table is checked against `pyo.units`; resolver fallback (mocked in unit tests, real QUDT in component tests); strict versus lenient; converting to the declared unit; a dimension mismatch raises an error. |
| Round-trip | export → store → read back: values and units are equal, for both frame kinds. |
| Connection / credentials (D11) | mode selection and conflicting arguments; DSN escaping (`@:/%` in passwords); precedence between `*_FILE`, `FLEXORCH_*`, `PG*` and `.pgpass`; the DSN reaches the child process only through the environment, and the toml contains no `pg_dsn`; a **redaction test** that searches logs, exceptions, `server.log` and metadata for the password; a wrong password → `DBAuthError`; preflight failures; a second managed server is refused; `recreate` is rejected. |
| Study export/import (D17) | the folder layout matches the study (one file per dataset, spec and result tag); every file validates against the published JSON Schema; result files keep `component`/`index`/`unit`/`pyomo_unit` for time-only, time × custom, scalar and custom-indexed values from `toy_pyomo_model`; timestamps keep their timezone, and floats read back exactly; stale values and NaN → `null` plus the `stale` list; non-string raw indices round-trip; **export → import → export gives identical files** (apart from provenance fields); `load_result` frames equal the frames read from the database; checksum tampering and an unknown schema version are both rejected on import; import into an existing study → `StudyExistsError`, and an existing tag → `TagExistsError`; tombstones are only exported with `include_deleted`; a crash part-way through leaves no partial folder; refuses a non-empty target unless `overwrite`; **no secrets or DSNs in any exported file** (scanned); gzip round-trip; memory stays bounded on a large study (benchmark tier). |
| Extension API (D18) | **Core:** `@command` parameter checking; Python and CLI calls give the same result; each call is recorded in the command ledger; `Progress` and cancellation; a long-running command goes through the job path, and Ray dispatch works from a command; `execute_study` behaves the same as a built-in command; name collisions fail at startup; entry-point discovery; **unlisted plugins don't load**; `api_version` mismatch → a clear error; two subclasses → a startup error; subclass overrides (`before_store` rejecting an item, `default_export_selection`); lifecycle events fire in order; the sample plugin's own test suite passes using `flex_orchestrator.testing`. **Server (Phase 2):** every route under `/ext/<plugin>` returns 401 with no token and 403 with another tenant's token, **checked by walking every route the app registers**, so a new plugin route can't escape the check; the role in `@command(role=...)` is enforced on `/commands/{name}`; reserved prefixes are protected; plugin OpenAPI tags appear in the generated client; `create_app` is pure (two apps with different plugins don't affect each other); the isolation suite passes with a hostile plugin enabled. |
| Regression (D19) | **Selectors:** each of the six kinds resolves to the expected stream references and window; a dataset in another study of the same tenant; `result` selectors on exported model keys; an ambiguous `resolve` fails; SPARQL that writes is rejected; unit conversion to `units`. **Preparation:** inner and outer alignment, timezone normalisation, resampling, `gaps=error` refuses gappy data, `interpolate` with `limit`, `longest_segment`, the `max_rows` guard. **Fitting:** `multilinear` and `constant_intensity` through `get_regressor` (the 0.2 types; the matrix grows with the pin); a reserved type gives a clear error naming the flex-pse version; QUDT to Pyomo conversion of column units, and an unmappable unit is an error; the hook's holdout metrics match a hand computation; a custom import-string regressor following the protocol; a regressor that doesn't follow the protocol → a clear error; tag templates over `variants`; `TagExistsError` / `on_existing`; holdout validation only when `predict` exists. **Provenance:** the data hash is stable across re-runs and changes when the data changes; resolved references are recorded; the spec links to its fit run; `fits/` round-trips through the D17 export. **Integration:** `fits:` then `sweeps` in one config; the same results with `max_workers=1` and `N`; Python, CLI and HTTP all store identical specs. Uses the flex-pse fixtures and synthetic linear data with known coefficients (ARMA data is added with U8). |
| Hooks (D1) | callable, registered-name and import-string resolution; a bad import string gives a clear error; `SolveOutcome` failure handling. |
| Specs (D2) | canonical hashing ignores key order and `1` vs `1.0`, and sees values and list order; a **pinned hash** guards the canonical form; `run_hash` over overrides; non-JSON values (sets, NaN, infinity) rejected; format detection; validators skipped when their library is missing; dict and pydantic forms store identical normalised JSON; normalisation is idempotent; kind/format mismatch rejected; tag rules; `SpecRecord` JSON round trip and `verify()` tamper check; a stored, normalised spec replays through `build`. |
| Frames (D7) | polars ↔ pandas, DatetimeIndex restored, dtypes kept. |
| Query (D8) | study scoping; read-only SPARQL rejects UPDATE; `runs.where` filters. |
| Study config | schema validation, sweep expansion (product and zip), `output_name`, dry-run, resume. |
| Parallel (D13) | `run_task` on its own (unit); Ray component tests: `TaskSpec` serialisation, a `__main__` hook rejected, a hung task killed at its timeout, retries, `on_error`, cancellation, `max_calls` restarts, thread env vars set, uniqueness with many tasks, **the same results with `max_workers=1` and `N`**. A multi-node per-tenant Ray integration test. |
| **Isolation suite (D14)** | With tenants A and B: **(DB)** B's role can't `CONNECT` to A's database, and can't create databases or roles; **(secret)** A's secret file isn't mounted in B's containers, and isn't readable inside the orchestrator container; **(network)** B's orchestrator and Ray can't resolve or reach A's Acquirium, orchestrator or Ray, and nothing outside can reach either Acquirium; **(volume)** A's `data_dir` isn't mounted anywhere in B; **(compute)** a malicious hook in B (it tries to open A's hostnames and ports, read `/var/run/docker.sock`, and list mounts) finds nothing of A's; **(data)** a study and spec written in A are invisible from every B API; containers are non-root. Phase 2 adds the **(auth)** cases: B's token gets 403 on every A route; no token gets 401; an expired or bad-signature token gets 401; role checks per route; forged gateway headers are ignored. |
| **Backup / restore** | drill: write known data into A → back up → destroy A → restore to a point in time in a scratch tenant → `verify` matches the studies, tags, a timeseries sample, the graph metadata and the file artifacts; a per-tenant `pg_dump` restore; a deprovision takes its final backup before dropping. |
| Server (Phase 2) | FastAPI `TestClient`: auth and roles, 409 on duplicates, job lifecycle and cancel, results JSON shape (golden files), `/query`, SSE progress, OpenAPI schema snapshot. |
| Examples | every `examples/*.py` runs as an `integration` test. |
| Docs (§8) | the `flexorchdoc` table functions are unit-tested without Sphinx (field tables match the pydantic models, the env table matches `Settings`); `sphinx-build -W --keep-going` runs in CI; the scripts embedded on doc pages are the ones the integration tests run. |

---

## 8. Documentation (same structure and tooling as flex-pse)

**Tooling** (copied from flex-pse):
- **Sphinx** with `myst-parser` (Markdown pages) and the **furo** theme. Reference pages use `autodoc`,
  `autosummary` and `napoleon` with Google-style docstrings.
- `intersphinx` links to python, pyomo, pandas, pydantic and numpy, plus polars and ray. flex-pse is added once its
  Pages site publishes `objects.inv`.
- `nitpicky = True`, and the build runs with `sphinx-build -W --keep-going`, so **warnings fail CI**.
- A `docs` extra in `pyproject.toml` (`sphinx`, `myst-parser`, `furo`). `ruff` with `pydocstyle.convention = "google"`
  checks the docstrings on the public API.
- `.github/workflows/docs.yml` builds on every PR and deploys to GitHub Pages on `main`, the same as flex-pse.
- The same skip hook as flex-pse: members whose docstring is marked `TODO`, `DEPRECATED` or `.. deprecated::` are
  left out of the reference.

**Layout:**

```
docs/
  conf.py  Makefile  index.rst  changelog.md
  _ext/flexorchdoc.py            # directives that generate tables from code (below)
  _static/  _templates/autosummary/
  getting_started/
    installation.md              # conda envs, idaes get-extensions, connection modes          (M0)
    ten_minutes.md               # scripts 1 + 2: store data/specs, solve, store + read results (M2)
  explanation/
    architecture_and_layering.md          # library vs server, packages, import rules          (M0)
    studies_tags_and_uniqueness.md        # D9/D10: studies, tags, duplicates, tombstones       (M1)
    connection_modes_and_credentials.md   # D11: local / postgres / remote, secrets             (M1)
    storage_layout_in_acquirium.md        # D5/D10: what is stored where, backup coverage       (M1)
    model_data_export_and_units.md        # D3: time-set detection, keys, indexing, QUDT units  (M2)
    export_format.md                      # D17: folder layout, flexorch-export/1 schema        (M3)
    parallel_execution.md                 # D13: Ray, task model, main-process writes           (M5)
    tenancy_isolation_and_backups.md      # D14: per-tenant stacks, guarantees, backups         (M6)
    auth_and_http_api.md                  # D15/D16: tokens, roles, control plane contract      (M7)
    extending_the_orchestrator.md         # D18: commands vs plugins vs subclassing vs routes   (M4, Phase 2 part in M7)
  how_to/
    connect_to_postgres.md                (M1)
    store_and_retrieve_data.md            (M1)
    bring_your_own_build_and_solve.md     (M2)
    select_and_export_model_variables.md  (M2)
    query_acquirium.md                    (M3)
    export_and_import_a_study.md          (M3)
    run_a_study_from_config.md            (M4)
    run_sweeps_in_parallel_with_ray.md    (M5)
    fit_a_surrogate_from_acquirium_data.md (M4; HTTP example added in M7)
    write_a_custom_command.md             (M4)
    write_and_ship_a_plugin.md            (M4; routes added in M7)
    subclass_the_orchestrator.md          (M4)
    add_custom_http_routes.md             (M7)
    provision_a_tenant.md                 (M6)
    back_up_and_restore_a_tenant.md       (M6)
    call_the_http_api.md                  (M7)
    integrate_with_the_control_plane_and_ui.md  (M8)
  examples/
    index.md                     # embeds examples/*.py with literalinclude; these are the same scripts the
                                 #   integration tests run, so the docs can't drift from working code
  reference/
    flex_orchestrator/index.rst  # Orchestrator, ResultHandle, errors
    flex_orchestrator/export.rst # export_model_data, ModelData, UnitMapper
    flex_orchestrator/archive.rst# export/import API + flexorch-export/1 schema tables
    flex_orchestrator/study.rst  # StudyConfig (field table), run_task, TaskSpec/TaskResult
    flex_orchestrator/hooks.rst  # BuildFn/SolveFn/ExportFn, SolveOutcome, flex-pse default hooks
    flex_orchestrator/backend.rst# StorageBackend protocol
    flex_orchestrator/settings.rst # connection modes + FLEXORCH_* env var table
    flex_orchestrator/ext.rst    # @command, Plugin, events, stable subclass methods, PLUGIN_API_VERSION
    flex_orchestrator/fitting.rst# fit_surrogate parameters, selector and prep tables (generated from pydantic)
    flex_orchestrator/testing.rst# helpers for plugin authors
    flex_orchestrator/server_deps.rst # Phase 2: create_app, deps for plugin routes
    cli.rst                      # flex-orch commands (generated from --help)
    http_api.rst                 # Phase 2: OpenAPI-generated route reference
    glossary.md                  # study, tag, spec, run, tombstone, tenant, time set, …
```

`index.rst` uses flex-pse's toctree captions in the same order: *Getting started · Explanation · How-to · Examples ·
Release notes · Reference*.

**Docs generated from code, so reference pages can't drift** (the same idea as flex-pse's `flexdoc` extension, kept
small):
- `.. flexorch-config-table:: <pydantic model>` renders a field table: name, type, default and description. It is
  used for `StudyConfig`, the `execution` and `export` blocks, every `flexorch-export/1` file model, and `Settings`.
- `.. flexorch-env-table::` lists every `FLEXORCH_*` env var and secret file, with its source and precedence.
- `.. flexorch-cli::` includes the `--help` output of each CLI command.
- Phase 2 adds `.. flexorch-openapi::`, which renders the route reference from the app's generated OpenAPI schema.
- The JSON Schemas in `docs/schemas/` are written by the same code and linked from `export_format.md`.
- The table-building functions don't depend on Sphinx, so unit tests call them directly, as flex-pse's
  `test_flexdoc_tables.py` does.

**Rules:**
- **Each milestone adds the pages listed next to it above.** A milestone isn't done until its pages are written and
  the docs build passes with `-W`.
- Every public function and class has a Google-style docstring with at least one usage example. Ruff checks the
  docstrings, and nitpicky mode checks the cross-references.
- How-to pages use only the public API. Their code blocks are copied from `examples/` or tested snippets, never
  written only for the docs.
- `docs/changelog.md` gets an entry with every PR, matching the CHANGELOG rule in §9.

---

## 9. Processes

- **Branching:** trunk-based on `main`. Short-lived `feat/…` and `fix/…` branches, squash-merged. Each PR is one
  milestone task and includes its tests.
- **PR checklist:** tests in the right tier; coverage floor met; docs or an example updated; a CHANGELOG entry; no
  layering violations (especially no HTTP in the core library); `ruff check .` and `black --check .` are clean (D20).
- **Code style:** black and ruff exactly as in flex-pse (D20). Install the hooks once with
  `pre-commit install --hook-type pre-commit --hook-type pre-push`. Never use `--no-verify` to get a commit through.
  PRs that only reformat stay separate from PRs that change behaviour.
- **Security review:** any PR that touches `connection.py`, `settings.py`, `deploy/tenant/` or `server/auth.py` needs
  a second reviewer, and must keep the isolation suite and the redaction test passing.
- **Extension API changes:** any change to `ext/`, `testing/` or `server/deps.py` updates `PLUGIN_API_VERSION`
  (a breaking change bumps the major version) and records it in the changelog. The sample plugin must keep passing.
- **Versioning:** SemVer. `0.1.0` at the end of Phase 1, `0.2.0` at the end of Phase 2. Every stored run records the
  package versions and hook names.
- **Operations:** the restore drill runs every quarter against a scratch tenant. Tenant database secrets are rotated
  by re-provisioning.
- **Upstream sync:** U-items are tracked as issues in their upstream repos. The pinned SHAs are checked at every
  milestone.
- **Definition of done for a milestone:** the exit criterion is met; CI passes on Windows and Linux (unit +
  component); integration passes on Linux; the example is written; **the milestone's docs pages (§8) are written and
  `sphinx-build -W` passes**.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| PR #78 API changes before merge | Every Acquirium call goes through `backend/acquirium.py`, and the contract tests catch drift. |
| Data leaks between tenants | One stack per tenant (database, role, secret, volume, network, Ray), with nothing shared. The isolation suite gates M6, M7 and M8. Phase 2 checks the token's tenant claim in the orchestrator itself. |
| User hook code is arbitrary code | It runs only inside the tenant's own containers and Ray: non-root, no Docker socket, no route to other tenants. |
| Shared Postgres cluster is a single point of failure | PITR backups, monitoring, and optionally a streaming replica. Per-tenant `pg_dump` for moving tenants elsewhere. |
| Backups miss graph or artifacts (not in Postgres) | Volume snapshots plus the Oxigraph export taken with each database backup, a restore drill as an exit criterion, and U7 upstream. |
| Tombstones lost in a restore | Tombstones live in the SQL ledger, not in graph metadata. |
| Database credentials leak | Secret files only, `SecretStr`, redaction everywhere, the redaction test, and U5. |
| Two Acquirium servers on one database | One Acquirium per tenant database by construction, plus the mode 2 server lock. |
| `recreate=true` drops data | Rejected by the orchestrator. The tenant role can only affect its own database, and backups cover it. |
| No hard delete in Acquirium | Tombstone ledger, `purge` as a placeholder, and U3. |
| Acquirium `define` silently returns an existing study | Check for existence first, verify after writing, and one writing process per tenant. |
| A label's unit can't change | Convert to the declared unit, and raise an error if the dimensions don't match. |
| Pyomo units with no QUDT match | Mapping table, resolver fallback, overrides, and strict mode. |
| Many streams per run (no parquet) | M2 benchmark and `include`/`exclude` selection. |
| Hung or leaking IPOPT solves | `ray.cancel(force=True)` and `max_calls` restarts. |
| Ray on Windows is less mature | Local Ray tested on Windows in CI. Tenant stacks are Linux. `max_workers=1` avoids Ray entirely. |
| Cost of 10–50 tenant stacks | Idle tenants stopped with their volumes kept; `exact_only=true` on Acquirium; Ray started only when needed. |
| Export format drifts from what's stored | A versioned `flexorch-export/N` schema built from the same pydantic models the code uses, a round-trip test in CI, and import that refuses unknown versions. |
| Large JSON results (many keys × 8,760 steps) | One file per result, streamed writes, optional gzip, and `include`/`exclude` at export time. |
| Regression on poor or ambiguous data | `gaps=error` by default, ambiguous `resolve` fails, every fit records its provenance and data hash, and holdout metrics are available where `predict` exists. |
| Large training pulls exhaust memory | `max_rows`, windowed selectors, data loaded once and shared through `ray.put`. |
| Regression API changes in flex-pse | Only the `Regressor` protocol and `get_regressor` are used, through `flexpse/regression.py`, and contract tests run against the pinned flex-pse. |
| A plugin route ships without auth | Auth dependencies are attached to the router when it is mounted, reserved prefixes are enforced, and a test walks every registered route. |
| Plugins break when the core changes | A small stable surface (commands, `Plugin`, events, the listed subclass methods, `server/deps`), versioned with `PLUGIN_API_VERSION`, and the sample plugin tested in CI. |
| Plugin code compromises a tenant | Plugins are installed per image, enabled per tenant through the allowlist, run inside the tenant sandbox, and the isolation suite runs with a hostile plugin enabled. |
| HTTP arriving late hides integration problems | The core API is shaped so it maps 1:1 onto the §4 routes, and hook import strings and `TaskSpec` are plain data from M4 on. M7 is mostly wiring. |
