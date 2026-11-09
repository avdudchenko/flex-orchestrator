# Design study

Sizes a battery and solar for one plant across two modeled years and two
representative months, at three solar prices, and stores the results on disk.

    python examples/design_study/example.py

- `flowsheet.json` is an ordinary flowsheet. Storage must be cyclic: a design study
  refuses fixed initial states.
- `study.yaml` adds a `design:` section. Without it, the same config runs as an
  ordinary study.
- By default a design study stores only its summary, which
  `orch.design_results(tag)` reads. Export patterns like
  `horizon[2030,7].battery.charge` add per-horizon series.
