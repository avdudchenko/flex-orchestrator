# Examples

Scripts that use flex-orchestrator from start to finish. They aren't part of the
installed package, and each runs in the test suite as an integration test
(`src/flex_orchestrator/tests/test_examples.py`), except
`ro_study/compare_ray.py`, a timing demo that takes a few minutes.

| Example | What it covers |
|---|---|
| [`ro_surrogates/`](ro_surrogates/README.md) | One short script: store synthetic RO data, emit a tariff-priced flex-pse model's spec and store it, fit an average energy intensity and a linear power surrogate from the data, solve the model with each, and compare them. |
| [`ro_study/`](ro_study/README.md) | The same model as study configs, one script per sweep: surrogates (fitted with the built-in regression), a variable, a configuration option, a nested sweep, and hand-picked cases. Plus the surrogate sweep stored in a folder on disk with no Acquirium, and a timing of a nested sweep in-process against Ray. |
| [`plugins/ro_tools/`](plugins/ro_tools/README.md) | A sample plugin, with its own `pyproject.toml` and tests: a command, named hooks and an orchestrator subclass. |

Run them from the repository root in the `flex-orch-dev` environment.
