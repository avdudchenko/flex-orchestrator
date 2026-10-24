"""The surrogate sweep stored in a folder on disk, with no Acquirium.

``FileBackend`` keeps every study in ``on_disk/``: each result is written as
it's stored, so nothing waits for an export and a crash loses only the task in
progress. The study runs, then a new orchestrator opens the same folder, as a
later script would, and reads the results back.

    python examples/ro_study/store_on_disk.py
"""

import shutil

from common import HERE, new_study, report
from sweep_surrogates import STUDY

from flex_orchestrator import Orchestrator
from flex_orchestrator.backend import FileBackend

FOLDER = HERE / "on_disk"

if __name__ == "__main__":
    shutil.rmtree(FOLDER, ignore_errors=True)  # start afresh each time
    with new_study("ro_on_disk", Orchestrator(FileBackend(FOLDER))) as orch:
        orch.execute_study(STUDY)

    with Orchestrator(FileBackend(FOLDER), study="ro_on_disk") as orch:
        print(f"Read back from {FOLDER}:")
        report(orch)
