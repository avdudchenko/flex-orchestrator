"""The built-in ``fit_surrogate`` command (PLAN.md D19).

For each fit (one per variant): load and prepare the data (once per distinct
selection), fit the regressor, store the fit run in ``<s>::fits``, then store
the surrogate spec, whose metadata names the fit run. Tags are checked before
anything is fitted, so a taken tag never costs a fit. With ``max_workers > 1``
the fits themselves run on Ray (D13, D19); loading and storing stay here.
"""

import json
from typing import Any

from flex_orchestrator.errors import TagNotFoundError
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


def run_fits(
    orch: Any,
    params: FitParams,
    progress: Progress,
    execution: Any = None,
    workers: int | None = None,
) -> FitSummary:
    """Run every fit of ``params``; see :mod:`flex_orchestrator.fitting.command`.

    With more than one worker the variants are fitted on Ray: the data is
    loaded and prepared here, once per selection, and sent to the workers
    once (``ray.put``); this process stores every fit. Ray is the one at the
    study's address, else ``params.ray_address``, else the orchestrator's
    (``Orchestrator(ray_address=)`` or ``FLEXORCH_RAY_ADDRESS``), else local.

    Args:
        orch: The orchestrator.
        params: The fits.
        progress: Advanced after each fit.
        execution: A study's ``Execution`` (Ray address, resources), when the
            fits run as part of one.
        workers: Fits at once; ``params.max_workers`` if ``None``.

    Raises:
        FitError: If a fit can't be done (see the selectors, preparation and
            regressor errors).
        TagExistsError: With ``on_existing: error``, if a tag is stored.
    """
    workers = workers or params.max_workers
    plans = params.expand()
    replace = params.on_existing == "replace"
    skipped = set()
    for plan in plans:
        if params.on_existing == "skip" and _stored(orch, plan.tag):
            skipped.add(plan.tag)
            continue
        orch._check_new(plan.tag, SURROGATE_SPEC, replace)
        orch._check_new(plan.tag, FIT_KIND, replace)
    todo = [plan for plan in plans if plan.tag not in skipped]
    progress.update(done=0, total=len(plans), message="fitting")
    cache: dict[str, preparation.Prepared] = {}
    data = {plan.tag: _prepared(orch, plan, cache) for plan in todo}
    if workers > 1 and len(todo) > 1:
        address = (
            (execution and execution.ray_address)
            or params.ray_address
            or orch.ray_address
        )
        computed = _on_ray(todo, data, workers, execution, progress, address)
    else:
        computed = {}
    records = []
    for plan in plans:
        progress.check()
        if plan.tag in skipped:
            records.append(FitRecord(tag=plan.tag, status="skipped"))
        else:
            result = computed.get(plan.tag) or _compute(plan, data[plan.tag])
            records.append(_write(orch, plan, data[plan.tag], result, replace))
        progress.advance(message=f"{plan.tag}: {records[-1].status}")
    return FitSummary(fits=records)


def _stored(orch: Any, tag: str) -> bool:
    try:
        orch._latest(tag, SURROGATE_SPEC)
    except TagNotFoundError:
        return False
    return True


def _prepared(
    orch: Any, plan: FitPlan, cache: dict[str, preparation.Prepared]
) -> preparation.Prepared:
    """The plan's data, loaded and prepared once per distinct selection."""
    key = json.dumps(
        plan.model_dump(mode="json", include={"X", "y", "window", "prep"}),
        sort_keys=True,
    )
    if key not in cache:
        xs = [selectors.load(orch, s, plan.window) for s in _listed(plan.X)]
        ys = [selectors.load(orch, s, plan.window) for s in _listed(plan.y)]
        cache[key] = preparation.prepare(xs, ys, plan.prep)
    return cache[key]


def _on_ray(
    plans: list[FitPlan],
    data: dict[str, preparation.Prepared],
    workers: int,
    execution: Any,
    progress: Progress,
    ray_address: str | None,
) -> dict[str, dict[str, Any]]:
    """Fit ``plans`` on Ray at ``ray_address``; each prepared dataset is sent once."""
    from flex_orchestrator.parallel import connect, map_ordered

    ray = connect(ray_address)
    shared: dict[int, Any] = {}
    for plan in plans:
        prepared = data[plan.tag]
        if id(prepared) not in shared:
            shared[id(prepared)] = ray.put(prepared)
    options: dict[str, Any] = {}
    if execution is not None:
        options = {
            "num_cpus": execution.resources.num_cpus,
            "memory": execution.resources.memory,
            "max_calls": execution.max_calls,
        }
    results = map_ordered(
        _compute,
        [(plan, shared[id(data[plan.tag])]) for plan in plans],
        workers=workers,
        ray_address=ray_address,
        progress=progress,
        **options,
    )
    return {plan.tag: result for plan, result in zip(plans, results, strict=True)}


def _compute(plan: FitPlan, data: preparation.Prepared) -> dict[str, Any]:
    """Fit one plan: the spec, the fit result and the series, as plain data.

    Needs no store, so it runs on a Ray worker as well as here.
    """
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
    series = None
    if plan.store_series:
        series = store.series_table(
            X.index, y.iloc[:, 0].tolist(), regress.predict(fitted, X).tolist()
        )
    return {
        "spec": spec,
        "result": result,
        "train_rows": len(X),
        "columns": {"X": list(X.columns), "y": list(y.columns)},
        "series": series,
    }


def _write(
    orch: Any,
    plan: FitPlan,
    data: preparation.Prepared,
    computed: dict[str, Any],
    replace: bool,
) -> FitRecord:
    """Store a computed fit: the fit run, then the surrogate spec naming it."""
    result = computed["result"]
    records: dict[str, Any] = {
        store.FIT_PARAMS: plan.model_dump(mode="json"),
        store.TRAINING_DATA: {
            "resolved": data.provenance,
            "steps": data.steps,
            "rows": len(data.X),
            "train_rows": computed["train_rows"],
            "columns": computed["columns"],
            "units": data.units,
            "data_hash": data.data_hash,
        },
        store.FIT_RESULT: result,
    }
    if computed["series"] is not None:
        records[store.FIT_SERIES] = computed["series"]
    run_id = store.write(
        orch,
        plan.tag,
        {"regressor": plan.regressor, "data_hash": data.data_hash},
        records,
        replace,
    )
    with orch._with_metadata({"fit_run_id": run_id, "data_hash": data.data_hash}):
        record = orch.store_surrogate_spec(plan.tag, computed["spec"], replace=replace)
    return FitRecord(
        tag=plan.tag,
        status="stored",
        run_id=run_id,
        spec_hash=record.spec_hash,
        regressor=plan.regressor,
        coefficients=result["coefficients"],
        metrics=result["metrics"],
        holdout=result["holdout"],
        n_samples=result["n_samples"],
        data_hash=data.data_hash,
    )


def _listed(selectors_: Any) -> list:
    return selectors_ if isinstance(selectors_, list) else [selectors_]
