"""Commands, progress and the command ledger."""

import pytest
from pydantic import BaseModel

from flexorchestrator.errors import CommandCancelled, CommandError
from flexorchestrator.ext import COMMANDS_LEDGER, CommandRegistry, Progress, command
from flexorchestrator.ext.commands import NoParams, to_json
from flexorchestrator.testing import memory_orchestrator, run_command_in_test

pytestmark = pytest.mark.unit


class Sweep(BaseModel):
    prices: list[float]
    base_tag: str = "base"


@command(name="price_sweep", long_running=True, role="owner")
def price_sweep(orch, params: Sweep, progress, scale=1.0) -> dict:
    """Pretend to sweep prices.

    More words.
    """
    progress.update(total=len(params.prices))
    for _ in params.prices:
        progress.check()
        progress.advance()
    return {"stored": [p * scale for p in params.prices], "study": orch.study}


@command()
def ping(orch) -> str:
    return "pong"


@command()
def explode(orch) -> None:
    raise RuntimeError("boom")


def ledger(orch):
    if not orch.backend.has_study(COMMANDS_LEDGER):
        return []
    return orch.backend.runs(COMMANDS_LEDGER)


def test_command_infers_its_params_and_description():
    assert price_sweep.name == "price_sweep"
    assert price_sweep.params_model is Sweep
    assert price_sweep.description == "Pretend to sweep prices."
    assert (price_sweep.role, price_sweep.long_running) == ("owner", True)
    assert ping.params_model is NoParams
    assert ping.description == ""


def test_command_needs_a_pydantic_params_annotation():
    with pytest.raises(CommandError, match="pydantic model"):

        @command()
        def bad(orch, params: dict, progress): ...

    @command(params=Sweep, description="given")
    def explicit(orch, params, progress): ...

    assert (explicit.params_model, explicit.description) == (Sweep, "given")


def test_validate_merges_fields_and_reports_every_problem():
    assert price_sweep.validate({"prices": [1]}, base_tag="x").base_tag == "x"
    model = Sweep(prices=[2])
    assert price_sweep.validate(model) is model
    assert price_sweep.validate(model, base_tag="y").base_tag == "y"
    with pytest.raises(CommandError, match=r"prices: .*required"):
        price_sweep.validate({})


def test_run_command_records_a_succeeded_run():
    orch = memory_orchestrator()
    orch.register_command(price_sweep)
    seen = []
    orch.on("command_finished", lambda **kw: seen.append(kw))
    progress = Progress()
    result = orch.run_command(
        "price_sweep", {"prices": [1, 2]}, progress=progress, runtime={"scale": 2.0}
    )
    assert result == {"stored": [2.0, 4.0], "study": "test"}
    assert progress.snapshot() == {
        "done": 2,
        "total": 2,
        "message": None,
        "cancelled": False,
    }
    [run] = ledger(orch)
    assert run.status == "succeeded"
    assert run.metadata["flexorch_kind"] == "command"
    assert run.metadata["flexorch_tag"] == "price_sweep"
    assert run.metadata["flexorch_study"] == "test"
    assert run.metadata["flexorch_runtime"] == {"scale": "2.0"}
    records = {
        r.label: r.value for r in orch.backend.records(COMMANDS_LEDGER, run.run_id)
    }
    assert records["params"] == {"prices": [1.0, 2.0], "base_tag": "base"}
    assert records["result"] == result
    assert seen == [{"name": "price_sweep", "status": "succeeded", "error": None}]


def test_a_command_that_raises_leaves_a_failed_run():
    orch = memory_orchestrator(study=None)
    orch.register_command(explode)
    seen = []
    orch.on("command_finished", lambda **kw: seen.append(kw))
    with pytest.raises(RuntimeError, match="boom"):
        orch.run_command("explode")
    [run] = ledger(orch)
    assert run.status == "failed"
    assert "flexorch_study" not in run.metadata
    assert seen[0]["status"] == "failed"
    assert seen[0]["error"] == "RuntimeError: boom"


def test_unknown_commands_and_bad_params_are_command_errors():
    orch = memory_orchestrator()
    with pytest.raises(CommandError, match="no command 'nope'"):
        orch.run_command("nope")
    with pytest.raises(CommandError, match="Invalid parameters"):
        orch.run_command("fit_surrogate", tag="x")
    assert ledger(orch) == []


def test_cancelling_stops_a_command():
    progress = Progress()
    progress.cancel()
    assert progress.cancelled
    with pytest.raises(CommandCancelled):
        run_command_in_test(price_sweep, prices=[1], progress=progress)


def test_progress_reports_to_its_callback():
    reports = []
    progress = Progress(reports.append)
    progress.update(done=1, total=3, message="a")
    progress.advance(2, message="b")
    assert [r["done"] for r in reports] == [1, 3]
    assert reports[-1]["message"] == "b"
    progress.check()  # not cancelled


def test_registry_refuses_collisions_and_non_commands():
    registry = CommandRegistry()
    registry.register(ping, source="plugin 'a'")
    with pytest.raises(CommandError, match="collides with the one from plugin 'a'"):
        registry.register(ping)
    with pytest.raises(CommandError, match="not a Command"):
        registry.register(lambda orch: None)
    assert "ping" in registry and registry.names() == ["ping"]
    assert registry.source("ping") == "plugin 'a'"
    assert [c.name for c in registry] == ["ping"]


def test_builtin_names_are_taken():
    orch = memory_orchestrator()
    assert orch.commands.names() == ["execute_study", "fit_surrogate"]
    assert orch.commands.source("execute_study") == "flex-orchestrator"
    with pytest.raises(CommandError, match="collides"):
        orch.register_command(command(name="execute_study")(lambda orch: None))


def test_run_command_in_test_registers_and_runs():
    assert run_command_in_test(ping) == "pong"
    orch = memory_orchestrator()
    assert run_command_in_test(price_sweep, orch=orch, prices=[3]) == {
        "stored": [3.0],
        "study": "test",
    }
    assert run_command_in_test("price_sweep", orch=orch, prices=[1])["stored"] == [1.0]


def test_to_json_handles_models_and_objects():
    from datetime import UTC, datetime

    when = datetime(2025, 1, 1, tzinfo=UTC)
    assert to_json(Sweep(prices=[1])) == {"prices": [1.0], "base_tag": "base"}
    assert to_json({"m": Sweep(prices=[]), "t": when, "o": object}) == {
        "m": {"prices": [], "base_tag": "base"},
        "t": "2025-01-01T00:00:00+00:00",
        "o": "<class 'object'>",
    }
