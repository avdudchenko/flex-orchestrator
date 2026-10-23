"""A nested sweep: every surrogate at every feed limit, stored and exported in full.

With ``combine: product`` (the default) the sweeps nest: 2 surrogates x 3
withdrawal limits = 6 solves. ``combine: zip`` would instead step them
together, pairing the first surrogate with the first limit and so on.

The config sets no ``export``, so each result stores the full model: every
variable, parameter and expression. At the end the whole study (specs, data,
fits and results) is exported to ``export/ro_nested_sweep/``, a folder of JSON
files, and each result is read back from it with no database and checked
against the stored one.

    python examples/ro_study/sweep_nested.py
"""

from common import FITS, HERE, new_study, report

from flex_orchestrator.archive import load_result

STUDY = {
    "base_model_spec": "base",
    "fits": FITS,
    "sweeps": [
        {
            "type": "surrogate",
            "unit": "plant.ro",
            "surrogates": ["fixed_intensity", "linear_surrogate"],
        },
        {
            "type": "configuration_option",
            "name": "withdrawal",
            "path": "plant.feed.max_withdrawal.value",  # the number; units kept
            "values": [160, 180, 200],
        },
    ],
    "combine": "product",
    "output_name": "{surrogate}_{withdrawal}",
}

if __name__ == "__main__":
    orch = new_study("ro_nested_sweep")
    orch.execute_study(STUDY)
    report(orch)

    folder = HERE / "export" / "ro_nested_sweep"
    exported = orch.export_study(folder, overwrite=True)
    print(f"Exported to {folder}: {exported.counts}")
    for result in orch.stored_model_results():
        stored = {s.key: s for s in result.data()}
        on_disk = load_result(folder / "results" / f"{result.tag}.json")
        assert {s.key: s for s in on_disk.data()} == stored
        print(f"{result.tag}: all {len(stored)} values read back from disk unchanged")
    orch.close()
