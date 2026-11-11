# Quick start

This is trimmed from
[`examples/ro_surrogates/example.py`](https://github.com/flex-pse/flex-orchestrator/blob/main/examples/ro_surrogates/example.py).
That script defines `operating_data`, `ro_model`, `UNITS`, `KEEP` and the
fitted `linear` surrogate; here they are taken as given.

```python
import flexops as fo
from flexorchestrator import Orchestrator

with Orchestrator(data_dir=".flexorch") as orch:
    orch.create_study("ro_surrogates")

    # Measured data, with the unit of each column.
    orch.store_data("ro_operation", operating_data(), units=UNITS)

    # The model as a spec: plain JSON, plus the files it references.
    orch.store_model_spec("base", fo.emit_model(ro_model(), relative_to=HERE), base_dir=HERE)

    # A surrogate for the RO's power, and a spec that uses it.
    orch.store_surrogate_spec("ro_linear", linear.to_surrogate_spec())
    spec = orch.apply_surrogate("base", unit="plant.ro", surrogate="ro_linear")
    orch.store_model_spec("ro_linear", spec, parent="base")

    # Build, solve and store the result.
    orch.solve_and_store("linear_surrogate", orch.build("ro_linear"), include=KEEP)

    for result in orch.stored_model_results():
        print(result.tag, result.scalars()["costing.aggregate_operating_cost"])
```

What happens here:

1. `Orchestrator(data_dir=".flexorch")` starts a local Acquirium on DuckDB in
   `.flexorch/`, or attaches to one already running there. See
   {doc}`../explanation/connecting` for the other ways to connect.
2. `create_study` makes the study and selects it. Everything after this is
   stored in it.
3. Specs are stored as data, not as live models. A stored spec can be rebuilt
   in another process, on a Ray worker, or a year later.
4. `solve_and_store` stores the solved model's values with their units. You
   read them back with {meth}`~flexorchestrator.Orchestrator.stored_model_results`.

The same study can be written as a config and run with one call. See
{doc}`../explanation/studies`.

## The examples

The [`examples/`](https://github.com/flex-pse/flex-orchestrator/tree/main/examples)
folder has complete scripts. Each one runs in the test suite.

| Example | What it covers |
| --- | --- |
| `ro_surrogates/` | The script above, in full: fit two surrogates from data, solve with each, and compare them. |
| `ro_study/` | The same model as study configs, one script per kind of sweep, plus storing on disk and running on Ray. |
| `design_study/` | Sizing a battery and solar over several years with a `design:` section. |
| `plugins/ro_tools/` | A sample plugin: a command, named hooks and an orchestrator subclass. |
