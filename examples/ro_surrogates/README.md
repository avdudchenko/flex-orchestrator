# RO surrogates

A feed → RO → tank → product plant must deliver a constant 90 m³/hr. It buys
electricity on a summer time-of-use tariff (`tariff.csv`, read by EECO), and
the tank lets the RO run harder off-peak. The RO's specific energy depends on
its permeate flow, lowest at the 90 m³/hr design point:

    SEC(permeate) = 2.5 + 0.0004 (permeate − 90)²   kWh/m³

`example.py` compares two ways of representing that in the model, in one
script with no options:

1. Store a month of synthetic RO operating data: permeate flow, and power
   `SEC(permeate) × permeate` with 2% noise.
2. Build the model in Python, emit its spec with flex-pse's `emit_model`, and
   store it as `base`. The tariff file is stored with the spec.
3. Read the data back and fit two relationships with flex-pse's regressors,
   storing both as surrogate specs:
   - `ro_average`: the average energy intensity (`ConstantIntensityRegressor`);
   - `ro_linear`: power as a linear function of permeate (`LinearRegressor`).
4. Solve `base` with the average intensity fixed (a build override), and
   `ro_linear`, the spec with the linear surrogate applied to the RO. Store
   both results.
5. Read the results back. For each, compare the RO energy the model predicts
   with what the RO would really use running that schedule, from the curve.

```
python examples/ro_surrogates/example.py
```

It keeps its data in `examples/ro_surrogates/.flexorch` and starts the study
again on each run. Its output ends with:

```
average intensity: 2.976 kWh/m^3
linear surrogate: power = 3.417 * permeate -40.0 kW
Comparing estimated vs real power use due to surrogate simplifications
fixed_intensity: $1,285.96, RO energy 10,355 kWh (true 8,725 kWh)
linear_surrogate: $1,238.92, RO energy 9,970 kWh (true 8,988 kWh)
```

The linear surrogate picks a cheaper schedule, and its energy estimate is
closer to the truth (11% high, against 19% for the fixed intensity).
