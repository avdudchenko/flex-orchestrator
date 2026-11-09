"""Shared flex-pse specs for tests (plain data, no flex-pse import).

Tests that need a Pyomo model build :data:`DEMO_SPEC` with the flex-pse build
hook, so they exercise flex-pse's real TimeBlock, References and units rather
than a hand-made stand-in.

Besides the hand-written :data:`DEMO_SPEC`, three specs are flex-pse's own: emitted
with ``flexops.emit_model`` from flex-pse 0.2.0's round-trip corpus
(``flexops/tests/fixtures/roundtrip_corpus.py``) and kept as JSON under
``data/``. Re-emit them when the flex-pse pin moves.
"""

import json
from pathlib import Path

_DATA = Path(__file__).parent / "data"


def _load(name: str) -> dict:
    return json.loads((_DATA / f"{name}.json").read_text(encoding="utf-8"))


DEMO_SPEC: dict = {
    "schema_version": "0.1.1",
    "name": "demo",
    "elements": [
        {
            "kind": "time",
            "start_date": "2025-07-08",
            "end_date": "2025-07-08T06:00",
            "time_step": "1 hr",
        },
        {
            "kind": "property_package",
            "name": "properties",
            "property_class": "SimpleAqueousFlow",
        },
        {
            "kind": "costing",
            "name": "costing",
            "energy_prices": {"electrical": {"value": 0.1, "units": "USD/kWh"}},
        },
        {"kind": "plant", "name": "demo"},
        {
            "kind": "unit",
            "name": "demo.tank",
            "unit_model_class": "Tank",
            "costing_package": None,
            "construction_options": {
                "max_volume": {"value": 1000.0, "units": "m^3"},
                "initial_volume": {"value": 500.0, "units": "m^3"},
            },
            "connections": [
                {"port": "outlet", "to": "demo.pump.inlet", "name": "arc_0"}
            ],
        },
        {
            "kind": "unit",
            "name": "demo.pump",
            "unit_model_class": "ConstantEnergyIntensityModel",
            "construction_options": {
                "energy_intensity": {"value": 0.5, "units": "kWh/m^3"}
            },
        },
        {
            "kind": "unit",
            "name": "demo.battery",
            "unit_model_class": "BatteryModel",
            "property_package": None,
            "construction_options": {
                "energy_capacity": {"value": 10.0, "units": "kWh"}
            },
        },
        {"kind": "objective", "name": "objective"},
    ],
}
"""dict: A six-hour version of flex-pse's ``plant_config_demo.json``
(schema 0.1.1) with a flat energy price instead of a tariff file, so it builds
from any working directory."""

LINEAR_POWER: dict = {
    "surrogate_type": "multilinear",
    "data": {
        "input_variables": {"flow_in": "m^3/hr"},
        "output_variables": {"power_electrical": "kW"},
        "coefficients": {"intercept": 0.0, "flow_in": 0.6},
    },
}
"""dict: A multilinear surrogate for ``demo.pump``'s power relation:
0.6 kWh/m^3, no offset."""

LOGIC_SPEC: dict = _load("logic_indexed")
"""dict: Three RO trains as an indexed unit (``p.ro[0..2]``, over the set
element ``p.trains = [0, 1, 2]``) with status and startup/shutdown logic;
6 points."""

SURROGATE_DISPATCH_SPEC: dict = _load("surrogate_and_dispatch")
"""dict: A pump with a multilinear surrogate (``coefficient_vars`` indexed by
coefficient name) and a battery with a dispatched ``power_charge``; 6 points."""

NETWORK_SPEC: dict = _load("network_two_plants")
"""dict: A network of two plants (``net.a``, ``net.b``); 24 points."""
