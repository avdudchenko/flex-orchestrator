"""The flex-pse regression hooks for ``fit_surrogate`` (PLAN.md D19).

:func:`get_regressor` resolves a ``SurrogateType`` name to flex-pse's
regressor class (``multilinear``: ``LinearRegressor``, ``constant_intensity``:
``ConstantIntensityRegressor``). Neither 0.2 regressor has ``predict``, so
:func:`predict_surrogate` evaluates their surrogate specs for holdout metrics
and fitted series.
"""

from importlib.metadata import PackageNotFoundError, version
from typing import Any

import pandas as pd
from flexparameterize.regression import get_regressor as _get_regressor

from flex_orchestrator.errors import FitError


def get_regressor(name: str) -> type:
    """Return the flex-pse regressor class for a ``SurrogateType`` name.

    Raises:
        NotImplementedError: If the installed flex-pse reserves the type but
            doesn't fit it yet; the message names the installed version.
        FitError: If ``name`` isn't a surrogate type.

    Example:
        >>> get_regressor("multilinear").__name__  # doctest: +SKIP
        'LinearRegressor'
    """
    try:
        return _get_regressor(name)
    except NotImplementedError as exc:
        raise NotImplementedError(
            f"{exc} (installed flex-pse {_flexpse_version()})"
        ) from None
    except Exception as exc:
        raise FitError(str(exc)) from exc


def predict_surrogate(spec: dict[str, Any], X: pd.DataFrame) -> list[float]:
    """Evaluate a ``multilinear`` or ``constant_intensity`` surrogate spec on ``X``.

    ``multilinear`` is its intercept plus each coefficient times its term, a
    ``*``-separated product of input columns; ``constant_intensity`` is its
    coefficient times the one input. Values are in the units the spec was
    fitted in, which are ``X``'s.

    Raises:
        FitError: For another surrogate type, or a term ``X`` doesn't have.
    """
    kind = spec.get("surrogate_type")
    coefficients = spec.get("data", {}).get("coefficients", {})
    if kind == "constant_intensity":
        [(_, coefficient)] = coefficients.items()
        if X.shape[1] != 1:
            raise FitError("A constant_intensity fit has one input column.")
        return [float(v) * coefficient for v in X.iloc[:, 0]]
    if kind != "multilinear":
        raise FitError(
            f"Can't evaluate a {kind!r} surrogate; only multilinear and "
            "constant_intensity are evaluated here."
        )
    total = pd.Series(float(coefficients.get("intercept", 0.0)), index=X.index)
    for term, coefficient in coefficients.items():
        if term == "intercept":
            continue
        product = pd.Series(1.0, index=X.index)
        for factor in term.split("*"):
            if factor not in X.columns:
                raise FitError(
                    f"The surrogate's term {term!r} needs column {factor!r}."
                )
            product = product * X[factor]
        total = total + coefficient * product
    return [float(v) for v in total]


def _flexpse_version() -> str:
    try:
        return version("flex-pse")
    except PackageNotFoundError:  # pragma: no cover - flex-pse is installed here
        return "unknown"
