# ro_tools: a sample plugin

A complete flex-orchestrator plugin (PLAN.md D18), with its own `pyproject.toml`
and tests. It provides:

- a command, `cost_table`, that tabulates one scalar of every stored result;
- named hooks, `ro_fake` (the fake build and solve, for tests);
- an orchestrator subclass, `ROOrchestrator`, that stores only `plant.*` and the
  operating cost by default (`default_export_selection`), refuses results tagged
  `scratch_*`, and stamps every stored item with the plugin's version
  (`before_store`).

```
pip install -e examples/plugins/ro_tools
FLEXORCH_PLUGINS=ro_tools flex-orch cmd cost_table --study my_study
```

Installing it isn't enough: it loads only when `FLEXORCH_PLUGINS` lists it or
it's passed explicitly (`create_orchestrator(plugins=["ro_tools"])`,
`flex-orch --plugin ro_tools`). Its tests run with
`pytest examples/plugins/ro_tools/tests` once it's installed.
