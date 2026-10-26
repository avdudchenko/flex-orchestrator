"""Pyomo -> QUDT unit keys (generated from pint's parts) and unit conversion."""

import warnings

import pytest
from pyomo.environ import units as pu

from flex_orchestrator.errors import UnitError
from flex_orchestrator.export import BASE_UNITS, UnitMapper, conversion_factor

QUDT = "http://qudt.org/vocab/unit/"

# A tiny stand-in for Acquirium's resolve_unit: QUDT key -> (multiplier, offset),
# plus text that resolves to a key. "a" resolving to ARE (an area) is a real
# QUDT text match for Pyomo's year.
FAKE_QUDT = {
    "L-PER-DAY": (1e-3 / 86400, 0.0),
    "KiloW-HR": (3.6e6, 0.0),
    "M3-PER-HR": (1 / 3600, 0.0),
    "J-PER-KiloGM-K": (1.0, 0.0),
    "PSI": (6894.757293168361, 0.0),
    "ARE": (100.0, 0.0),
    "PER-YR": (1.0, 0.0),  # wrong on purpose: 1/a is 3.17e-8 1/s
    "DEG_C": (1.0, 273.15),
}
FAKE_TEXT = {"psi": "PSI", "a": "ARE"}


def fake_resolver(identifier):
    key = FAKE_TEXT.get(identifier, identifier)
    if key not in FAKE_QUDT:
        raise ValueError(f"Unit {identifier!r} not found")
    multiplier, offset = FAKE_QUDT[key]
    return {"uri": QUDT + key, "multiplier": multiplier, "offset": offset}


@pytest.fixture
def usd():
    if not hasattr(pu, "USD"):
        pu.load_definitions_from_strings(["USD = [currency]"])


# Generation from parts (no resolver)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("unit", "key"),
    [
        ("L/day", "L-PER-DAY"),
        ("kg/m**3", "KiloGM-PER-M3"),
        ("kWh", "KiloW-HR"),
        ("kW*h", "KiloW-HR"),
        ("m**3/h", "M3-PER-HR"),
        ("1/a", "PER-YR"),
        ("J/kg/K", "J-PER-KiloGM-K"),
        ("mg/L", "MilliGM-PER-L"),
        ("m/s**2", "M-PER-SEC2"),
        ("kmol/h", "KiloMOL-PER-HR"),
        ("gal/min", "GAL_US-PER-MIN"),
        ("km**2", "KiloM2"),
        ("degC", "DEG_C"),
        ("degC/h", "DEG_C-PER-HR"),
        ("percent", "PERCENT"),
        ("dimensionless", "UNITLESS"),
    ],
)
def test_keys_are_generated_from_parts(unit, key):
    mapping = UnitMapper().map(unit)
    assert (mapping.pyomo, mapping.qudt, mapping.source) == (unit, key, "generated")
    assert not mapping.verified


@pytest.mark.unit
def test_currencies_become_ccy_keys(usd):
    mapper = UnitMapper()
    assert mapper.map("USD").qudt == "CCY_USD"
    assert mapper.map("USD/kWh").qudt == "CCY_USD-PER-KiloW-HR"
    assert mapper.map("USD/a").qudt == "CCY_USD-PER-YR"


@pytest.mark.unit
@pytest.mark.parametrize("unit", ["psi", "not a unit", "Wh**2"])
def test_units_with_no_generated_key_warn(unit):
    with pytest.warns(UserWarning, match="No QUDT unit"):
        mapping = UnitMapper().map(unit)
    assert (mapping.qudt, mapping.source) == (None, "none")


@pytest.mark.unit
def test_base_units_can_be_replaced():
    assert UnitMapper(base_units={"liter": "LITRE"}).map("L/s").qudt is None
    assert UnitMapper(base_units={"liter": "LITRE"}).map("L").qudt == "LITRE"


# Verification and lookup through a resolver


@pytest.mark.unit
@pytest.mark.parametrize(
    ("unit", "key"),
    [("L/day", "L-PER-DAY"), ("kW*h", "KiloW-HR"), ("m**3/h", "M3-PER-HR")],
)
def test_resolver_verifies_generated_keys(unit, key):
    mapping = UnitMapper(resolver=fake_resolver).map(unit)
    assert (mapping.qudt, mapping.source, mapping.verified) == (key, "verified", True)


@pytest.mark.unit
def test_resolver_tries_other_orderings():
    # J/K/kg generates J-PER-K-KiloGM first; QUDT spells it J-PER-KiloGM-K.
    mapping = UnitMapper(resolver=fake_resolver).map("J/K/kg")
    assert (mapping.qudt, mapping.source) == ("J-PER-KiloGM-K", "verified")


@pytest.mark.unit
def test_offset_units_are_verified_by_key_alone():
    mapping = UnitMapper(resolver=fake_resolver).map("degC")
    assert (mapping.qudt, mapping.source) == ("DEG_C", "verified")


@pytest.mark.unit
def test_generated_key_missing_from_qudt_is_none():
    with pytest.warns(UserWarning, match=r"\(KiloW-HR-PER-M3 isn't in QUDT\)"):
        mapping = UnitMapper(resolver=fake_resolver).map("kWh/m**3")
    assert (mapping.qudt, mapping.source) == (None, "none")


@pytest.mark.unit
def test_generated_key_with_wrong_multiplier_is_rejected():
    with pytest.warns(UserWarning):
        assert UnitMapper(resolver=fake_resolver).map("1/a").qudt is None


@pytest.mark.unit
def test_unlisted_base_units_are_found_through_the_resolver():
    mapping = UnitMapper(resolver=fake_resolver).map("psi")
    assert (mapping.qudt, mapping.source) == ("PSI", "verified")


@pytest.mark.unit
def test_text_match_with_wrong_multiplier_is_not_trusted():
    # Without "year" in the base units, "a" is looked up and QUDT says ARE.
    with pytest.warns(UserWarning):
        mapping = UnitMapper(resolver=fake_resolver, base_units={}).map("a")
    assert mapping.qudt is None


@pytest.mark.unit
def test_base_lookup_ignores_offset_answers():
    # QUDT's DEG_C has an offset; it can't stand for a part of a compound unit.
    FAKE_TEXT["°C"] = "DEG_C"
    try:
        with pytest.warns(UserWarning):
            mapping = UnitMapper(resolver=fake_resolver, base_units={}).map("degC")
    finally:
        del FAKE_TEXT["°C"]
    assert mapping.qudt is None


@pytest.mark.unit
def test_whole_unit_lookup_when_parts_have_no_key():
    FAKE_TEXT["m**3/h"] = "M3-PER-HR"
    try:
        mapping = UnitMapper(resolver=fake_resolver, base_units={}).map("m**3/h")
    finally:
        del FAKE_TEXT["m**3/h"]
    assert (mapping.qudt, mapping.source) == ("M3-PER-HR", "resolver")


@pytest.mark.unit
def test_resolver_returning_nothing_is_a_miss():
    with pytest.warns(UserWarning):
        assert UnitMapper(resolver=lambda unit: None).map("L/day").qudt is None


# Overrides, strictness, caching


@pytest.mark.unit
def test_override_wins():
    mapping = UnitMapper(overrides={"kW": "MY-KW"}, resolver=fake_resolver).map("kW")
    assert (mapping.qudt, mapping.source, mapping.verified) == (
        "MY-KW",
        "override",
        True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("unit", ["psi", "not a unit"])
def test_strict_mode_raises(unit):
    with pytest.raises(UnitError, match="unit_map="):
        UnitMapper(strict=True).map(unit)


@pytest.mark.unit
def test_unknown_units_are_none_quietly():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert UnitMapper(strict=True).map(None).qudt is None


@pytest.mark.unit
def test_answers_are_cached():
    calls = []

    def resolver(identifier):
        calls.append(identifier)
        return fake_resolver(identifier)

    mapper = UnitMapper(resolver=resolver)
    mapper.map("L/day")
    mapper.map("L/day")
    assert calls == ["L-PER-DAY"]
    with pytest.warns(UserWarning):
        mapper.map("kWh/m**3")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        mapper.map("kWh/m**3")  # no second warning


# Conversion


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "target", "factor"),
    [("kW", "W", 1000.0), ("m**3/h", "m**3/s", 1 / 3600), ("kWh", "kWh", 1.0)],
)
def test_conversion_factor(source, target, factor):
    assert conversion_factor(source, target) == pytest.approx(factor)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "target", "match"),
    [
        ("kW", "m", "dimensions differ"),
        ("degC", "K", "offset unit"),
        ("kW", "not a unit", "Can't parse"),
    ],
)
def test_conversion_factor_errors(source, target, match):
    with pytest.raises(UnitError, match=match):
        conversion_factor(source, target)


# Against Acquirium's real QUDT vocabulary


@pytest.fixture(scope="module")
def qudt_resolver():
    acquirium = pytest.importorskip("acquirium")
    from pathlib import Path

    from acquirium.internals.qudt_units import QUDTUnitConverter

    ttl = Path(acquirium.__file__).parent / "_ontologies" / "qudt_unit.ttl"
    converter = QUDTUnitConverter(str(ttl))

    def resolver(identifier):
        found = converter.resolve_unit(identifier)
        return {
            "uri": str(found.uri),
            "multiplier": found.multiplier,
            "offset": found.offset,
        }

    return resolver


@pytest.mark.component
def test_every_base_unit_is_in_qudt(qudt_resolver):
    registry = pu.pint_registry
    for base, key in BASE_UNITS.items():
        found = qudt_resolver(key)
        assert found["uri"].endswith(f"/{key}"), base
        quantity = registry.Quantity(1, base)
        if quantity._is_multiplicative:
            si = quantity.to_base_units().magnitude
            assert found["multiplier"] == pytest.approx(si, rel=1e-6), (base, key)


@pytest.mark.component
@pytest.mark.parametrize(
    ("unit", "key"),
    [
        ("L/day", "L-PER-DAY"),
        ("kg/day", "KiloGM-PER-DAY"),
        ("mg/L", "MilliGM-PER-L"),
        ("ug/L", "MicroGM-PER-L"),
        ("m/s**2", "M-PER-SEC2"),
        ("J/kg/K", "J-PER-KiloGM-K"),
        ("W/m**2", "W-PER-M2"),
        ("m**3/min", "M3-PER-MIN"),
        ("kmol/h", "KiloMOL-PER-HR"),
        ("gal/min", "GAL_US-PER-MIN"),
        ("MWh", "MegaW-HR"),
        ("kW*h", "KiloW-HR"),
        ("1/a", "PER-YR"),
        ("a", "YR"),
        ("degC", "DEG_C"),
        ("degC/h", "DEG_C-PER-HR"),
        ("degF/h", "DEG_F-PER-HR"),
        ("dimensionless", "UNITLESS"),
        ("psi", "PSI"),  # base found through QUDT
        ("hp", "HP"),
    ],
)
def test_generated_keys_verify_against_real_qudt(qudt_resolver, unit, key):
    mapping = UnitMapper(resolver=qudt_resolver).map(unit)
    assert (mapping.qudt, mapping.source) == (key, "verified")


@pytest.mark.component
@pytest.mark.parametrize("unit", ["kWh/m**3", "USD/kWh", "USD/a"])
def test_units_missing_from_real_qudt(qudt_resolver, usd, unit):
    with pytest.warns(UserWarning, match="isn't in QUDT"):
        assert UnitMapper(resolver=qudt_resolver).map(unit).qudt is None
