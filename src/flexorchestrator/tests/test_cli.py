"""The flex-orch CLI, on a memory orchestrator.

``_orchestrator`` is replaced, so these don't start Acquirium;
``test_examples.py`` runs the real command against a local one.
"""

import json

import pytest
from pydantic import BaseModel

from flexorchestrator import cli
from flexorchestrator.ext import command
from flexorchestrator.testing import memory_orchestrator

pytestmark = pytest.mark.unit


class Options(BaseModel):
    count: int
    scale: float = 1.0
    name: str | None = None
    tags: list[str] = []
    loud: bool = False
    extra: dict = {}


@command(name="echo")
def echo(orch, params: Options, progress) -> dict:
    """Echo the parameters back."""
    return params.model_dump()


@pytest.fixture
def orch(monkeypatch):
    orch = memory_orchestrator(study=None)
    orch.register_command(echo)
    monkeypatch.setattr(cli, "_orchestrator", lambda args: orch)
    return orch


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_cmd_options_come_from_the_params_model(orch, capsys):
    code, out, _ = run(
        capsys,
        "cmd",
        "echo",
        "--count",
        "3",
        "--scale",
        "0.5",
        "--tags",
        "a",
        "b",
        "--loud",
        "--extra",
        '{"k": [1]}',
    )
    assert code == 0
    assert out == {
        "count": 3,
        "scale": 0.5,
        "name": None,
        "tags": ["a", "b"],
        "loud": True,
        "extra": {"k": [1]},
    }
    code, out, _ = run(
        capsys, "cmd", "echo", "--count", "1", "--no-loud", "--name", "x"
    )
    assert out["loud"] is False and out["name"] == "x"


def test_cmd_errors(orch, capsys):
    with pytest.raises(SystemExit):
        cli.main(["cmd", "echo"])  # --count is required
    with pytest.raises(SystemExit):
        cli.main(["cmd", "echo", "--count", "1", "--extra", "{nope"])
    code, out, err = run(capsys, "cmd", "nope")
    assert code == 1 and "CommandError" in err and out is None
    with pytest.raises(SystemExit):
        cli.main(["commands", "--surprise"])


def test_commands_lists_their_sources(orch, capsys):
    _, out, _ = run(capsys, "commands")
    assert {c["name"]: c["source"] for c in out} == {
        "echo": "user",
        "execute_study": "flex-orchestrator",
        "fit_surrogate": "flex-orchestrator",
    }
    assert out[0]["description"] == "Echo the parameters back."


def test_run_a_study_config(orch, capsys, tmp_path):
    orch.create_study("s")
    orch.store_model_spec("base", {"value": 1})
    config = tmp_path / "study.yaml"
    config.write_text("base_model_spec: base\n")
    _, out, _ = run(capsys, "run", str(config), "--study", "s", "--dry-run")
    assert out["dry_run"] is True and out["tasks"][0]["tag"] == "base"
    code, _, err = run(capsys, "run", str(config))
    assert code == 0  # the study selected above
    orch._study = None
    code, _, err = run(capsys, "run", str(config))
    assert code == 1 and "Name the study" in err
    config.write_text("study: t\nbase_model_spec: base\n")
    seen = {}
    orch.execute_study = lambda cfg, **kwargs: seen.update(kwargs) or {"ok": 1}
    code, out, _ = run(
        capsys, "run", str(config), "--workers", "2", "--ray-address", "ray://h:1"
    )
    assert code == 0 and seen["max_workers"] == 2
    assert seen["ray_address"] == "ray://h:1"


def test_cmd_selects_a_study_first(orch, capsys):
    orch.create_study("a")
    orch._study = None
    _, out, _ = run(capsys, "cmd", "echo", "--study", "a", "--count", "1")
    assert out["count"] == 1 and orch.study == "a"


def test_export_and_import(orch, capsys, tmp_path):
    orch.create_study("s")
    orch.store_model_spec("base", {"value": 1})
    _, out, _ = run(capsys, "export", "s", str(tmp_path / "e"), "--parts", "specs")
    assert out["counts"]["specs"] == 1
    _, out, _ = run(capsys, "import", str(tmp_path / "e"), "--name", "s2")
    assert out == {
        "study": "s2",
        "counts": {"specs": 1, "data": 0, "fits": 0, "results": 0},
        "skipped": [],
    }
    _, out, _ = run(
        capsys, "import", str(tmp_path / "e"), "--name", "s2", "--on-existing", "skip"
    )
    assert out["skipped"] == ["model_spec:base"]


def test_the_real_orchestrator_factory(monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return "orch"

    monkeypatch.setattr("flexorchestrator.ext.create_orchestrator", fake)
    args = cli._parser().parse_args(["--data-dir", "d", "--plugin", "p", "commands"])
    assert cli._orchestrator(args) == "orch"
    assert seen == {
        "data_dir": "d",
        "db": None,
        "acquirium_url": None,
        "plugins": ["p"],
    }


def test_files_runs_on_a_folder_with_no_acquirium(tmp_path, capsys):
    from flexorchestrator import Orchestrator
    from flexorchestrator.backend import FileBackend

    with Orchestrator(FileBackend(tmp_path / "a"), environ={}) as orch:
        orch.create_study("s")
        orch.store_model_spec("base", {"value": 1.0})
    folder, copy = str(tmp_path / "e"), str(tmp_path / "b")
    assert run(capsys, "--files", str(tmp_path / "a"), "export", "s", folder)[0] == 0
    assert run(capsys, "--files", copy, "import", folder)[0] == 0
    with Orchestrator(FileBackend(copy), study="s", environ={}) as orch:
        assert orch.model_spec("base").spec == {"value": 1.0}
    with pytest.raises(SystemExit):
        cli.main(["--files", copy, "--data-dir", "d", "commands"])
    assert "takes no --data-dir" in capsys.readouterr().err


class Awkward(BaseModel):
    either: int | str = 0
    pairs: list[dict] = []


def test_awkward_fields_take_json():
    parser = cli.command_parser(
        command(name="awkward", params=Awkward)(lambda orch: None)
    )
    args = parser.parse_args(["--either", '"x"', "--pairs", '[{"a": 1}]'])
    assert vars(args) == {"either": "x", "pairs": [{"a": 1}]}
