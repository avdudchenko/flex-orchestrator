"""Every example runs from start to finish (PLAN.md §7, integration tier).

Each runs in its own process with its own Acquirium, so it doesn't share the
session's server.

- The step-by-step RO example, the five study-config ones, and the surrogate
  sweep stored on disk (no Acquirium).
- The sample plugin (``examples/plugins/ro_tools``), installed with pip into
  a temporary folder: it loads only when ``FLEXORCH_PLUGINS`` lists it, its
  command gives the same result from Python and from ``flex-orch``, its own
  tests pass, and the CLI exports and imports the study it ran on.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1800)]

EXAMPLES = Path(__file__).parents[3] / "examples"


def run(command: list[str], env: dict | None = None, cwd: Path | None = None) -> str:
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        cwd=cwd,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    return done.stdout


def script(path: Path, *args: str) -> str:
    return run([sys.executable, str(path), *args])


@pytest.mark.needs_highs
def test_ro_surrogates():
    pytest.importorskip("acquirium")
    pytest.importorskip("flexparameterize")
    out = script(EXAMPLES / "ro_surrogates" / "example.py")
    for tag in ("fixed_intensity", "linear_surrogate"):
        assert f"\n{tag}: $" in out


STUDY_TAGS = {
    "sweep_surrogates": ["fixed_intensity", "linear_surrogate"],
    "sweep_variables": ["intensity_2.5", "intensity_3.0", "intensity_3.5"],
    "sweep_configuration": ["withdrawal_160", "withdrawal_180", "withdrawal_200"],
    "sweep_nested": [
        f"{s}_{w}"
        for s in ("fixed_intensity", "linear_surrogate")
        for w in (160, 180, 200)
    ],
    "sweep_cases": ["linear_tight_feed", "fixed_loose_feed", "aged_membranes"],
}


@pytest.fixture
def export_folder():
    """``sweep_nested``'s export folder, removed before and after the test."""
    folder = EXAMPLES / "ro_study" / "export"
    shutil.rmtree(folder, ignore_errors=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.needs_highs
@pytest.mark.parametrize("name", sorted(STUDY_TAGS))
def test_ro_study(name, export_folder):
    pytest.importorskip("acquirium")
    pytest.importorskip("flexparameterize")
    out = "\n" + script(EXAMPLES / "ro_study" / f"{name}.py")
    for tag in STUDY_TAGS[name]:
        assert f"\n{tag}: $" in out
    if name == "sweep_nested":
        # The full model is stored, exported and read back unchanged.
        assert "'results': 6}" in out
        assert (export_folder / "ro_nested_sweep" / "checksums.sha256").is_file()
        for tag in STUDY_TAGS[name]:
            [line] = [x for x in out.splitlines() if x.startswith(f"{tag}: all ")]
            assert int(line.split()[2]) > 400  # every variable, not a selection


@pytest.mark.needs_highs
def test_ro_study_on_disk():
    pytest.importorskip("flexparameterize")
    out = "\n" + script(EXAMPLES / "ro_study" / "store_on_disk.py")
    assert "Read back from" in out
    for tag in STUDY_TAGS["sweep_surrogates"]:
        assert f"\n{tag}: $" in out
    shutil.rmtree(EXAMPLES / "ro_study" / "on_disk")


POPULATE = """
import json, sys
from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.ext import create_orchestrator

def cost(value):
    return ModelData([ExportedSeries(
        key="costing.aggregate_operating_cost", kind="scalar",
        component="costing.aggregate_operating_cost", index=[], values=[value],
        timestamps=None, unit_pyomo="USD", unit_qudt=None)])

with create_orchestrator(data_dir=sys.argv[1]) as orch:
    if "cost_table" not in orch.commands:
        print(json.dumps({"loaded": False}))
        sys.exit(0)
    orch.create_study("plugin_demo")
    orch.store_model_data("a", cost(1.25))
    orch.store_model_data("b", cost(2.5))
    print(json.dumps({
        "loaded": True,
        "class": type(orch).__name__,
        "stamp": orch.result("a").metadata.get("ro_tools_version"),
        "table": orch.run_command("cost_table"),
    }))
"""


def test_the_sample_plugin(tmp_path):
    pytest.importorskip("acquirium")
    source = tmp_path / "ro_tools"
    shutil.copytree(EXAMPLES / "plugins" / "ro_tools", source)
    site = tmp_path / "site"
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(site),
            str(source),
            "-q",
        ]
    )
    base = {k: v for k, v in os.environ.items() if k != "FLEXORCH_PLUGINS"}
    base["PYTHONPATH"] = os.pathsep.join(
        [str(site), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    enabled = {**base, "FLEXORCH_PLUGINS": "ro_tools"}
    data = str(tmp_path / "store")
    populate = [sys.executable, "-c", POPULATE, data]

    # Installed but not in the allowlist: not loaded.
    assert json.loads(run(populate, env=base)) == {"loaded": False}

    found = json.loads(run(populate, env=enabled))
    assert found == {
        "loaded": True,
        "class": "ROOrchestrator",
        "stamp": "0.1.0",
        "table": {"a": 1.25, "b": 2.5},
    }
    cli = [sys.executable, "-m", "flex_orchestrator.cli", "--data-dir", data]
    from_cli = run([*cli, "cmd", "cost_table", "--study", "plugin_demo"], env=enabled)
    assert json.loads(from_cli) == found["table"]
    exported = json.loads(
        run([*cli, "export", "plugin_demo", str(tmp_path / "export")], env=enabled)
    )
    assert exported["counts"]["results"] == 2
    imported = json.loads(
        run([*cli, "import", str(tmp_path / "export"), "--name", "copy"], env=enabled)
    )
    assert imported["counts"]["results"] == 2

    # The plugin's own tests, from the installed copy.
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(source / "tests"),
        ],
        env=base,
        cwd=Path(__file__).parents[3],
    )
