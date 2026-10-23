"""Cases: hand-picked combinations instead of every one.

Each case is named and sets any of ``surrogates`` (``{unit: tag}``),
``configuration_options`` and ``variables`` (``{path: value}``), and
``spec``. A ``cases`` sweep is one sweep like any other, so it can also be
nested with others, e.g. every case at every price.

    python examples/ro_study/sweep_cases.py
"""

from common import EXPORT, FITS, new_study, report

STUDY = {
    "base_model_spec": "base",
    "fits": FITS,
    "sweeps": [
        {
            "type": "cases",
            "cases": [
                {
                    "name": "linear_tight_feed",
                    "surrogates": {"plant.ro": "linear_surrogate"},
                    "configuration_options": {"plant.feed.max_withdrawal.value": 160},
                },
                {
                    "name": "fixed_loose_feed",
                    "surrogates": {"plant.ro": "fixed_intensity"},
                    "configuration_options": {"plant.feed.max_withdrawal.value": 200},
                },
                {
                    "name": "aged_membranes",
                    "variables": {
                        "plant.ro.energy_intensity": {"value": 3.5, "units": "kWh/m^3"}
                    },
                },
            ],
        }
    ],
    "export": EXPORT,
    "output_name": "{case}",
}

if __name__ == "__main__":
    orch = new_study("ro_cases")
    orch.execute_study(STUDY)
    report(orch)
    orch.close()
