"""Sweep a surrogate: the ro_surrogates comparison, as one study config.

The built-in regression fits both surrogates from the stored data, then each
is swapped into the RO and the model is solved. The results match
``examples/ro_surrogates`` exactly.

    python examples/ro_study/sweep_surrogates.py
"""

from common import EXPORT, FITS, compare, new_study

STUDY = {
    "base_model_spec": "base",
    "fits": FITS,
    "sweeps": [
        {
            "type": "surrogate",
            "unit": "plant.ro",
            "surrogates": ["fixed_intensity", "linear_surrogate"],
        }
    ],
    "export": EXPORT,
    "output_name": "{surrogate}",
}

if __name__ == "__main__":
    with new_study("ro_surrogate_sweep") as orch:
        run = orch.execute_study(STUDY)
        for fit in run.fits[0].fits:
            print(f"{fit.tag}: {fit.coefficients}, r2 = {fit.metrics['r2']:.3f}")
        compare(orch)
