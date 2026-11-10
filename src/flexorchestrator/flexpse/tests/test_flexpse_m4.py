"""The flex-pse hooks for regression, prediction and costing overrides."""

import copy

import pandas as pd
import pytest

from flexorchestrator.errors import FitError, SpecError
from flexorchestrator.export.units import pyomo_unit_of, qudt_to_pyomo

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("key", "unit"),
    [
        ("M3-PER-HR", "meter**3/hour"),
        ("KiloW-HR-PER-M3", "kilowatt*hour/meter**3"),
        ("MilliGM-PER-L", "milligram/liter"),
        ("PER-HR", "1/hour"),
        ("UNITLESS", "dimensionless"),
        ("NOT_A_UNIT", None),
        ("KiloXYZ", None),
        ("CCY_XYZ-PER-HR", None),  # an unknown currency
        ("M3-PER-XYZ", None),
    ],
)
def test_qudt_keys_to_pyomo_units(key, unit):
    assert qudt_to_pyomo(key) == unit


def test_pyomo_unit_of_takes_text_or_keys():
    assert pyomo_unit_of(None) is None
    assert pyomo_unit_of("kW") == "kW"
    assert pyomo_unit_of("M3-PER-DAY") == "meter**3/day"
    assert pyomo_unit_of("A") == "ampere"
    assert pyomo_unit_of("not a unit") is None


def test_get_regressor():
    pytest.importorskip("flexparameterize")
    from flexorchestrator.flexpse import get_regressor

    assert get_regressor("constant_intensity").__name__ == "ConstantIntensityRegressor"
    with pytest.raises(NotImplementedError, match="installed flex-pse"):
        get_regressor("quadratic")
    with pytest.raises(FitError, match="not a known SurrogateType"):
        get_regressor("nope")


def test_predict_surrogate():
    pytest.importorskip("flexparameterize")
    from flexorchestrator.flexpse import predict_surrogate

    X = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]})
    linear = {
        "surrogate_type": "multilinear",
        "data": {"coefficients": {"intercept": 1.0, "a": 2.0, "a*b": 0.5}},
    }
    assert predict_surrogate(linear, X) == [4.5, 9.0]
    constant = {
        "surrogate_type": "constant_intensity",
        "data": {"coefficients": {"energy_intensity": 3.0}},
    }
    assert predict_surrogate(constant, X[["a"]]) == [3.0, 6.0]
    with pytest.raises(FitError, match="one input column"):
        predict_surrogate(constant, X)
    for coefficients in ({}, {"a": 1.0, "b": 2.0}):
        bad = {
            "surrogate_type": "constant_intensity",
            "data": {"coefficients": coefficients},
        }
        with pytest.raises(FitError, match="one coefficient"):
            predict_surrogate(bad, X[["a"]])
    with pytest.raises(FitError, match="needs column 'c'"):
        predict_surrogate(
            {"surrogate_type": "multilinear", "data": {"coefficients": {"c": 1}}}, X
        )
    with pytest.raises(FitError, match="Can't evaluate a 'arima'"):
        predict_surrogate({"surrogate_type": "arima", "data": {}}, X)


def test_overrides_edit_costing_fields(demo_spec):
    pytest.importorskip("flexops")
    from flexorchestrator.flexpse import with_overrides

    before = copy.deepcopy(demo_spec)
    price = {"value": 0.2, "units": "USD/kWh"}
    spec = with_overrides(
        demo_spec,
        {"costing.energy_prices.electrical": price, "demo.pump.flow_in": 1.0},
    )
    assert demo_spec == before
    costing = next(e for e in spec["elements"] if e.get("kind") == "costing")
    assert costing["energy_prices"]["electrical"] == price
    assert spec["elements"][-1]["variable"] == "demo.pump.flow_in"
    with pytest.raises(SpecError, match="'value' is .*not a dict"):
        with_overrides(demo_spec, {"costing.energy_prices.electrical.value.x": 1})


def test_configure_sets_options_of_any_element(demo_spec):
    pytest.importorskip("flexops")
    from flexorchestrator.flexpse import configure

    before = copy.deepcopy(demo_spec)
    units = {e["name"]: e for e in demo_spec["elements"] if e.get("kind") == "unit"}
    name = next(iter(units))
    whole = {"value": 2.0, "units": "kW"}
    spec = configure(demo_spec, f"{name}.size", whole)
    assert demo_spec == before
    edited = {e.get("name"): e for e in spec["elements"]}
    assert edited[name]["construction_options"]["size"] == whole
    # One key of a dict option, creating the dicts on the way.
    spec = configure(spec, f"{name}.size.value", 3.0)
    spec = configure(spec, f"{name}.extra.a.b", [1, 2])
    options = {e.get("name"): e for e in spec["elements"]}[name]["construction_options"]
    assert options["size"] == {"value": 3.0, "units": "kW"}
    assert options["extra"] == {"a": {"b": [1, 2]}}
    # A non-unit element's options are its own fields.
    price = {"value": 0.3, "units": "USD/kWh"}
    spec = configure(demo_spec, "costing.energy_prices.electrical", price)
    costing = next(e for e in spec["elements"] if e.get("kind") == "costing")
    assert costing["energy_prices"]["electrical"] == price


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("nope.size", "doesn't start with an element's name"),
        ("costing", "names the element itself"),
        ("costing.energy_prices.electrical.value.x", "'value' is .*not a dict"),
    ],
)
def test_configure_errors(demo_spec, path, message):
    pytest.importorskip("flexops")
    from flexorchestrator.flexpse import configure

    with pytest.raises(SpecError, match=message):
        configure(demo_spec, path, 1)
