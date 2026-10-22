# Examples

Scripts that use flex-orchestrator from start to finish. They aren't part of the
installed package, and each runs in the test suite as an integration test
(`src/flex_orchestrator/tests/test_examples.py`).

| Example | What it covers |
|---|---|
| [`ro_surrogates/`](ro_surrogates/README.md) | One short script: store synthetic RO data, emit a tariff-priced flex-pse model's spec and store it, fit an average energy intensity and a linear power surrogate from the data, solve the model with each, and compare them. |

Run them from the repository root in the `flex-orch-dev` environment.
