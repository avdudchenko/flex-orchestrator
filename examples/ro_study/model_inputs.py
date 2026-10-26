"""Set a model variable from stored data: cost the schedule the plant ran.

``model_inputs`` fixes the RO's permeate flow to what the stored operating data
recorded. The model runs 8-10 July and the data is June's, so ``offset: -30D``
reads 8-10 June. The ``as_operated`` case costs that schedule; the
``optimized`` case lets the model pick its own.

    python examples/ro_study/model_inputs.py
"""

from common import EXPORT, new_study, report

AS_OPERATED = {
    "dataset": "ro_operation",
    "columns": ["permeate"],
    "rename": {"permeate": "plant.ro.permeate"},
    "offset": "-30D",
}

STUDY = {
    "base_model_spec": "base",
    "sweeps": [
        {
            "type": "cases",
            "cases": [
                {"name": "optimized"},
                {"name": "as_operated", "model_inputs": [AS_OPERATED]},
            ],
        }
    ],
    "export": EXPORT,
    "output_name": "{case}",
}

if __name__ == "__main__":
    with new_study("ro_model_inputs") as orch:
        orch.execute_study(STUDY)
        report(orch)
        [source] = orch.result("as_operated").model_inputs
        print(f"as_operated read {source['paths']} from {source['window']}")
