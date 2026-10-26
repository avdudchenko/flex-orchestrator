"""The top-level package: lazy imports."""

import subprocess
import sys

import pytest

import flex_orchestrator

pytestmark = pytest.mark.unit


def test_export_model_data_is_importable_from_the_top():
    from flex_orchestrator.export import export_model_data

    assert flex_orchestrator.export_model_data is export_model_data


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = flex_orchestrator.nope


def test_importing_the_package_does_not_import_pyomo():
    code = "import sys, flex_orchestrator; print('pyomo' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


@pytest.mark.unit
def test_schemas_are_written(tmp_path):
    from flex_orchestrator import schemas

    written = schemas.write(tmp_path)
    assert {p.name for p in written} >= {
        "result.schema.json",
        "study_config.schema.json",
    }
    assert (tmp_path / "fit_params.schema.json").read_text().startswith("{")
