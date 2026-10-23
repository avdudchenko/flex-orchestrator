"""The built-in ``fit_surrogate`` command (PLAN.md D19).

For each fit (one per variant): load and prepare the data (once per distinct
selection), fit the regressor, store the fit run in ``<s>::fits``, then store
the surrogate spec, whose metadata names the fit run. Tags are checked before
anything is fitted, so a taken tag never costs a fit.
"""

import json
from typing import Any

from flex_orchestrator.errors import FitError
from flex_orchestrator.ext.commands import Progress, command
from flex_orchestrator.fitting import prep as preparation
from flex_orchestrator.fitting import regress, selectors, store
from flex_orchestrator.fitting.models import FitParams, FitPlan, FitRecord, FitSummary
from flex_orchestrator.orchestrator import FIT_KIND, SURROGATE_SPEC


@command(name="fit_surrogate", long_running=True)
def fit_surrogate(
    orch: Any, params: FitParams, progress: Progress, **runtime: Any
) -> FitSummary:
    """Fit surrogates from stored data and store each spec with its fit run."""
    return run_fits(orch, params, progress)


FIT_SURROGATE = fit_surrogate


def run_fits(orch: Any, params: FitParams, progress: Progress) -> FitSummary:
    """Run every fit of ``params``; see :mod:`flex_orchestrator.fitting.command`.

    Raises:
        FitError: If a fit can't be done (see the selectors, preparation and
            regressor errors).
        TagExistsError: With ``on_existing: error``, if a tag is stored.
    """
    if params.max_workers > 1:
        raise FitError(
            "max_workers > 1 fits variants in parallel on Ray, which arrives with "
            "M5; use max_workers=1."
        )
    plans = params.expand()
    replace = params.on_existing == "replace"
    skipped = set()
    for plan in plans:
        if params.on_existing == "skip" and _stored(orch, plan.tag):
            skipped.add(plan.tag)
            continue
        orch._check_new(plan.tag, SURROGATE_SPEC, replace)
        orch._check_new(plan.tag, FIT_KIND, replace)
    progress.update(done=0, total=len(plans), message="fitting")
    cache: dict[str, preparation.Prepared] = {}
    records = []
    for plan in plans:
        progress.check()
        if plan.tag in skipped:
            records.append(FitRecord(tag=plan.tag, status="skipped"))
        else:
            records.append(_fit_one(orch, plan, cache, replace))
        progress.advance(message=f"{plan.tag}: {records[-1].status}")
    return FitSummary(fits=records)


def _stored(orch: Any, tag: str) -> bool:
    return any(s.tag == tag for s in orch.stored_specs(SURROGATE_SPEC))


def _fit_one(
    orch: Any,
    plan: FitPlan,
    cache: dict[str, preparation.Prepared],
    replace: bool,
) -> FitRecord:
    key = json.dumps(
        plan.model_dump(mode="json", include={"X", "y", "window", "prep"}),
        sort_keys=True,
    )
    if key not in cache:
        xs = [selectors.load(orch, s, plan.window) for s in _listed(plan.X)]
        ys = [selectors.load(orch, s, plan.window) for s in _listed(plan.y)]
        cache[key] = preparation.prepare(xs, ys, plan.prep)
    data = cache[key]
    X, y, holdout = data.X, data.y, None
    if plan.validation is not None:
        X, y, X_test, y_test = preparation.split_holdout(X, y, plan.validation.holdout)
    fitted = regress.fit(
        plan.regressor, plan.params, plan.spec_options, X, y, data.units
    )
    if plan.validation is not None:
        holdout = regress.metrics(y_test.iloc[:, 0], regress.predict(fitted, X_test))
        holdout["rows"] = len(X_test)
    spec = dict(fitted.spec)
    spec["provenance"] = {
        **(spec.get("provenance") or {}),
        "fit": {
            "regressor": plan.regressor,
            "data_hash": data.data_hash,
            "n_samples": fitted.n_samples,
            "data_window": fitted.data_window,
        },
    }
    result = {
        "regressor": plan.regressor,
        "coefficients": fitted.coefficients,
        "metrics": fitted.metrics,
        "n_samples": fitted.n_samples,
        "data_window": fitted.data_window,
        "diagnostics": fitted.diagnostics,
        "holdout": holdout,
    }
    records: dict[str, Any] = {
        store.FIT_PARAMS: plan.model_dump(mode="json"),
        store.TRAINING_DATA: {
            "resolved": data.provenance,
            "steps": data.steps,
            "rows": len(data.X),
            "train_rows": len(X),
            "columns": {"X": list(X.columns), "y": list(y.columns)},
            "units": data.units,
            "data_hash": data.data_hash,
        },
        store.FIT_RESULT: result,
    }
    if plan.store_series:
        records[store.FIT_SERIES] = store.series_table(
            X.index, y.iloc[:, 0].tolist(), regress.predict(fitted, X).tolist()
        )
    run_id = store.write(
        orch,
        plan.tag,
        {"regressor": plan.regressor, "data_hash": data.data_hash},
        records,
        replace,
    )
    with orch._with_metadata({"fit_run_id": run_id, "data_hash": data.data_hash}):
        record = orch.store_surrogate_spec(plan.tag, spec, replace=replace)
    return FitRecord(
        tag=plan.tag,
        status="stored",
        run_id=run_id,
        spec_hash=record.spec_hash,
        regressor=plan.regressor,
        coefficients=fitted.coefficients,
        metrics=fitted.metrics,
        holdout=holdout,
        n_samples=fitted.n_samples,
        data_hash=data.data_hash,
    )


def _listed(selectors_: Any) -> list:
    return selectors_ if isinstance(selectors_, list) else [selectors_]
