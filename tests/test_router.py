"""Tests for Slice 4: the learning-to-route pointer policy (neural router).

Training a small policy is fast and deterministic (seed=42). We verify the
REINFORCE loop actually learns (the optimality gap shrinks and it beats the
nearest-neighbour heuristic), that decoding always yields a valid permutation
with per-step reasoning, that an *untrained* policy still returns a usable tour
(deploy-safe fallback), and that the admin demo page renders.
"""
import numpy as np
import pytest

from app import create_app
from app.extensions import db
from app.models import User, Hub, Shipment, ShipmentStatus, Role
from app.ml import neural_router as nr


# --------------------------------------------------------------------------- #
#  Training
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def trained():
    # Small, fast config — still enough for the policy to learn the structure.
    bundle, metrics = nr.train_router(iterations=40, batch=32, lr=0.05, seed=42)
    return bundle, metrics


def test_training_returns_bundle_and_metrics(trained):
    bundle, metrics = trained
    assert bundle["kind"] == "route_policy"
    assert len(bundle["theta"]) == nr.N_FEATURES
    assert bundle["feature_names"] == list(nr.FEATURE_NAMES)
    for key in ("val_gap_vs_two_opt_pct", "val_sampled_gap_vs_two_opt_pct",
                "val_improve_vs_nn_pct", "learning_curve_gap_pct", "weights"):
        assert key in metrics


def test_training_reduces_optimality_gap(trained):
    _, metrics = trained
    curve = metrics["learning_curve_gap_pct"]
    # The gap after training is far below the untrained starting point.
    assert curve[-1] < curve[0]
    assert curve[-1] < 20.0


def test_learned_policy_beats_nearest_neighbour(trained):
    _, metrics = trained
    # Best-of-N sampled decoding should not be worse than the greedy heuristic.
    assert metrics["val_improve_vs_nn_pct"] >= 0.0


# --------------------------------------------------------------------------- #
#  Decoding / reasoning
# --------------------------------------------------------------------------- #
POINTS = [
    (30.06, 31.25), (30.10, 31.20), (30.02, 31.30), (30.08, 31.35),
    (30.05, 31.22), (30.12, 31.28), (30.00, 31.24), (30.07, 31.19),
]
DEPOT = (30.05, 31.25)


def test_route_returns_valid_permutation(trained):
    bundle, _ = trained
    policy = nr.RoutePolicy(np.asarray(bundle["theta"]))
    res = policy.route(POINTS, DEPOT, samples=16)
    assert sorted(res["order"]) == list(range(len(POINTS)))
    assert res["stops"] == len(POINTS)
    assert res["length_km"] > 0
    assert len(res["steps"]) == len(POINTS)


def test_route_steps_carry_reasoning(trained):
    bundle, _ = trained
    policy = nr.RoutePolicy(np.asarray(bundle["theta"]))
    res = policy.route(POINTS, DEPOT, samples=8)
    for step in res["steps"]:
        assert 0.0 <= step["probability"] <= 1.0
        assert step["reasons"], "each pick must explain itself"
        for r in step["reasons"]:
            assert "label" in r and "weight" in r


def test_route_reports_baselines(trained):
    bundle, _ = trained
    policy = nr.RoutePolicy(np.asarray(bundle["theta"]))
    res = policy.route(POINTS, DEPOT, samples=16)
    b = res["baselines"]
    assert b["nearest_neighbour_km"] > 0
    assert b["two_opt_km"] > 0
    # 2-opt is a local optimum, so never longer than nearest-neighbour.
    assert b["two_opt_km"] <= b["nearest_neighbour_km"] + 1e-6
    assert sorted(b["nearest_neighbour_order"]) == list(range(len(POINTS)))


def test_untrained_policy_still_routes():
    # Zero weights -> uniform policy. Must still produce a valid tour so routing
    # never breaks even if the artifact is missing.
    policy = nr.RoutePolicy()
    res = policy.route(POINTS, DEPOT, samples=8)
    assert sorted(res["order"]) == list(range(len(POINTS)))
    assert res["length_km"] > 0


def test_empty_points():
    policy = nr.RoutePolicy()
    res = policy.route([], DEPOT)
    assert res["order"] == []
    assert res["length_km"] == 0.0


# --------------------------------------------------------------------------- #
#  Distance helpers
# --------------------------------------------------------------------------- #
def test_haversine_matrix_properties():
    coords = np.array([[30.0, 31.0], [30.1, 31.1], [29.9, 31.2]])
    D = nr._haversine_matrix(coords)
    assert np.allclose(np.diag(D), 0.0)
    assert np.allclose(D, D.T)
    assert D[0, 1] > 0


# --------------------------------------------------------------------------- #
#  Admin demo page
# --------------------------------------------------------------------------- #
@pytest.fixture
def app(trained):
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        # Inject the pre-trained policy so the endpoint doesn't retrain slowly.
        bundle, metrics = trained
        router = nr.NeuralRouter()
        router._policy = nr.RoutePolicy(np.asarray(bundle["theta"]))
        router._metrics = metrics
        nr._router = router

        hub = Hub(name="Central Hub", lat=30.05, lon=31.25)
        admin = User(name="Boss", email="a@test.io", role=Role.ADMIN)
        admin.set_password("admin12345")
        merchant = User(name="Merchant", email="m@test.io", role=Role.MERCHANT,
                        business_name="Shop")
        merchant.set_password("merchant123")
        db.session.add_all([hub, admin, merchant])
        db.session.flush()
        for i, (lat, lon) in enumerate(POINTS):
            db.session.add(Shipment(
                tracking_number=f"SR-RT{i:05d}", merchant_id=merchant.id,
                hub_id=hub.id, sender_name="Shop", receiver_name=f"Cust {i}",
                receiver_phone="0100000000", district="Maadi", lat=lat, lon=lon,
                weight_kg=1.5, status=ShipmentStatus.OUT_FOR_DELIVERY,
            ))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()
        nr._router = None


def test_router_page_renders(app):
    client = app.test_client()
    client.post("/auth/login", data={
        "email": "a@test.io", "password": "admin12345"}, follow_redirects=True)
    resp = client.get("/admin/ai/router")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Learning-to-Route" in html
    assert "Neural tour" in html
    assert "Pick order" in html


def test_router_page_needs_enough_stops(app):
    # Delete down to 2 stops -> the page shows the guidance message, not a plan.
    with app.app_context():
        extra = Shipment.query.filter_by(status=ShipmentStatus.OUT_FOR_DELIVERY).all()[2:]
        for s in extra:
            db.session.delete(s)
        db.session.commit()
    client = app.test_client()
    client.post("/auth/login", data={
        "email": "a@test.io", "password": "admin12345"}, follow_redirects=True)
    resp = client.get("/admin/ai/router")
    assert resp.status_code == 200
    assert "at least 3 parcels" in resp.get_data(as_text=True)
