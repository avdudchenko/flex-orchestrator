# Studies from a config

A study config holds a whole study as data: the base model spec, the
surrogate fits, the sweeps, and what to store. It can be a dict, a YAML file
or a JSON file. Its model is {class}`~flexorchestrator.study.StudyConfig`, and
its JSON Schema is `docs/schemas/study_config.schema.json` (see
{doc}`../reference/schemas`).

```yaml
base_model_spec: base
fits:
  - tag: "ro_{name}"
    X: {dataset: ro_operation, columns: [permeate]}
    y: {dataset: ro_operation, columns: [power_electrical]}
    variants:
      - {name: average, regressor: constant_intensity}
      - {name: linear, regressor: multilinear}
sweeps:
  - type: surrogate
    unit: plant.ro
    surrogates: [ro_average, ro_linear]
export: {include: ["plant.ro.*", "costing.aggregate_operating_cost"]}
output_name: "{surrogate}"
```

Run it from Python, or from the command line:

```python
orch.execute_study("study.yaml")
```

```
flex-orch --data-dir .flexorch run study.yaml --study my_study [--dry-run]
```

## How a study runs

1. **Expand.** The sweeps' values are combined, as a `product` (the default)
   or with `zip`, and each combination's tag is rendered from `output_name`.
   A repeated or invalid tag fails here, before anything runs. `--dry-run`
   stops here and returns the plan.
2. **Check.** With `on_existing: error` (the default), a task whose tag is
   already stored raises before anything runs. `skip` leaves stored tasks
   alone, and `replace` solves them again.
3. **Fit.** The `fits` run first, so the sweeps can use the surrogates they
   store.
4. **Solve.** The tasks run one after another in this process, or on Ray when
   `execution.max_workers` is above 1 (see {doc}`parallel`). The stored
   results are the same either way.

Each call is recorded in the `flexorch::commands` ledger, with its parameters
and summary.

## The sweep types

A sweep varies one thing:

| Type | Varies |
| --- | --- |
| `surrogate` | The surrogate a unit uses. |
| `variable` | A model variable, fixed to each value. Use it for anything that is a Pyomo `Var`. |
| `configuration_option` | An option the model is built with, such as a unit's construction option or a price. The path is any element's name followed by the option. |
| `spec_list` | The model spec itself. |
| `model_inputs` | The stored data that model variables are set from. |
| `cases` | Named, hand-picked combinations of the above. |

```python
{"type": "variable", "path": "plant.ro.energy_intensity",
 "values": [{"value": 2.5, "units": "kWh/m^3"}, {"value": 3.0, "units": "kWh/m^3"}]}

{"type": "configuration_option", "path": "plant.feed.max_withdrawal",
 "values": [{"value": 160, "units": "m^3/hr"}, {"value": 200, "units": "m^3/hr"}]}

{"type": "cases", "cases": [
    {"name": "linear_tight_feed",
     "surrogates": {"plant.ro": "linear_surrogate"},
     "configuration_options": {"plant.feed.max_withdrawal.value": 160}},
    {"name": "aged_membranes",
     "variables": {"plant.ro.energy_intensity": {"value": 3.5, "units": "kWh/m^3"}}},
]}
```

The [`examples/ro_study/`](https://github.com/flex-pse/flex-orchestrator/tree/main/examples/ro_study)
folder has one script per sweep type.

## Model inputs

`model_inputs`, outside the sweeps, sets model variables from stored data in
every task. The data can be a dataset, a stored result, or Acquirium streams,
selected the same way as a fit's `X` and `y`. For example, a model can be
costed against the schedule a plant actually ran.

## Design studies

A `design:` section runs each task as a design study. The model spec is run
over every planning year and representative month, and it sizes shared
capacities such as a battery or solar. Leave the section out for an ordinary
study. See
[`examples/design_study/`](https://github.com/flex-pse/flex-orchestrator/tree/main/examples/design_study).

## Hooks are plain data

A config names its hooks as import strings (`"package.module:function"`) or
registered names, never as callables. That keeps a config plain data, so a Ray
worker or a host application's API can carry it. See {doc}`extending`.
