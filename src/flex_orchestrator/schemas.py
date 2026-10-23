"""The JSON Schemas published in ``docs/schemas/`` (PLAN.md §8).

``python -m flex_orchestrator.schemas [DIR]`` writes them (``docs/schemas`` by
default); a test checks the published files are current.

- ``study.schema.json``, ``spec.schema.json``, ``dataset.schema.json``,
  ``result.schema.json``, ``fit.schema.json``: the ``flexorch-export/1``
  files (D17);
- ``study_config.schema.json``: ``StudyConfig`` (M4);
- ``fit_params.schema.json``: the ``fit_surrogate`` parameters (D19).
"""

import json
import sys
from pathlib import Path
from typing import Any

from flex_orchestrator.archive.models import json_schemas
from flex_orchestrator.fitting.models import FitParams
from flex_orchestrator.study.config import StudyConfig

DEFAULT_DIR = Path("docs") / "schemas"


def all_schemas() -> dict[str, dict[str, Any]]:
    """Return ``{name: JSON Schema}`` for every published schema."""
    schemas = json_schemas()
    schemas["study_config"] = StudyConfig.model_json_schema()
    schemas["fit_params"] = FitParams.model_json_schema()
    return schemas


def render(schema: dict[str, Any]) -> str:
    """Return a schema as its published text."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write(directory: str | Path = DEFAULT_DIR) -> list[Path]:
    """Write ``<name>.schema.json`` for every schema; return the files."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in all_schemas().items():
        path = directory / f"{name}.schema.json"
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":  # pragma: no cover
    for path in write(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR):
        print(path)
