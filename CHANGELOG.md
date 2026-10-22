# Changelog

## Unreleased

### Added
- User-replaceable build and solve hooks (PLAN.md D1): `BuildFn`, `SolveFn`,
  `ApplySurrogateFn` and `SwapSurrogateFn` protocols, `SolveOutcome`, `HookSet`,
  `HookRegistry` (callables, registered names and import strings), and
  `run_solve`.
- Default flex-pse hooks in `flex_orchestrator.flexpse`, targeting flex-pse
  v0.2.0 (flowsheet spec schema 0.1.0): `build` (overrides become `fix`
  elements), `solve` (honours the spec's `solver`), `apply_surrogate` and
  `swap_surrogate` (`surrogate` elements, optional `relation`).
- Specs as data (PLAN.md D2): `flex_orchestrator.specs` with `to_json_spec`,
  canonical `spec_hash` / `run_hash`, `detect_format`, `validate_spec`
  (normalises flex-pse specs through their validators), `check_tag` and
  `SpecRecord`; flex-pse validators in `flex_orchestrator.flexpse`.
- Model data exporter (PLAN.md D3): `flex_orchestrator.export` with
  `export_model_data` (identity-based time detection, time / time x custom /
  custom / scalar classification, `ModelData` / `ExportedSeries`), and
  `UnitMapper`, which generates QUDT keys from pint's unit parts
  (`L/day` -> `L-PER-DAY`) and verifies them through an optional resolver,
  plus `conversion_factor`. New `ExportError` and `UnitError`; new
  `pyomo` extra.
- A `time_sets` hook kind (`TimeSetsFn`); the flex-pse default
  (`flex_orchestrator.flexpse.time_sets`) registers the TimeBlock's time
  set plus the `startup` / `shutdown` index sets, so those export as
  timeseries.
- Explicit results API (PLAN.md D4): `Orchestrator` with `create_study` /
  `select_study` / `get_or_create_study` (D9 guards),
  `store_model_results(tag, model, outcome, keys, include, exclude)`,
  `solve_and_store(tag, model, ...)` and `model_results(tag)`, returning
  `StoredResult`. A tag is stored once; a result whose solve wasn't `ok` is a
  `failed` run holding only its outcome, so the tag can be retried.
- Storage backends (PLAN.md D10): the `StorageBackend` contract,
  `MemoryBackend` and `AcquiriumBackend`, and the D3 step 4 writer
  (`flex_orchestrator.results`): only verified QUDT units are declared, the
  Pyomo unit, component and index go in variable metadata, and later runs are
  converted to a label's declared unit. New `StorageError`,
  `StudyExistsError`, `StudyNotFoundError`, `TagExistsError` and
  `TagNotFoundError`; new `acquirium` extra.
- Data API (PLAN.md D6): `Orchestrator.store_data(name, df, units=,
  time_column=)` and `get_data(name, columns=, start=, end=, column=,
  frame=)`; one dataset is one run of `<s>::data`, one timeseries per column.
  Units given as text (`m3/day`) or QUDT keys. New `AmbiguousColumnError`,
  `StoredData`, the `frame=` default on `Orchestrator`, and
  `flex_orchestrator.frames` (polars/pandas in and out). polars and pandas
  are now dependencies.
- `Orchestrator.store_model_data(tag, data, outcome)` stores values that are
  already exported.
- Results are stored as a snapshot (PLAN.md D5): a `model_values` table
  (Parquet, through Acquirium's `table` kind) and a `model_manifest` JSON.
  `streams=` on `store_model_results` / `store_model_data` /
  `solve_and_store` also stores chosen keys as native variables, and
  `model_results(tag, units=)` converts with pint. `StoredResult.streams`.
- Storage contract: the `table` kind; a timeseries sample must have a value
  (missing values are left out). The Acquirium backend declares units as QUDT
  URIs.
- Acquirium pin moved to PR #78 `9a70a1f`.
- Storage contract: `variables(study)` and `records(..., labels=)`.
- An opt-in `benchmark` tier (`pytest -m benchmark -s`) with the D5 storage
  benchmark.
- The flex-pse `time_sets` hook returns `[]` for a model without exactly one
  TimeBlock, so user models store through the default hooks.
- `flex_orchestrator.testing`: fake hooks for tests without a solver, and the
  shared flex-pse specs tests build models from: `DEMO_SPEC`, plus
  `LOGIC_SPEC`, `SURROGATE_DISPATCH_SPEC` and `NETWORK_SPEC` emitted from
  flex-pse's round-trip corpus (repo-wide `demo_spec` / `demo_model`
  fixtures).
- Project scaffold: `pyproject.toml`, black/ruff/import-linter config and
  pre-commit hooks matching flex-pse (PLAN.md D20), pytest tier markers.
