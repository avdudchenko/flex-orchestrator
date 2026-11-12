# Hooks, commands and plugins

The orchestrator manages storage and runs. How a model is built and solved,
and what extra operations exist, can be swapped out.

## Hooks

A run is `build(spec, **overrides)` followed by `solve(model, **options)`.
Both steps are **hooks**: plain callables that follow
{class}`~flexorchestrator.hooks.BuildFn` and {class}`~flexorchestrator.hooks.SolveFn`.
The defaults are built on flex-pse ({data}`~flexorchestrator.hooks.DEFAULT_HOOKS`),
but the orchestrator itself never imports flex-pse.

The other hooks are optional:

| Hook | Does |
| --- | --- |
| `apply_surrogate` | Puts a surrogate into a spec, before building. |
| `swap_surrogate` | Swaps the surrogate of a unit on a live model. |
| `configure` | Sets a configuration option in a spec. |
| `time_sets` | Names a built model's time Sets, for the exporter. |
| `time_grid` | Gives a spec's or a built model's time points. |

A hook can be given three ways:

1. a callable;
2. a name registered with
   {meth}`HookRegistry.register <flexorchestrator.hooks.HookRegistry.register>`,
   such as `"my_model"`;
3. an import string, `"package.module:function"`.

Ray workers and study configs need names or import strings, because they
can't carry a callable.

```python
orch.solve_and_store("r1", orch.build("base", build="my_pkg.models:build"))
```

## Commands

A command is a named, validated operation on an orchestrator. One definition
can be called from Python, from the command line, and from a host
application's API, all with the same JSON parameters.

```python
from pydantic import BaseModel
from flexorchestrator.ext import command


class CostTableParams(BaseModel):
    key: str = "costing.aggregate_operating_cost"


@command(name="cost_table")
def cost_table(orch, params: CostTableParams, progress) -> dict:
    """Tabulate one scalar of every stored result."""
    return {r.tag: r.scalars().get(params.key) for r in orch.stored_model_results()}


orch.register_command(cost_table)
orch.run_command("cost_table", key="costing.aggregate_operating_cost")
```

```
flex-orch cmd cost_table --study my_study --key costing.aggregate_operating_cost
```

`execute_study` and `fit_surrogate` are built-in commands. Every call is
recorded in the `flexorch::commands` ledger, with its parameters and result.

## Lifecycle events

For a small reaction to what the orchestrator does, register a listener
instead of subclassing:

```python
@orch.on("result_stored")
def notify(item):
    print("stored", item.tag)
```

The events are `study_created`, `spec_stored`, `result_stored`,
`data_stored`, `fit_stored`, `job_finished` and `command_finished`. A listener
runs after the item is stored, so if it raises, nothing is undone.

## Subclassing the orchestrator

A subclass may override these stable methods: `build_hooks`,
`default_export_selection`, `before_store`, `after_store`, `on_task_result`
and `on_study_created`.

## Plugins

A {class}`~flexorchestrator.ext.Plugin` bundles commands, named hooks,
listeners and an orchestrator subclass. A package exposes one through the
`flexorchestrator.plugins` entry-point group:

```toml
[project.entry-points."flexorchestrator.plugins"]
ro_tools = "ro_tools:plugin"
```

Being installed isn't enough to load it. A plugin loads only when its name is
in `FLEXORCH_PLUGINS` (comma-separated), or when it is passed explicitly, so
each deployment enables its own set:

```python
from flexorchestrator.ext import create_orchestrator

orch = create_orchestrator(plugins=["ro_tools"])
```

```
FLEXORCH_PLUGINS=ro_tools flex-orch cmd cost_table --study my_study
flex-orch --plugin ro_tools cmd cost_table --study my_study
```

Loading checks that the plugin's `api_version` range includes the current
plugin API version. It also checks that no two plugins claim the same command
or hook name, and that only one provides an orchestrator subclass. See the
sample plugin in
[`examples/plugins/ro_tools/`](https://github.com/flex-pse/flex-orchestrator/tree/main/examples/plugins/ro_tools).
