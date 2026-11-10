"""Prepare selected data for a regressor: align, resample, gaps.

X and y must share one index, and later time-series regressors need it
regular and gap-free, so the selectors' frames are joined on their
timestamps, optionally resampled, and their gaps dealt with. Every step is
recorded with the fit, as is a sha256 of the prepared data
(:func:`data_hash`): the same data gives the same hash on every run.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from flexorchestrator.errors import FitError
from flexorchestrator.fitting.models import Prep
from flexorchestrator.fitting.selectors import Selected


@dataclass
class Prepared:
    """Data ready to fit.

    Attributes:
        X: The input columns.
        y: The one output column.
        units: ``{column: Pyomo unit or None}`` for every column.
        steps: What was done, in order.
        provenance: Each selector's provenance, ``{"X": [...], "y": [...]}``.
        data_hash: See :func:`data_hash`.
    """

    X: pd.DataFrame
    y: pd.DataFrame
    units: dict[str, str | None]
    steps: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    data_hash: str = ""


def prepare(xs: list[Selected], ys: list[Selected], prep: Prep) -> Prepared:
    """Join the selectors' data and prepare it as ``prep`` says.

    Raises:
        FitError: If two selectors give the same column, ``y`` isn't one
            column, there are too many rows, or gaps remain that ``prep``
            doesn't allow.
    """
    x_columns = [c for part in xs for c in part.frame.columns]
    y_columns = [c for part in ys for c in part.frame.columns]
    every = x_columns + y_columns
    repeated = sorted({c for c in every if every.count(c) > 1})
    if repeated:
        raise FitError(
            f"Columns {repeated} come from more than one selector; rename them."
        )
    if len(y_columns) != 1:
        raise FitError(f"y must give one column; it gives {y_columns}.")
    units = {c: u for part in xs + ys for c, u in part.units.items()}
    data = pd.concat([p.frame for p in xs + ys], axis=1, join=prep.align).sort_index()
    steps: list[dict[str, Any]] = [
        {"step": "align", "how": prep.align, "rows": len(data)}
    ]
    if len(data) > prep.max_rows:
        raise FitError(
            f"The selected data has {len(data)} rows, more than max_rows="
            f"{prep.max_rows}; narrow the window or resample."
        )
    if prep.resample is not None:
        data = data.resample(prep.resample.freq).agg(prep.resample.agg)
        steps.append(
            {
                "step": "resample",
                "freq": prep.resample.freq,
                "agg": prep.resample.agg,
                "rows": len(data),
            }
        )
    data = _gaps(data, prep, steps)
    if data.empty:
        raise FitError("No rows are left to fit after preparing the data.")
    prepared = Prepared(
        X=data[x_columns],
        y=data[y_columns],
        units=units,
        steps=steps,
        provenance={
            "X": [p.provenance for p in xs],
            "y": [p.provenance for p in ys],
        },
    )
    prepared.data_hash = data_hash(prepared.X, prepared.y)
    return prepared


def _gaps(data: pd.DataFrame, prep: Prep, steps: list[dict[str, Any]]) -> pd.DataFrame:
    missing = int(data.isna().any(axis=1).sum())
    if not missing:
        return data
    if prep.gaps == "error":
        first = data.index[data.isna().any(axis=1)][0]
        raise FitError(
            f"{missing} of {len(data)} rows have missing values (the first at "
            f"{first.isoformat()}). Set prep.gaps to 'drop', 'interpolate' or "
            "'longest_segment'."
        )
    if prep.gaps == "drop":
        data = data.dropna()
    elif prep.gaps == "interpolate":
        data = data.interpolate(method="time", limit=prep.limit, limit_area="inside")
        left = int(data.isna().any(axis=1).sum())
        if left:
            raise FitError(
                f"{left} rows still have missing values after interpolating "
                f"(limit={prep.limit}, and the ends aren't extrapolated). Raise "
                "the limit, or use 'drop' or 'longest_segment'."
            )
    else:
        complete = ~data.isna().any(axis=1)
        segment = (complete != complete.shift()).cumsum()[complete]
        if segment.empty:
            raise FitError("Every row has a missing value; there is no segment.")
        longest = segment.value_counts().idxmax()
        data = data[complete & (segment.reindex(data.index) == longest)]
    steps.append(
        {"step": "gaps", "how": prep.gaps, "rows_with_gaps": missing, "rows": len(data)}
    )
    return data


def data_hash(X: pd.DataFrame, y: pd.DataFrame) -> str:
    """Return the sha256 of the prepared data, the same for the same data.

    Covers the timestamps, the column names and every value, exactly (floats
    are written in their shortest round-tripping form).
    """
    payload = {
        "time": [when.isoformat() for when in X.index],
        "X": {c: [float(v) for v in X[c].tolist()] for c in X.columns},
        "y": {c: [float(v) for v in y[c].tolist()] for c in y.columns},
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def split_holdout(
    X: pd.DataFrame, y: pd.DataFrame, holdout: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split off the last ``holdout`` (a pandas offset) as test rows.

    Returns:
        ``(X_train, y_train, X_test, y_test)``.

    Raises:
        FitError: If the offset doesn't parse, or either side would be empty.
    """
    try:
        cutoff = X.index.max() - pd.tseries.frequencies.to_offset(holdout)
    except ValueError as exc:
        raise FitError(f"Can't read holdout {holdout!r}: {exc}") from exc
    train = X.index <= cutoff
    if train.all() or not train.any():
        raise FitError(
            f"Holding out {holdout!r} leaves no rows to "
            f"{'test' if train.all() else 'train'} on."
        )
    return X[train], y[train], X[~train], y[~train]
