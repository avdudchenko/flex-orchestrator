"""fit_surrogate against a local Acquirium: general-store streams and fit runs (D19).

The stream selectors read streams registered in Acquirium's general store
(a stand-in for a plant historian), and fit runs, their series table and the
spec's link to them are stored in Acquirium's experiment ledger.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from flex_orchestrator.backend import AcquiriumBackend
from flex_orchestrator.errors import FitError
from flex_orchestrator.fitting import store
from flex_orchestrator.orchestrator import Orchestrator

pytest.importorskip("flexparameterize")
pytestmark = pytest.mark.component

START = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def historian(acquirium_local):
    """Two streams in a fresh datasource: feed flow (m3/h) and biogas (m3/day)."""
    key = uuid.uuid4().hex[:8]
    source = f"historian_{key}"
    acquirium_local.register_datasource(source)
    acquirium_local.register_streams(
        [
            {
                "point_uri": f"urn:test:{key}:feed",
                "source_id": source,
                "ref_name": f"FT_{key}_feed",
                "label": f"feed flow {key}",
                "unit": "M3-PER-HR",
                "value_kind": "numeric",
            },
            {
                "point_uri": f"urn:test:{key}:biogas",
                "source_id": source,
                "ref_name": f"FT_{key}_biogas",
                "label": f"biogas volume {key}",
                "unit": "M3-PER-DAY",
                "value_kind": "numeric",
            },
        ]
    )
    stamps = [START + timedelta(hours=h) for h in range(24)]
    feed = [10.0 + h for h in range(24)]
    acquirium_local.insert_timeseries(
        source, f"FT_{key}_feed", list(zip(stamps, feed, strict=True))
    )
    acquirium_local.insert_timeseries(
        source,
        f"FT_{key}_biogas",
        [(t, 0.5 * f * 24) for t, f in zip(stamps, feed, strict=True)],
    )
    refs = {
        name: str(acquirium_local.stream(source, f"FT_{key}_{name}"))
        for name in ("feed", "biogas")
    }
    return {"key": key, "refs": refs}


@pytest.fixture
def aq_orch(acquirium_local, study_name):
    orch = Orchestrator(AcquiriumBackend(acquirium_local), environ={})
    orch.create_study(study_name)
    return orch


def test_fit_from_general_store_streams(aq_orch, historian):
    key, refs = historian["key"], historian["refs"]
    summary = aq_orch.fit_surrogate(
        tag="biogas",
        regressor="multilinear",
        X={"ref_uris": [refs["feed"]], "rename": {"*": "flow_in"}},
        y={
            "resolve": f"biogas volume {key}",
            "rename": {"*": "biogas_volume"},
            "units": "m^3/hr",
        },
        store_series=True,
    )
    record = summary["biogas"]
    assert record.coefficients["flow_in"] == pytest.approx(0.5)
    assert record.n_samples == 24
    spec = aq_orch.surrogate_spec("biogas").spec
    assert spec["data"]["input_variables"] == {"flow_in": "meter**3/hour"}
    assert spec["data"]["output_variables"] == {"biogas_volume": "m^3/hr"}
    [fit] = store.read_all(aq_orch)
    resolved = fit["training_data"]["resolved"]
    assert resolved["X"][0]["refs"] == {"flow_in": refs["feed"]}
    assert resolved["y"][0]["refs"] == {"biogas_volume": refs["biogas"]}
    assert fit["fit_series"].height == 24
    run = aq_orch._latest("biogas", "surrogate_spec")
    assert run.metadata["fit_run_id"] == record.run_id


def test_sparql_and_resolve_against_acquirium(aq_orch, historian):
    key, refs = historian["key"], historian["refs"]
    query = f"""PREFIX ref: <https://brickschema.org/schema/Brick/ref#>
SELECT ?ref WHERE {{ <urn:test:{key}:feed> ref:hasExternalReference ?ref }}"""
    found = aq_orch.backend.sparql_select(query)
    assert found["rows"] == [[refs["feed"]]]
    info = aq_orch.backend.stream_info([refs["feed"]])
    assert info[refs["feed"]] == {
        "name": f"FT_{key}_feed",
        "label": f"feed flow {key}",
        "unit": "M3-PER-HR",
    }
    assert aq_orch.backend.stream_info([]) == {}
    assert [s["ref"] for s in aq_orch.backend.find_streams(f"FEED FLOW {key}")] == [
        refs["feed"]
    ]
    assert len(aq_orch.backend.find_streams(key)) == 2
    rows = aq_orch.backend.stream_rows(
        refs["feed"], START + timedelta(hours=2), START + timedelta(hours=3)
    )
    assert [v for _, v in rows] == [12.0, 13.0]
    assert aq_orch.backend.stream_rows(refs["feed"], START + timedelta(days=9)) == []
    summary = aq_orch.fit_surrogate(
        tag="feed_mean",
        regressor="constant_intensity",
        X={"sparql": query},
        y={"ref_uris": [refs["biogas"]]},
    )
    assert summary["feed_mean"].coefficients["energy_intensity"] == pytest.approx(12.0)
    with pytest.raises(FitError, match="matched 2 streams"):
        aq_orch.fit_surrogate(
            tag="x", regressor="multilinear", X={"resolve": key}, y={"resolve": key}
        )
