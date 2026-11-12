"""Resolve a regressor, fit it, and predict with the fit.

A regressor is named by a flex-pse ``SurrogateType`` (resolved by the
flex-pse hook :func:`flexorchestrator.flexpse.get_regressor`) or by the import
string of any class following flex-pse's ``Regressor`` protocol:
``fit(X, y)``, ``to_fit_result()`` and ``to_surrogate_spec(**options)``.

Nothing here is specific to one regressor. The columns' units are passed to
``fit`` and ``to_surrogate_spec`` when they take ``input_units`` /
``output_units`` (flex-pse's ``multilinear`` and ``constant_intensity`` do),
so every surrogate spec carries the units its data was fitted in.
"""

import inspect
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from flexorchestrator.errors import FitError, HookError
from flexorchestrator.hooks import import_hook
from flexorchestrator.specs import to_json_spec

GET_REGRESSOR = "flexorchestrator.flexpse:get_regressor"
"""str: The flex-pse hook that resolves a ``SurrogateType`` name to a class."""
PREDICT = "flexorchestrator.flexpse:predict_surrogate"
"""str: The flex-pse hook that evaluates a fitted surrogate spec on data."""


@dataclass
class Fitted:
    """One fitted regressor.

    Attributes:
        regressor: The fitted regressor object.
        spec: Its surrogate spec, as JSON.
        coefficients: The fit result's coefficients.0.
        metrics: The fit result's metrics (``r2``, ``rmse``, ...).
        n_samples: Rows the fit used.
        data_window: ``[first, last]`` timestamp of the rows used, ISO 8601.
        diagnostics: ``fit_diagnostics``, if the regressor has them.
    """

    regressor: Any
    spec: dict[str, Any]
    coefficients: dict[str, float]
    metrics: dict[str, float]
    n_samples: int
    data_window: list[str | None]
    diagnostics: dict[str, Any] = field(default_factory=dict)


def regressor_class(name: str) -> type:
    """Return the regressor class ``name`` names.

    Raises:
        FitError: If it can't be imported, isn't a class, or is a surrogate type
            the installed flex-pse doesn't fit.
    """
    if ":" in name:
        try:
            cls = import_hook(name)
        except HookError as exc:
            raise FitError(f"Can't import regressor {name!r}: {exc}") from exc
        if not isinstance(cls, type):
            raise FitError(
                f"Regressor {name!r} is a {type(cls).__name__}, not a class."
            )
        return cls
    try:
        get = import_hook(GET_REGRESSOR)
    except HookError as exc:
        raise FitError(
            f"Regressor {name!r} is a flex-pse surrogate type, and flex-pse isn't "
            f"installed ({exc}). Install it, or name a class by import string."
        ) from exc
    try:
        return get(name)
    except NotImplementedError as exc:
        raise FitError(str(exc)) from exc
    except Exception as exc:
        raise FitError(f"Unknown regressor {name!r}: {exc}") from exc


def fit(
    regressor: str,
    params: dict[str, Any],
    spec_options: dict[str, Any],
    X: pd.DataFrame,
    y: pd.DataFrame,
    units: dict[str, str | None],
) -> Fitted:
    """Fit ``regressor`` on ``X`` / ``y`` and return the fit and its surrogate spec.

    Raises:
        FitError: If the regressor needs units a column doesn't have, or the
            fit raises.
    """
    cls = regressor_class(regressor)
    try:
        model = cls(**params)
    except TypeError as exc:
        raise FitError(
            f"Regressor {regressor!r} rejects params {params}: {exc}"
        ) from exc
    inputs = {c: units.get(c) for c in X.columns}
    output = units.get(y.columns[0])
    unit_args = {"input_units": inputs, "output_units": output}
    fit_args = _unit_args(model.fit, unit_args, regressor)
    try:
        model.fit(X, y, **fit_args)
    except Exception as exc:
        raise FitError(f"Fitting {regressor!r} failed: {exc}") from exc
    options = dict(spec_options)
    for key, value in _unit_args(model.to_surrogate_spec, unit_args, regressor).items():
        # A single-input regressor takes its input's unit as one string.
        if key == "input_units" and _wants_str(model.to_surrogate_spec, key):
            value = next(iter(value.values()))
        options.setdefault(key, value)
    try:
        spec = to_json_spec(model.to_surrogate_spec(**options))
        result = model.to_fit_result()
    except Exception as exc:
        raise FitError(
            f"{regressor!r} couldn't write its surrogate spec: {exc}"
        ) from exc
    diagnostics = getattr(model, "fit_diagnostics", None)
    if callable(diagnostics):
        diagnostics = diagnostics()
    window = list(getattr(result, "data_window", ()) or ())
    return Fitted(
        regressor=model,
        spec=spec,
        coefficients={k: float(v) for k, v in dict(result.coefficients).items()},
        metrics={k: float(v) for k, v in dict(result.metrics).items()},
        n_samples=int(result.n_samples),
        data_window=[_iso(w) for w in window],
        diagnostics=_plain(dict(diagnostics or {})),
    )


def _unit_args(method: Any, unit_args: dict[str, Any], regressor: str) -> dict:
    try:
        accepted = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - a builtin method
        return {}
    wanted = {k: v for k, v in unit_args.items() if k in accepted}
    if "input_units" in wanted:
        missing = [c for c, u in wanted["input_units"].items() if u is None]
        if missing:
            raise FitError(
                f"{regressor!r} needs the unit of every input; {missing} have "
                "none. Give units= on the selector."
            )
    if "output_units" in wanted and wanted["output_units"] is None:
        raise FitError(
            f"{regressor!r} needs the output's unit, which the data doesn't have. "
            "Give units= on the y selector."
        )
    return wanted


def _wants_str(method: Any, name: str) -> bool:
    annotation = inspect.signature(method).parameters[name].annotation
    return annotation is str or annotation == "str"


def predict(fitted: Fitted, X: pd.DataFrame) -> pd.Series:
    """Evaluate the fit on ``X``.

    Uses the regressor's own ``predict`` when it has one; otherwise the
    flex-pse hook evaluates the surrogate spec (the 0.2 regressors have no
    ``predict``).

    Raises:
        FitError: If neither can evaluate it.
    """
    own = getattr(fitted.regressor, "predict", None)
    if callable(own):
        values = own(X)
    else:
        try:
            values = import_hook(PREDICT)(fitted.spec, X)
        except HookError as exc:
            raise FitError(f"Can't evaluate the fit on data: {exc}") from exc
    return pd.Series(list(values), index=X.index, dtype="float64")


def metrics(actual: pd.Series, predicted: pd.Series) -> dict[str, float]:
    """Return a prediction's ``rmse``, ``mae`` and ``mape`` (over non-zero values)."""
    error = predicted - actual
    nonzero = actual != 0
    found = {
        "rmse": math.sqrt(float((error**2).mean())),
        "mae": float(error.abs().mean()),
    }
    if nonzero.any():
        found["mape"] = float((error[nonzero] / actual[nonzero]).abs().mean())
    return found


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, float | int | str | bool) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)
