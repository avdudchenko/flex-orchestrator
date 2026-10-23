"""fit_surrogate: selectors, preparation, regressors and what is stored (PLAN.md D19).

The memory backend runs these at the unit tier; scikit-learn fits without a
solver. A stand-in for the general store tests the stream selectors here, and
``test_fitting_streams.py`` runs them against Acquirium.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from flex_orchestrator.backend import MemoryBackend
from flex_orchestrator.errors import FitError, TagExistsError
from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.fitting import (
    FitParams,
    Prep,
    Selector,
    regress,
    selectors,
    store,
)
from flex_orchestrator.fitting import prep as preparation
from flex_orchestrator.orchestrator import Orchestrator
from flex_orchestrator.testing import memory_orchestrator

pytest.importorskip("flexparameterize")
pytestmark = pytest.mark.unit

START = datetime(2025, 6, 1, tzinfo=UTC)
X = {"dataset": "ro", "columns": ["permeate"]}
Y = {"dataset": "ro", "columns": ["power_electrical"]}


def ro_frame(hours=48, seed=0):
    rng = np.random.default_rng(seed)
    time = pd.date_range(START, periods=hours, freq="h")
    permeate = rng.uniform(30, 150, hours)
    power = 3.0 * permeate + 10 + rng.normal(0, 1, hours)
    return pd.DataFrame({"permeate": permeate, "power_electrical": power}, time)


@pytest.fixture
def orch():
    orch = memory_orchestrator(study="fits")
    orch.store_data(
        "ro", ro_frame(), units={"permeate": "m^3/hr", "power_electrical": "kW"}
    )
    return orch


def fit(orch, **params):
    return orch.fit_surrogate(**{"X": X, "y": Y, **params})


# Selectors


def test_a_selector_has_exactly_one_source():
    assert Selector(dataset="a").source == "dataset"
    for bad in (
        {},
        {"dataset": "a", "result": "b", "keys": ["k"]},
        {"result": "r"},
        {"study": "s", "resolve": "x"},
        {"columns": ["c"], "ref_uris": ["u"]},
    ):
        with pytest.raises(ValueError):
            Selector(**bad)


def test_a_dataset_selector_reads_columns_with_their_units(orch):
    found = selectors.load(orch, Selector(**X, end=START + timedelta(hours=9)))
    assert list(found.frame.columns) == ["permeate"]
    assert len(found.frame) == 10
    assert str(found.frame.index.tz) == "UTC"
    assert found.units == {"permeate": "m^3/hr"}
    assert found.provenance["source"] == "dataset"
    assert found.provenance["rows"] == 10


def test_a_dataset_of_another_study(orch):
    orch.create_study("other")
    orch.store_data("flows", ro_frame(5)[["permeate"]], units={"permeate": "M3-PER-HR"})
    orch.select_study("fits")
    found = selectors.load(orch, Selector(dataset="flows", study="other"))
    assert found.provenance["study"] == "other"
    assert len(found.frame) == 5


def test_rename_and_unit_conversion(orch):
    found = selectors.load(
        orch,
        Selector(**X, rename={"*": "flow_out"}, units="m^3/day"),
    )
    raw = orch.get_data("ro", frame="pandas")["permeate"]
    assert list(found.frame.columns) == ["flow_out"]
    assert found.units == {"flow_out": "m^3/day"}
    assert found.frame["flow_out"].iloc[0] == pytest.approx(raw.iloc[0] * 24)
    found = selectors.load(
        orch, Selector(dataset="ro", rename={"permeate": "q"}, units={"q": "M3-PER-HR"})
    )
    assert found.units["q"] == "meter**3/hour"


@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ({"dataset": "nope"}, "no stored data 'nope'"),
        ({**X, "rename": {"*": "a", "b": "c"}}, "needs a selector with one column"),
        ({"dataset": "ro", "rename": {"*": "a"}}, "needs a selector with one column"),
        ({**X, "rename": {"zzz": "a"}}, "rename names columns"),
        ({**X, "units": {"zzz": "kW"}}, "units= names columns"),
        ({**X, "units": "not a unit at all"}, "Can't read unit"),
        ({**X, "units": "kW"}, "Can't convert"),
        ({**X, "start": START + timedelta(days=9)}, "matched no rows"),
    ],
)
def test_selector_errors(orch, selector, message):
    with pytest.raises(FitError, match=message):
        selectors.load(orch, Selector(**selector))


def test_a_column_without_a_unit_cant_be_converted(orch):
    orch.store_data("bare", ro_frame(3)[["permeate"]])
    with pytest.raises(FitError, match="has no stored unit"):
        selectors.load(orch, Selector(dataset="bare", units="m^3/day"))


def test_a_result_selector(orch):
    stamps = [START + timedelta(hours=h) for h in range(3)]
    data = ModelData(
        [
            ExportedSeries(
                key="plant.ro.permeate",
                kind="timeseries",
                component="plant.ro.permeate",
                index=[],
                values=[1.0, 2.0, 3.0],
                timestamps=stamps,
                unit_pyomo="m**3/h",
                unit_qudt=None,
            )
        ]
    )
    orch.store_model_data("sim", data)
    found = selectors.load(
        orch, Selector(result="sim", keys=["plant.ro.permeate"], start=stamps[1])
    )
    assert found.frame["plant.ro.permeate"].tolist() == [2.0, 3.0]
    assert found.units == {"plant.ro.permeate": "m**3/h"}
    assert found.provenance["source"] == "result"
    with pytest.raises(FitError, match="has no timeseries"):
        selectors.load(orch, Selector(result="sim", keys=["nope"]))
    found = selectors.load(
        orch, Selector(result="sim", keys=["plant.ro.permeate"], end=stamps[0])
    )
    assert found.frame.shape == (1, 1)


class StreamBackend(MemoryBackend):
    """The memory backend plus a small general store."""

    def __init__(self):
        super().__init__()
        stamps = [START + timedelta(hours=h) for h in range(4)]
        self.streams = {
            "urn:s#1": ("FT_1", "digester biogas volume", "M3-PER-DAY", stamps),
            "urn:s#2": ("FT_2", "digester feed flow", "M3-PER-HR", stamps),
            "urn:s#3": (None, "feed flow two", None, stamps[:2]),
        }
        self.queries = []

    def stream_rows(self, ref, start=None, end=None):
        return [
            (t, float(i))
            for i, t in enumerate(self.streams[ref][3])
            if (start is None or t >= start) and (end is None or t <= end)
        ]

    def sparql_select(self, query):
        self.queries.append(query)
        return {"columns": ["ref"], "rows": [["urn:s#2"], ["urn:s#2"], [None]]}

    def stream_info(self, refs):
        return {
            r: dict(zip(("name", "label", "unit"), self.streams[r][:3], strict=True))
            for r in refs
        }

    def find_streams(self, text):
        return [
            {"ref": r, "label": label, "point": "p"}
            for r, (_, label, _, _) in self.streams.items()
            if all(w in label for w in text.split())
        ]


@pytest.fixture
def stream_orch():
    orch = Orchestrator(StreamBackend(), environ={})
    orch.create_study("s")
    return orch


def test_ref_uri_selectors_read_streams(stream_orch):
    found = selectors.load(stream_orch, Selector(ref_uris=["urn:s#1", "urn:s#3"]))
    assert list(found.frame.columns) == ["FT_1", "3"]
    assert found.units == {"FT_1": "meter**3/day", "3": None}
    assert found.frame["3"].isna().sum() == 2
    assert found.provenance["refs"] == {"FT_1": "urn:s#1", "3": "urn:s#3"}


def test_resolve_needs_exactly_one_stream(stream_orch):
    found = selectors.load(
        stream_orch, Selector(resolve="biogas", rename={"*": "biogas_volume"})
    )
    assert list(found.frame.columns) == ["biogas_volume"]
    assert found.provenance["refs"] == {"biogas_volume": "urn:s#1"}
    assert found.provenance["label"] == "digester biogas volume"
    with pytest.raises(FitError, match="matched 2 streams"):
        selectors.load(stream_orch, Selector(resolve="feed flow"))
    with pytest.raises(FitError, match="matched no stream"):
        selectors.load(stream_orch, Selector(resolve="nothing"))


def test_sparql_selectors_only_read(stream_orch):
    found = selectors.load(stream_orch, Selector(sparql="SELECT ?ref WHERE {}"))
    assert list(found.frame.columns) == ["FT_2"]
    for query in ("INSERT DATA { <a> <b> <c> }", "DESCRIBE <a>"):
        with pytest.raises(FitError, match="read-only SELECT"):
            selectors.load(stream_orch, Selector(sparql=query))
    stream_orch.backend.sparql_select = lambda q: {"columns": ["r"], "rows": []}
    with pytest.raises(FitError, match="no stream refs"):
        selectors.load(stream_orch, Selector(sparql="SELECT ?r WHERE {}"))


def test_two_streams_with_one_name(stream_orch):
    stream_orch.backend.streams["urn:s#4"] = ("FT_1", "x", None, [START])
    with pytest.raises(FitError, match="both named 'FT_1'"):
        selectors.load(stream_orch, Selector(ref_uris=["urn:s#1", "urn:s#4"]))


def test_stream_selectors_need_a_general_store(orch):
    with pytest.raises(FitError, match="doesn't have; use the Acquirium backend"):
        selectors.load(orch, Selector(ref_uris=["urn:x"]))


# Preparation


def selected(frame, units=None):
    return selectors.Selected(frame, units or dict.fromkeys(frame.columns))


def test_prepare_aligns_and_hashes():
    frame = ro_frame(6)
    xs = [selected(frame[["permeate"]].iloc[1:])]
    ys = [selected(frame[["power_electrical"]])]
    inner = preparation.prepare(xs, ys, Prep())
    assert len(inner.X) == 5
    assert inner.steps[0] == {"step": "align", "how": "inner", "rows": 5}
    again = preparation.prepare(xs, ys, Prep())
    assert again.data_hash == inner.data_hash
    changed = preparation.prepare(
        [selected(frame[["permeate"]].iloc[1:] + 1)], ys, Prep()
    )
    assert changed.data_hash != inner.data_hash
    with pytest.raises(FitError, match="have missing values"):
        preparation.prepare(xs, ys, Prep(align="outer"))


@pytest.mark.parametrize(
    ("gaps", "rows"), [("drop", 5), ("interpolate", 6), ("longest_segment", 3)]
)
def test_gap_handling(gaps, rows):
    frame = ro_frame(6)
    frame.iloc[1 if gaps == "longest_segment" else 2, 0] = np.nan
    if gaps == "longest_segment":
        frame.iloc[5, 0] = np.nan  # complete runs: row 0, rows 2-4
    result = preparation.prepare(
        [selected(frame[["permeate"]])],
        [selected(frame[["power_electrical"]])],
        Prep(gaps=gaps, limit=1),
    )
    assert len(result.X) == rows
    assert result.steps[-1]["how"] == gaps
    assert not result.X.isna().any().any()


def test_interpolation_leaves_ends_and_long_gaps():
    frame = ro_frame(6)
    frame.iloc[0, 0] = np.nan
    with pytest.raises(FitError, match="still have missing values"):
        preparation.prepare(
            [selected(frame[["permeate"]])],
            [selected(frame[["power_electrical"]])],
            Prep(gaps="interpolate"),
        )
    frame.iloc[:, 0] = np.nan
    with pytest.raises(FitError, match="no segment"):
        preparation.prepare(
            [selected(frame[["permeate"]])],
            [selected(frame[["power_electrical"]])],
            Prep(gaps="longest_segment"),
        )


def test_resample_and_guards():
    frame = ro_frame(6)
    xs, ys = [selected(frame[["permeate"]])], [selected(frame[["power_electrical"]])]
    result = preparation.prepare(xs, ys, Prep(resample={"freq": "2h", "agg": "max"}))
    assert len(result.X) == 3
    assert result.steps[1]["step"] == "resample"
    with pytest.raises(FitError, match="more than max_rows=2"):
        preparation.prepare(xs, ys, Prep(max_rows=2))
    with pytest.raises(FitError, match="more than one selector"):
        preparation.prepare(xs + xs, ys, Prep())
    with pytest.raises(FitError, match="one column"):
        preparation.prepare(
            xs, ys + [selected(frame[["permeate"]].rename(columns=str.upper))], Prep()
        )
    with pytest.raises(FitError, match="No rows are left"):
        empty = frame.iloc[:0]
        preparation.prepare(
            [selected(empty[["permeate"]])],
            [selected(empty[["power_electrical"]])],
            Prep(),
        )


def test_holdout_split():
    frame = ro_frame(48)
    X_, y_ = frame[["permeate"]], frame[["power_electrical"]]
    train, _, test, _ = preparation.split_holdout(X_, y_, "12h")
    assert (len(train), len(test)) == (36, 12)
    with pytest.raises(FitError, match="Can't read holdout"):
        preparation.split_holdout(X_, y_, "fortnight")
    with pytest.raises(FitError, match="no rows to train"):
        preparation.split_holdout(X_, y_, "60D")


# Regressors


class MeanRegressor:
    """A regressor by import string: the protocol, plus predict."""

    def __init__(self, scale=1.0):
        self.scale = scale

    def fit(self, X, y):
        self.mean = float(y.iloc[:, 0].mean()) * self.scale
        self.n = len(y)
        return self

    def predict(self, X):
        return [self.mean] * len(X)

    def to_fit_result(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            coefficients={"mean": self.mean},
            metrics={"r2": 0.0},
            n_samples=self.n,
            data_window=(),
        )

    def to_surrogate_spec(self, **options):
        return {"kind": "mean", "data": {"mean": self.mean, **options}}

    def fit_diagnostics(self):
        return {"converged": True, "values": (1, np.float64(2.0)), "obj": object()}


def not_a_class():
    """Importable, callable, not a class."""


def test_regressor_resolution():
    assert regress.regressor_class("multilinear").__name__ == "LinearRegressor"
    assert regress.regressor_class(__name__ + ":MeanRegressor") is MeanRegressor
    with pytest.raises(FitError, match="Can't import regressor"):
        regress.regressor_class("nowhere.module:Thing")
    with pytest.raises(FitError, match="not a class"):
        regress.regressor_class(__name__ + ":not_a_class")
    with pytest.raises(FitError, match="Unknown regressor 'nonsense'"):
        regress.regressor_class("nonsense")
    with pytest.raises(FitError, match="installed flex-pse"):
        regress.regressor_class("quadratic")


def test_fit_a_custom_regressor_with_params(orch):
    summary = fit(
        orch,
        tag="mean",
        regressor=__name__ + ":MeanRegressor",
        params={"scale": 2.0},
        spec_options={"note": "x"},
        validation={"holdout": "12h"},
        store_series=True,
    )
    record = summary["mean"]
    assert record.coefficients["mean"] > 0
    assert set(record.holdout) == {"rmse", "mae", "mape", "rows"}
    spec = orch.surrogate_spec("mean").spec
    assert spec["data"]["note"] == "x"
    assert spec["provenance"]["fit"]["regressor"].endswith("MeanRegressor")
    [run] = store.read_all(orch)
    assert run["fit_result"]["diagnostics"]["values"] == [1, 2.0]
    assert isinstance(run["fit_result"]["diagnostics"]["obj"], str)
    assert run["fit_series"].columns == ["time", "actual", "fitted", "residual"]
    with pytest.raises(FitError, match="rejects params"):
        fit(orch, tag="m2", regressor=__name__ + ":MeanRegressor", params={"x": 1})


def test_a_multilinear_fit_is_stored_with_its_fit_run(orch):
    summary = fit(orch, tag="ro_linear", regressor="multilinear", store_series=True)
    record = summary["ro_linear"]
    assert summary.tags == ["ro_linear"]
    assert record.coefficients["permeate"] == pytest.approx(3.0, abs=0.05)
    assert record.metrics["r2"] > 0.99
    assert record.n_samples == 48
    spec = orch.surrogate_spec("ro_linear")
    assert spec.spec["data"]["input_variables"] == {"permeate": "m^3/hr"}
    assert spec.spec["data"]["output_variables"] == {"power_electrical": "kW"}
    assert spec.spec_hash == record.spec_hash
    spec_run = orch._latest("ro_linear", "surrogate_spec")
    assert spec_run.metadata["fit_run_id"] == record.run_id
    assert spec_run.metadata["data_hash"] == record.data_hash
    [fit_run] = store.read_all(orch)
    assert fit_run["tag"] == "ro_linear"
    assert fit_run["metadata"]["regressor"] == "multilinear"
    assert fit_run["training_data"]["data_hash"] == record.data_hash
    assert fit_run["training_data"]["resolved"]["X"][0]["dataset"] == "ro"
    assert fit_run["fit_params"]["X"] == {
        **X,
        "study": None,
        "result": None,
        "keys": None,
        "ref_uris": None,
        "resolve": None,
        "sparql": None,
        "start": None,
        "end": None,
        "units": None,
        "rename": {},
    }
    residual = fit_run["fit_series"]["residual"].to_list()
    assert max(abs(r) for r in residual) < 5


def test_variants_share_the_data_and_render_tags(orch):
    calls = []
    original = selectors.load
    selectors.load = lambda *a, **k: calls.append(a[1]) or original(*a, **k)
    try:
        summary = fit(
            orch,
            tag="ro_{name}",
            variants=[
                {"name": "average", "regressor": "constant_intensity"},
                {"name": "linear", "regressor": "multilinear"},
                {
                    "name": "early",
                    "regressor": "multilinear",
                    "window": {"end": START + timedelta(hours=23)},
                },
            ],
        )
    finally:
        selectors.load = original
    assert summary.tags == ["ro_average", "ro_linear", "ro_early"]
    assert len(calls) == 4  # X and y once for the full data, once for "early"
    average = orch.surrogate_spec("ro_average").spec
    assert average["surrogate_type"] == "constant_intensity"
    assert average["data"]["input_variables"] == {"permeate": "m^3/hr"}
    assert summary["ro_early"].n_samples == 24
    assert summary["ro_average"].data_hash == summary["ro_linear"].data_hash


def test_fits_on_ray_find_ray_as_studies_do(orch, monkeypatch):
    from flex_orchestrator.fitting import command

    seen = []

    def on_ray(plans, data, workers, execution, progress, address):
        seen.append(address)
        return {}  # nothing fitted there: they are fitted here

    monkeypatch.setattr(command, "_on_ray", on_ray)
    variants = [
        {"name": "a", "regressor": "multilinear"},
        {"name": "b", "regressor": "constant_intensity"},
    ]
    orch.ray_address = "ray://orch:1"  # Orchestrator(ray_address=) or the env
    fit(orch, tag="x_{name}", variants=variants, max_workers=2)
    fit(orch, tag="y_{name}", variants=variants, max_workers=2, ray_address="ray://p:1")
    assert seen == ["ray://orch:1", "ray://p:1"]
    assert orch.surrogate_spec("y_b").spec["surrogate_type"] == "constant_intensity"


def test_on_existing(orch):
    first = fit(orch, tag="ro", regressor="multilinear")
    with pytest.raises(TagExistsError):
        fit(orch, tag="ro", regressor="multilinear")
    skipped = fit(orch, tag="ro", regressor="multilinear", on_existing="skip")
    assert skipped.fits[0].status == "skipped"
    assert skipped.tags == []
    replaced = fit(orch, tag="ro", regressor="multilinear", on_existing="replace")
    assert replaced["ro"].data_hash == first["ro"].data_hash
    assert replaced["ro"].spec_hash == first["ro"].spec_hash
    assert replaced["ro"].run_id != first["ro"].run_id
    assert len(store.read_all(orch)) == 1


def test_fit_params_errors(orch):
    with pytest.raises(FitError, match="has no regressor"):
        FitParams(tag="a", X=X, y=Y).expand()
    with pytest.raises(FitError, match="put '\\{name\\}' in the tag"):
        FitParams(
            tag="a",
            X=X,
            y=Y,
            regressor="multilinear",
            variants=[{"name": "x"}, {"name": "y"}],
        ).expand()
    with pytest.raises(FitError, match="no variants"):
        FitParams(tag="a_{name}", X=X, y=Y, regressor="multilinear").expand()


def test_units_are_needed_by_flexpse_regressors(orch):
    orch.store_data("bare", ro_frame())
    with pytest.raises(FitError, match="unit of every input"):
        orch.fit_surrogate(
            tag="a",
            regressor="multilinear",
            X={"dataset": "bare", "columns": ["permeate"]},
            y={"dataset": "ro", "columns": ["power_electrical"]},
        )
    with pytest.raises(FitError, match="output's unit"):
        orch.fit_surrogate(
            tag="a",
            regressor="multilinear",
            X=X,
            y={"dataset": "bare", "columns": ["power_electrical"]},
        )


def test_fitting_failures_are_fit_errors(orch):
    class Broken(MeanRegressor):
        def fit(self, X, y):
            raise RuntimeError("diverged")

    class NoSpec(MeanRegressor):
        def to_surrogate_spec(self, **options):
            raise RuntimeError("no spec")

    frame = ro_frame(3)
    for cls, message in ((Broken, "failed: diverged"), (NoSpec, "couldn't write")):
        globals()["_Temp"] = cls
        with pytest.raises(FitError, match=message):
            regress.fit(
                __name__ + ":_Temp",
                {},
                {},
                frame[["permeate"]],
                frame[["power_electrical"]],
                {},
            )


def test_predict_needs_a_way_to_evaluate():
    fitted = regress.Fitted(
        regressor=object(),
        spec={"surrogate_type": "quadratic"},
        coefficients={},
        metrics={},
        n_samples=0,
        data_window=[],
    )
    with pytest.raises(FitError, match="Can't evaluate a 'quadratic'"):
        regress.predict(fitted, ro_frame(2)[["permeate"]])


def test_metrics():
    actual = pd.Series([0.0, 2.0, 4.0])
    found = regress.metrics(actual, pd.Series([1.0, 2.0, 2.0]))
    assert found["mae"] == pytest.approx(1.0)
    assert found["mape"] == pytest.approx(0.25)
    assert "mape" not in regress.metrics(pd.Series([0.0]), pd.Series([1.0]))


def test_a_fit_run_that_cant_be_recorded_fails(orch, monkeypatch):
    original = orch.backend.record

    def record(run_id, label, value):
        if label == store.FIT_RESULT:
            raise RuntimeError("store down")
        return original(run_id, label, value)

    monkeypatch.setattr(orch.backend, "record", record)
    with pytest.raises(RuntimeError, match="store down"):
        fit(orch, tag="ro", regressor="multilinear")
    [run] = orch.backend.runs(orch._ledger(orch.study, "fits"))
    assert run.status == "failed"
    assert store.read_all(orch) == []


def test_flexpse_types_need_flexpse(monkeypatch):
    from flex_orchestrator.errors import HookError

    def missing(ref):
        raise HookError("no flex-pse")

    monkeypatch.setattr(regress, "import_hook", missing)
    with pytest.raises(FitError, match="flex-pse isn't installed"):
        regress.regressor_class("multilinear")


def test_a_summary_indexes_by_tag():
    from flex_orchestrator.fitting import FitRecord, FitSummary

    summary = FitSummary(fits=[FitRecord(tag="a", status="skipped")])
    assert summary["a"].status == "skipped"
    with pytest.raises(KeyError):
        summary["b"]
