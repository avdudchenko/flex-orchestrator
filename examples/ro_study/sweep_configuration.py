"""Sweep a configuration option: how much water the feed can supply.

A ``configuration_option`` sweep changes how the model is built. The path is
an element's name and then the option, here the feed's ``max_withdrawal``
construction option; each value is set in a copy of the base spec, stored as
its own model spec (parent ``base``), then built and solved.

The option is a dict (``{"value", "units"}``), so a value can replace it
whole, as below, or a longer path can set one key:
``"path": "plant.feed.max_withdrawal.value", "values": [160, 180, 200]``.

    python examples/ro_study/sweep_configuration.py
"""

from common import EXPORT, new_study, report

STUDY = {
    "base_model_spec": "base",
    "sweeps": [
        {
            "type": "configuration_option",
            "name": "withdrawal",
            "path": "plant.feed.max_withdrawal",
            "values": [
                {"value": 160, "units": "m^3/hr"},
                {"value": 180, "units": "m^3/hr"},
                {"value": 200, "units": "m^3/hr"},
            ],
        }
    ],
    "export": EXPORT,
    "output_name": "withdrawal_{withdrawal}",
}

if __name__ == "__main__":
    orch = new_study("ro_configuration_sweep")
    orch.execute_study(STUDY)
    report(orch)
    orch.close()
