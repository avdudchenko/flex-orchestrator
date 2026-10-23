"""Sweep a model variable: the RO's specific energy, as membranes age.

A ``variable`` sweep fixes a variable of the built model to each value; here
``plant.ro.energy_intensity``, which the RO's power is proportional to.

    python examples/ro_study/sweep_variables.py
"""

from common import EXPORT, new_study, report

STUDY = {
    "base_model_spec": "base",
    "sweeps": [
        {
            "type": "variable",
            "name": "intensity",
            "path": "plant.ro.energy_intensity",
            "values": [
                {"value": 2.5, "units": "kWh/m^3"},
                {"value": 3.0, "units": "kWh/m^3"},
                {"value": 3.5, "units": "kWh/m^3"},
            ],
        }
    ],
    "export": EXPORT,
    "output_name": "intensity_{intensity}",
}

if __name__ == "__main__":
    orch = new_study("ro_variable_sweep")
    orch.execute_study(STUDY)
    report(orch)
    orch.close()
