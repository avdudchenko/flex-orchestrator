# RO studies from a config

The [`ro_surrogates`](../ro_surrogates/README.md) model and data again, now
run as study configs: each script declares one dict and hands it to
`orch.execute_study`. There's one script per kind of sweep, so each shows one
idea. [`common.py`](common.py) holds what they share: a fresh study with the
operating data and the emitted base spec (`new_study`), the surrogate fits
(`FITS`), what to export, and a cost printout.

| Script | Sweep | Tasks |
| --- | --- | --- |
| [`sweep_surrogates.py`](sweep_surrogates.py) | `surrogate`: the RO's power relation | fixed intensity, linear |
| [`sweep_variables.py`](sweep_variables.py) | `variable`: the RO's `energy_intensity` | 2.5, 3.0, 3.5 kWh/m³ |
| [`sweep_configuration.py`](sweep_configuration.py) | `configuration_option`: the feed's `max_withdrawal` | 160, 180, 200 m³/hr |
| [`sweep_nested.py`](sweep_nested.py) | surrogate × withdrawal (`combine: product`); stores the full model and exports the study | 2 × 3 |
| [`sweep_cases.py`](sweep_cases.py) | `cases`: chosen combinations | 3 named cases |
| [`store_on_disk.py`](store_on_disk.py) | `sweep_surrogates` stored in a folder on disk (`FileBackend`, no Acquirium), then read back by a new orchestrator | 2 |
| [`compare_ray.py`](compare_ray.py) | `sweep_nested` over a month, run in-process and then on Ray with half the cores, timed | 2 × 12 |

```
python examples/ro_study/sweep_surrogates.py
```

## The sweep types

```python
{"type": "surrogate", "unit": "plant.ro",
 "surrogates": ["fixed_intensity", "linear_surrogate"]}

{"type": "variable", "path": "plant.ro.energy_intensity",
 "values": [{"value": 2.5, "units": "kWh/m^3"}, ...]}

{"type": "configuration_option", "path": "plant.feed.max_withdrawal",
 "values": [{"value": 160, "units": "m^3/hr"}, ...]}

{"type": "cases", "cases": [
    {"name": "linear_tight_feed",
     "surrogates": {"plant.ro": "linear_surrogate"},
     "configuration_options": {"plant.feed.max_withdrawal.value": 160}},
    {"name": "aged_membranes",
     "variables": {"plant.ro.energy_intensity": {"value": 3.5, "units": "kWh/m^3"}}},
]}
```

- **`variable`** fixes a variable of the built model to each value (for
  flex-pse, a `fix` element). Use it for anything that is a Pyomo `Var`.
- **`configuration_option`** changes how the model is built. The path is any
  element's name followed by the option, so it works for any unit or block
  however deeply it's named. For a unit the option is one of its
  `construction_options`; for any other element it's one of the element's
  fields, so `costing.energy_prices.electrical` sweeps a price. Each task's
  spec is stored under its tag, with `base` as parent.
  - Options that are dicts can be set whole, with a dict value, or one key at
    a time with a longer path: `plant.feed.max_withdrawal.value` changes only
    the number and keeps the units. Missing dicts on the way are created.
  - Some options can't be swept as variables. The feed's `max_withdrawal`
    becomes the Param `withdrawal_max`, and a Param can be set but not fixed.
- **`surrogate`** swaps a stored or fitted surrogate into a unit. Options are
  set before surrogates are applied.
- **`cases`** runs named combinations that you choose. Each case sets any of
  `surrogates` (`{unit: tag}`), `configuration_options`, `variables`
  (`{path: value}`) and `spec`. A cases sweep counts as one sweep, so it can
  also be nested with others.
- **`spec_list`** builds each task from a different stored model spec.

## Combining sweeps

Several sweeps run every combination by default (`"combine": "product"`).
With `"combine": "zip"` they step together instead: the first surrogate with
the first value, and so on, so they must be the same length. For any other
mix, list the combinations as `cases`. Two sweeps can't set the same thing,
such as the same option or the same unit's surrogate, in one task.

Each task's tag comes from `output_name`. Its fields are each sweep's `name`
(by default `surrogate`, `case`, `spec`, or the path), plus `base` and
`index`. Values are written exactly as Python prints them, so `0.1 + 0.2`
gives `..._0.30000000000000004`. To shorten them, give the field a Python
format spec: `"{surrogate}_{withdrawal:.3g}"` or `"price_{price:.2f}"`. The tag
is only a name; the full value is stored with the result. If two values
format alike, the repeated tag is refused before anything runs.

## Storing and exporting the full model

The other scripts store only the RO's flow and power and the cost (`EXPORT`
in `common.py`). `sweep_nested.py` sets no `export`, so each result stores the
full model: about 400 values per result, covering every variable, parameter
and expression (35 timeseries, the rest scalars). It then writes the whole
study to disk and checks every value read back from the files against the
stored one:

```python
folder = HERE / "export" / "ro_nested_sweep"
exported = orch.export_study(folder, overwrite=True)
for result in orch.stored_model_results():
    on_disk = load_result(folder / "results" / f"{result.tag}.json")  # no database
    assert {s.key: s for s in on_disk.data()} == {s.key: s for s in result.data()}
```

The folder (about 1.2 MB here) holds JSON files: `specs/` (with the tariff
file), `data/`, `fits/` and `results/`, plus `study.json` and
`checksums.sha256`. Here that's 9 specs, 1 dataset, 2 fits and 6 results.
`orch.import_study(folder, name="copy")` (or `flex-orch import`) re-creates
the study in any store. `flex-orch export ro_nested_sweep <folder>` writes the
same folder from the command line. `export/` is in `.gitignore`, and the
example test deletes it before and after running.

The tariff package's cost terms (`costing.opex.*`) are indexed by their own
0–47 range rather than the model's time set, so each one is stored as 48
scalars (`...[0]` to `...[47]`) rather than as one timeseries.

## Results

`sweep_surrogates.py` matches `ro_surrogates` exactly:

```
fixed_intensity: $1,285.96, RO energy 10,355 kWh (true 8,725 kWh)
linear_surrogate: $1,238.92, RO energy 9,970 kWh (true 8,988 kWh)
```

The others print each task's operating cost. A higher specific energy costs
more, and so does a tighter feed: below about 200 m³/hr the plant can't
produce ahead of the expensive hours.

```
sweep_variables:      intensity_2.5 $1,082.00   intensity_3.0 $1,296.47   intensity_3.5 $1,510.93
sweep_configuration:  withdrawal_160 $1,600.26  withdrawal_180 $1,383.16  withdrawal_200 $1,296.47
sweep_nested:         fixed_intensity_160 $1,587.27   fixed_intensity_180 $1,371.95   fixed_intensity_200 $1,285.96
                      linear_surrogate_160 $1,507.82  linear_surrogate_180 $1,260.55  linear_surrogate_200 $1,238.92
sweep_cases:          linear_tight_feed $1,507.82  fixed_loose_feed $1,285.96  aged_membranes $1,510.93
```

Each case matches the same combination in the other scripts.

Written to a YAML or JSON file, any of these configs runs from the command
line: `flex-orch --data-dir examples/ro_study/.flexorch run study.yaml --study
my_study`. `orch.execute_study(STUDY, dry_run=True)` (or `--dry-run`) returns
the planned tasks without running anything.
