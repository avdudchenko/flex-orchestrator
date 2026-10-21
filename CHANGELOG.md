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
- `flex_orchestrator.testing`: fake hooks for tests without a solver, and the
  shared flex-pse specs tests build models from: `DEMO_SPEC`, plus
  `LOGIC_SPEC`, `SURROGATE_DISPATCH_SPEC` and `NETWORK_SPEC` emitted from
  flex-pse's round-trip corpus (repo-wide `demo_spec` / `demo_model`
  fixtures).
- Project scaffold: `pyproject.toml`, black/ruff/import-linter config and
  pre-commit hooks matching flex-pse (PLAN.md D20), pytest tier markers.
