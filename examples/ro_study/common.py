"""What every study script here shares: the ro_surrogates model, data and fits.

``new_study(name)`` opens a fresh local store, starts the study, and stores
the operating data and the emitted base spec, so each script only has its
study config to show.
"""

import sys
from pathlib import Path

import flexops as fo

from flex_orchestrator import Orchestrator

HERE = Path(__file__).parent
MODEL = HERE.parent / "ro_surrogates"  # the model, its tariff file and its data
sys.path.insert(0, str(MODEL))
from example import (  # noqa: E402, F401
    KEEP,
    UNITS,
    compare,
    fresh_store,
    operating_data,
    ro_model,
)

# Fits the two surrogates from the stored data, as the "fits" of a study.
FITS = [
    {
        "tag": "{name}",
        "X": {"dataset": "ro_operation", "columns": ["permeate"]},
        "y": {"dataset": "ro_operation", "columns": ["power_electrical"]},
        "variants": [
            {"name": "fixed_intensity", "regressor": "constant_intensity"},
            {"name": "linear_surrogate", "regressor": "multilinear"},
        ],
    }
]

EXPORT = {"include": KEEP}


def new_study(name: str, orch: Orchestrator | None = None) -> Orchestrator:
    """Start a fresh study holding the data and base spec.

    In ``orch``, or, if it's ``None``, in a fresh local store (see
    ``fresh_store``) opened here.
    """
    orch = orch or Orchestrator(data_dir=fresh_store(HERE))
    if name in orch.studies():
        orch.delete_study(name)
    orch.create_study(name)
    orch.store_data("ro_operation", operating_data(), units=UNITS)
    orch.store_model_spec(
        "base", fo.emit_model(ro_model(), relative_to=MODEL), base_dir=MODEL
    )
    return orch


def report(orch: Orchestrator) -> None:
    """Print each stored result's operating cost."""
    for result in orch.stored_model_results():
        cost = result.scalars()["costing.aggregate_operating_cost"]
        print(f"{result.tag}: ${cost:,.2f}")
