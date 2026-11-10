"""Fit surrogates from stored data: the ``fit_surrogate`` command.

Data comes from :class:`Selector` objects (a dataset, a stored result, or
general-store streams), is prepared (:class:`Prep`), and is fitted by a
regressor named by a flex-pse ``SurrogateType`` or an import string. Each fit
stores a surrogate spec in ``<s>::specs`` and a fit run, with its training-data
provenance, in ``<s>::fits``.

Example:
    >>> orch.fit_surrogate(  # doctest: +SKIP
    ...     tag="ro_{name}",
    ...     X={"dataset": "ro_operation", "columns": ["permeate"]},
    ...     y={"dataset": "ro_operation", "columns": ["power_electrical"]},
    ...     variants=[
    ...         {"name": "average", "regressor": "constant_intensity"},
    ...         {"name": "linear", "regressor": "multilinear"},
    ...     ],
    ... )
"""

from flexorchestrator.fitting.models import (
    FitParams,
    FitPlan,
    FitRecord,
    FitSummary,
    Prep,
    Resample,
    Selector,
    StreamName,
    Validation,
    Variant,
    Window,
)

__all__ = [
    "FitParams",
    "FitPlan",
    "FitRecord",
    "FitSummary",
    "Prep",
    "Resample",
    "Selector",
    "StreamName",
    "Validation",
    "Variant",
    "Window",
]
