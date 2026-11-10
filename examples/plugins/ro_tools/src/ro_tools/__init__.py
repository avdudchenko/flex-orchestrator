"""ro_tools: a sample flex-orchestrator plugin.

It adds one command, ``cost_table``, and an orchestrator subclass that stores
only the RO plant's values by default and stamps every stored item with the
plugin's version.

Enable it with ``FLEXORCH_PLUGINS=ro_tools`` (installing it isn't enough), then
``flex-orch cmd cost_table --study my_study``, or from Python::

    from flexorchestrator.ext import create_orchestrator
    orch = create_orchestrator(plugins=["ro_tools"])
    orch.run_command("cost_table")
"""

from pydantic import BaseModel

from flexorchestrator import Orchestrator
from flexorchestrator.ext import Plugin, command

__version__ = "0.1.0"


class CostTableParams(BaseModel):
    """Which value to tabulate."""

    key: str = "costing.aggregate_operating_cost"
    tags: list[str] | None = None


@command(name="cost_table", role="viewer")
def cost_table(orch, params: CostTableParams, progress) -> dict:
    """Tabulate one scalar of every stored result: {tag: value}."""
    return {
        result.tag: result.scalars().get(params.key)
        for result in orch.stored_model_results(params.tags)
    }


class ROOrchestrator(Orchestrator):
    """Stores the RO plant's values by default, and stamps the plugin's version."""

    def default_export_selection(self) -> dict:
        """Store the plant and its cost, not the whole model."""
        return {"include": ["plant.*", "costing.aggregate_operating_cost"]}

    def before_store(self, item) -> None:
        """Refuse scratch results; record the plugin's version on everything."""
        if item.kind == "result" and item.tag.startswith("scratch_"):
            raise ValueError(f"ro_tools doesn't store scratch results ({item.tag!r}).")
        item.metadata["ro_tools_version"] = __version__


plugin = Plugin(
    name="ro_tools",
    api_version=">=1,<2",
    commands=[cost_table],
    hooks={
        "ro_fake": {
            "build": "flexorchestrator.testing:fake_build",
            "solve": "flexorchestrator.testing:fake_solve",
        }
    },
    orchestrator_cls=ROOrchestrator,
)
