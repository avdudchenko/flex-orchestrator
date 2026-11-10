"""The JSON Schemas published in ``docs/schemas/``.

``python -m flexorchestrator.schemas [DIR]`` writes them (``docs/schemas`` by
default); a test checks the published files are current.

- ``study.schema.json``, ``spec.schema.json``, ``dataset.schema.json``,
  ``result.schema.json``, ``fit.schema.json``: the ``flexorch-export/1``
  files;
- ``study_config.schema.json``: ``StudyConfig``;
- ``fit_params.schema.json``: the ``fit_surrogate`` parameters.
"""

import json
import sys
from pathlib import Path
from typing import Any

from flexorchestrator.archive.models import json_schemas
from flexorchestrator.fitting.models import FitParams
from flexorchestrator.study.config import StudyConfig

DEFAULT_DIR = Path("docs") / "schemas"
"""Path: Where :func:`write` puts the schemas by default, relative to the repo."""


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
