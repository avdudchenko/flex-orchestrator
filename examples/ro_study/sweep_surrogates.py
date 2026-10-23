"""Sweep a surrogate: the ro_surrogates comparison, as one study config.

The built-in regression fits both surrogates from the stored data, then each
is swapped into the RO and the model is solved. The results match
``examples/ro_surrogates`` exactly.

    python examples/ro_study/sweep_surrogates.py
"""

from common import EXPORT, FITS, new_study, sec

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
    orch = new_study("ro_surrogate_sweep")
    run = orch.execute_study(STUDY)
    for fit in run.fits[0].fits:
        print(f"{fit.tag}: {fit.coefficients}, r2 = {fit.metrics['r2']:.3f}")

    print("Comparing estimated vs real power use due to surrogate simplifications")
    for result in orch.stored_model_results():
        cost = result.scalars()["costing.aggregate_operating_cost"]
        ro = result.to_pandas()
        model_kwh = ro["plant.ro.power_electrical"].sum()
        true_kwh = (sec(ro["plant.ro.permeate"]) * ro["plant.ro.permeate"]).sum()
        print(
            f"{result.tag}: ${cost:,.2f}, RO energy {model_kwh:,.0f} kWh "
            f"(true {true_kwh:,.0f} kWh)"
        )
    orch.close()
