"""Size equipment across a multi-year study, from a study config.

The results go to a folder on disk, with no Acquirium.

    python examples/design_study/example.py
"""

import json
import os
import shutil
from pathlib import Path

from flexorchestrator import Orchestrator
from flexorchestrator.backend import FileBackend

HERE = Path(__file__).parent
FOLDER = HERE / "on_disk"

if __name__ == "__main__":
    os.chdir(HERE)  # the config names sun_july.json relative to here
    shutil.rmtree(FOLDER, ignore_errors=True)  # start afresh each time
    with Orchestrator(FileBackend(FOLDER)) as orch:
        orch.create_study("sizing")
        orch.store_model_spec("plant", json.loads(Path("flowsheet.json").read_text()))
        run = orch.execute_study("study.yaml")
        print(run.counts())
        for task in run.tasks:
            summary = orch.design_results(task.tag)
            sizes = {
                name: f"{capacity.size:.1f} {capacity.units}"
                for name, capacity in summary.capacities.items()
            }
            print(f"{task.tag}: {sizes}, {summary.annualized_cost:,.0f} per year")

        july = orch.model_results("solar_600")["horizon[2030,7].battery.charge"]
        print(f"July 2030 battery charge starts {july.timestamps[0]}")
