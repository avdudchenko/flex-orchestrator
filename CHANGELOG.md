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
- `flex_orchestrator.testing` fake hooks for tests without a solver.
- Project scaffold: `pyproject.toml`, black/ruff/import-linter config and
  pre-commit hooks matching flex-pse (PLAN.md D20), pytest tier markers.
