"""The exporter on a user's own Pyomo model, not built by flex-pse.

Users can bring models that aren't flex-pse. These tests cover what such a
model can contain and flex-pse models don't: no TimeBlock, a custom index before
time, nested Set products, index Sets of unknown width, index values that
aren't strings or numbers, non-numeric Params and expressions with inconsistent
units. Everything flex-pse models do contain is tested on flex-pse models in
``test_walker.py``.
"""

import pyomo.environ as pyo
import pytest
from pyomo.environ import units as pu

from flexorchestrator.errors import ExportError
from flexorchestrator.export import export_model_data

pytestmark = pytest.mark.unit


@pytest.fixture
def user_model():
    m = pyo.ConcreteModel()
    m.t = pyo.Set(initialize=range(3), ordered=True)
    m.pumps = pyo.Set(initialize=["p1", "p2"], ordered=True)
    return m


def test_without_a_time_block_nothing_is_time(user_model):
    m = user_model
    m.v = pyo.Var(m.t, initialize=1.0)
    assert [s.kind for s in export_model_data(m)] == ["scalar"] * 3


def test_time_sets_without_datetimes_use_the_raw_values(user_model):
    m = user_model
    m.v = pyo.Var(m.t, initialize=1.0)
    assert export_model_data(m, time_sets=[m.t])["v"].timestamps == [0, 1, 2]


def test_custom_index_before_time(user_model):
    m = user_model
    m.flow = pyo.Var(m.pumps, m.t, units=pu.L / pu.day, initialize=2.0)
    data = export_model_data(m, time_sets=[m.t])
    assert data.keys() == ["flow[p1]", "flow[p2]"]
    assert (data["flow[p2]"].kind, data["flow[p2]"].unit_qudt) == (
        "timeseries",
        "L-PER-DAY",
    )


def test_time_inside_nested_products(user_model):
    m = user_model
    m.b = pyo.Set(initialize=[1, 2])
    m.v = pyo.Var(m.pumps * (m.b * m.t), initialize=1.0)
    data = export_model_data(m, time_sets=[m.t])
    assert data.keys() == ["v[p1,1]", "v[p1,2]", "v[p2,1]", "v[p2,2]"]
    assert data["v[p1,1]"].index == ["p1", 1]


def test_time_position_unknown_or_repeated_means_custom(user_model):
    m = user_model
    m.any = pyo.Set(initialize=[1, (2, 3)], dimen=None)  # unknown width
    m.v = pyo.Var(m.any * m.t, initialize=1.0)
    m.w = pyo.Var(m.t * m.t, initialize=1.0)  # time twice
    data = export_model_data(m, time_sets=[m.t])
    assert {s.kind for s in data} == {"scalar"}
    assert len([s for s in data if s.component == "w"]) == 9


def test_colliding_keys_raise():
    class Tag:
        def __init__(self, n):
            self.n = n

        def __str__(self):
            return "same"

    m = pyo.ConcreteModel()
    m.s = pyo.Set(initialize=[Tag(1), Tag(2)], dimen=1)
    m.v = pyo.Var(m.s, initialize=0.0)
    with pytest.raises(ExportError, match=r"Two values export as 'v\[same\]'"):
        export_model_data(m)


def test_non_json_index_values_become_strings():
    class Tag:
        def __str__(self):
            return "tag"

    m = pyo.ConcreteModel()
    m.s = pyo.Set(initialize=[Tag()], dimen=1)
    m.v = pyo.Var(m.s, initialize=0.0)
    assert export_model_data(m)["v[tag]"].index == ["tag"]


def test_non_numeric_params_warn_or_raise():
    m = pyo.ConcreteModel()
    m.label = pyo.Param(mutable=True, initialize="abc", within=pyo.Any)
    m.x = pyo.Var(initialize=1.0)
    with pytest.warns(UserWarning, match="non-numeric"):
        assert export_model_data(m).keys() == ["x"]
    with pytest.raises(ExportError, match="non-numeric"):
        export_model_data(m, keys=["label"])


def test_immutable_params_only_when_named():
    m = pyo.ConcreteModel()
    m.fixed = pyo.Param(initialize=7.0)
    m.x = pyo.Var(initialize=1.0)
    assert export_model_data(m).keys() == ["x"]
    assert export_model_data(m, keys=["fixed"])["fixed"].value == 7.0


def test_inconsistent_expression_units_are_unknown():
    m = pyo.ConcreteModel()
    m.x = pyo.Var(units=pu.kW, initialize=1.0)
    m.y = pyo.Var(units=pu.m, initialize=1.0)
    m.bad = pyo.Expression(expr=m.x + m.y)
    with pytest.warns(UserWarning, match="Can't work out the units of 'bad'"):
        data = export_model_data(m, keys=["bad"])
    assert (data["bad"].unit_pyomo, data["bad"].unit_qudt) == (None, None)
