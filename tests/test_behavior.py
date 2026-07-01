"""Tests for Slice 5: courier behaviour modelling (persona clustering).

We verify that stop-detection recovers the deliveries hidden in a simulated GPS
trace, that K-Means recovers the ground-truth archetypes (positive adjusted-Rand),
that ``analyze`` returns a persona with explained reasoning and flags, that an
*untrained* model still degrades gracefully, and that the admin demo page renders.
Training a small model is fast and deterministic (seed=42).
"""
import numpy as np
import pytest

from app import create_app
from app.extensions import db
from app.models import User, Hub, Role
from app.ml import behavior as bh


# --------------------------------------------------------------------------- #
#  Training
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def trained():
    # Fewer shifts per archetype keeps the test fast but still separable.
    bundle, metrics = bh.train_behavior(seed=42, shifts_per_archetype=25)
    return bundle, metrics


def test_training_returns_bundle_and_metrics(trained):
    bundle, metrics = trained
    assert bundle["kind"] == "behavior_kmeans"
    assert bundle["feature_names"] == list(bh.FEATURE_NAMES)
    assert len(bundle["centroids"]) == 4
    for key in ("n_shifts", "silhouette", "adjusted_rand_vs_archetypes",
                "persona_sizes", "personas"):
        assert key in metrics


def test_clusters_recover_archetypes(trained):
    _, metrics = trained
    # KMeans should align with the four behavioural archetypes well above chance.
    assert metrics["adjusted_rand_vs_archetypes"] > 0.5
    # No collapsed/singleton clusters — every persona has real membership.
    assert min(metrics["persona_sizes"].values()) > 5
    assert len(metrics["personas"]) == 4


# --------------------------------------------------------------------------- #
#  Stop detection
# --------------------------------------------------------------------------- #
def test_stop_detection_recovers_deliveries():
    shift = bh.simulate_shift("efficient", seed=7)
    det = bh.detect_states(shift["trace"], ideal_km=shift["ideal_km"])
    n_found = det["summary"]["n_deliveries"]
    # Most of the planted deliveries should be detected as delivery-length stops.
    assert n_found >= shift["n_deliveries"] * 0.6
    assert n_found > 0
    # Every ping is labelled with a known state.
    for p in det["points"]:
        assert p["state"] in {"driving", "delivery", "idle", "break"}


def test_summary_exposes_all_features():
    shift = bh.simulate_shift("steady", seed=3)
    summary = bh.detect_states(shift["trace"], ideal_km=shift["ideal_km"])["summary"]
    for key in bh.FEATURE_NAMES:
        assert key in summary
    assert summary["detour_ratio"] >= 1.0


def test_simulate_shift_trace_is_time_ordered():
    shift = bh.simulate_shift("wanderer", seed=5)
    ts = [p["t"] for p in shift["trace"]]
    assert ts == sorted(ts)
    assert shift["ideal_km"] > 0


# --------------------------------------------------------------------------- #
#  Analysis / reasoning
# --------------------------------------------------------------------------- #
def test_analyze_returns_persona_and_reasoning(trained):
    bundle, _ = trained
    model = bh.BehaviorModel(bundle)
    shift = bh.simulate_shift("idle_prone", seed=11)
    res = model.analyze(shift["trace"], ideal_km=shift["ideal_km"])
    assert res["persona"] is not None
    assert res["persona"]["name"] in {"Efficient", "Steady", "Idle-prone", "At-risk"}
    assert 0.0 <= res["persona_confidence"] <= 1.0
    assert 0.0 <= res["productivity_score"] <= 100.0
    assert isinstance(res["flags"], list)
    assert res["reasoning"], "every score must explain itself"
    for r in res["reasoning"]:
        assert r["verdict"] in {"good", "bad", "note"}
        assert "label" in r and "z" in r


def test_efficient_outscores_idle(trained):
    bundle, _ = trained
    model = bh.BehaviorModel(bundle)
    eff = model.analyze(*_shift("efficient"))
    idle = model.analyze(*_shift("idle_prone"))
    assert eff["productivity_score"] > idle["productivity_score"]


def _shift(archetype):
    s = bh.simulate_shift(archetype, seed=21)
    return s["trace"], s["ideal_km"]


def test_untrained_model_degrades_gracefully():
    # No bundle -> no persona, but analysis must not crash and still reasons.
    model = bh.BehaviorModel()
    shift = bh.simulate_shift("steady", seed=2)
    res = model.analyze(shift["trace"], ideal_km=shift["ideal_km"])
    assert res["persona"] is None
    assert res["summary"]["n_deliveries"] >= 0
    assert isinstance(res["reasoning"], list)


def test_feature_vector_length():
    summary = bh._empty_summary()
    assert bh.feature_vector(summary).shape == (bh.N_FEATURES,)


# --------------------------------------------------------------------------- #
#  Admin demo page
# --------------------------------------------------------------------------- #
@pytest.fixture
def app(trained):
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        # Inject the pre-trained model so the endpoint doesn't retrain slowly.
        bundle, metrics = trained
        svc = bh.BehaviorService()
        svc._model = bh.BehaviorModel(bundle)
        svc._metrics = metrics
        bh._service = svc

        hub = Hub(name="Central Hub", lat=30.05, lon=31.25)
        admin = User(name="Boss", email="a@test.io", role=Role.ADMIN)
        admin.set_password("admin12345")
        db.session.add_all([hub, admin])
        db.session.flush()
        for i in range(3):
            c = User(name=f"Courier {i}", email=f"c{i}@test.io", role=Role.COURIER,
                     hub_id=hub.id, vehicle_type="Motorcycle")
            c.set_password("courier123")
            db.session.add(c)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()
        bh._service = None


def test_behavior_page_renders(app):
    client = app.test_client()
    client.post("/auth/login", data={
        "email": "a@test.io", "password": "admin12345"}, follow_redirects=True)
    resp = client.get("/admin/ai/behavior")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Courier Behaviour Modelling" in html
    assert "Fleet roster" in html
    assert "Persona mix" in html


def test_behavior_page_courier_detail(app):
    with app.app_context():
        cid = User.query.filter_by(role=Role.COURIER).first().id
    client = app.test_client()
    client.post("/auth/login", data={
        "email": "a@test.io", "password": "admin12345"}, follow_redirects=True)
    resp = client.get(f"/admin/ai/behavior?courier_id={cid}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Why this persona" in html
    assert "Time budget" in html
