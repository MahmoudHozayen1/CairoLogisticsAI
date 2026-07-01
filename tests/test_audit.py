"""Tests for Slice 2: chain-of-custody audit, GIS confirmation, feedback loop.

These are fast and ML-free: the handoff hash chain, tamper detection, geofence
confirmation and the live feedback log/resolve logic are all deterministic. The
prediction *accuracy* backtest is covered by ``test_ml.py``; here we stub the
model service so the loop logic can be tested without training.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    User, Hub, Shipment, ShipmentStatus, Role,
    HandoffRecord, DeliveryConfirmation, PredictionLog, utcnow,
)
from app.audit import verify_chain, confirm_delivery_location


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        _seed()
        yield app
        db.session.remove()
        db.drop_all()


def _seed():
    hub = Hub(name="Central Hub", lat=29.9602, lon=31.2569)
    merchant = User(name="Merchant", email="m@test.io", role=Role.MERCHANT, business_name="Shop")
    merchant.set_password("merchant123")
    db.session.add_all([hub, merchant])
    db.session.flush()
    courier = User(name="Sam Courier", email="c@test.io", role=Role.COURIER,
                   hub_id=hub.id, vehicle_type="Motorcycle")
    courier.set_password("courier123")
    db.session.add(courier)
    db.session.commit()


def _make_shipment(delivered=True):
    hub = Hub.query.first()
    merchant = User.query.filter_by(role=Role.MERCHANT).first()
    courier = User.query.filter_by(role=Role.COURIER).first()
    created = utcnow() - timedelta(hours=6)
    s = Shipment(
        tracking_number="SR-TEST0001", merchant_id=merchant.id, hub_id=hub.id,
        sender_name=merchant.business_name, receiver_name="Nora",
        receiver_phone="0100000000", district="Maadi",
        lat=29.9605, lon=31.2575, weight_kg=2.0, cod_amount=0.0, created_at=created,
    )
    s.add_event(ShipmentStatus.PENDING, note="created")
    s.add_event(ShipmentStatus.AT_WAREHOUSE, location=hub.name)
    s.picked_up_at = created + timedelta(hours=1)
    s.courier_id = courier.id
    s.add_event(ShipmentStatus.OUT_FOR_DELIVERY)
    if delivered:
        s.add_event(ShipmentStatus.DELIVERED, location="Maadi")
        # Dispatch events are timestamped "now" in-test; put delivery just after
        # so the elapsed drop-off time is positive (mirrors real ordering).
        s.delivered_at = utcnow() + timedelta(minutes=40)
    db.session.add(s)
    db.session.commit()
    return s


# --------------------------------------------------------------------------- #
#  Chain of custody
# --------------------------------------------------------------------------- #
def test_handoff_chain_is_built_and_valid(app):
    s = _make_shipment(delivered=True)
    chain = verify_chain(s)
    assert chain["count"] == 3  # warehouse, courier, customer
    assert chain["ok"] is True
    stages = [i["record"].stage for i in chain["records"]]
    assert stages == ["merchant_to_hub", "hub_to_courier", "courier_to_customer"]
    # first record links to GENESIS, each subsequent to the previous hash
    recs = sorted(s.handoffs, key=lambda r: r.sequence)
    assert recs[0].prev_hash == "GENESIS"
    assert recs[1].prev_hash == recs[0].record_hash
    assert recs[2].prev_hash == recs[1].record_hash


def test_handoff_chain_survives_db_roundtrip(app):
    """Naive-UTC + microsecond hashing must verify after reload (SQLite strips tz)."""
    s = _make_shipment(delivered=True)
    sid = s.tracking_number
    db.session.expire_all()
    reloaded = Shipment.query.filter_by(tracking_number=sid).first()
    assert verify_chain(reloaded)["ok"] is True


def test_tampering_breaks_the_chain(app):
    s = _make_shipment(delivered=True)
    rec = sorted(s.handoffs, key=lambda r: r.sequence)[0]
    rec.to_party = "IMPOSTER"
    db.session.commit()
    result = verify_chain(s)
    assert result["ok"] is False
    # the altered record is flagged invalid
    assert result["records"][0]["valid"] is False


def test_no_handoff_for_pending_only(app):
    s = Shipment(tracking_number="SR-PEND", merchant_id=User.query.filter_by(role=Role.MERCHANT).first().id,
                 hub_id=Hub.query.first().id, sender_name="Shop", receiver_name="X",
                 receiver_phone="0100000000", lat=29.96, lon=31.25, weight_kg=1.0)
    s.add_event(ShipmentStatus.PENDING, note="created")
    db.session.add(s)
    db.session.commit()
    assert verify_chain(s)["count"] == 0


# --------------------------------------------------------------------------- #
#  GIS delivery confirmation
# --------------------------------------------------------------------------- #
def test_confirmation_verified_when_inside_geofence(app):
    s = _make_shipment(delivered=True)
    conf = confirm_delivery_location(s, lat=s.lat + 0.0003, lon=s.lon + 0.0003, commit=True)
    assert conf.source == "gps"
    assert conf.distance_m < conf.radius_m
    assert conf.verified is True


def test_confirmation_not_verified_when_far(app):
    s = _make_shipment(delivered=True)
    conf = confirm_delivery_location(s, lat=s.lat + 0.02, lon=s.lon + 0.02, commit=True)
    assert conf.distance_m > conf.radius_m
    assert conf.verified is False


def test_confirmation_simulated_without_coords(app):
    s = _make_shipment(delivered=True)
    conf = confirm_delivery_location(s, commit=True)
    assert conf.source == "simulated"
    assert conf.dest_lat == s.lat and conf.dest_lon == s.lon


def test_api_exposes_geo_confirmation(app):
    s = _make_shipment(delivered=True)
    confirm_delivery_location(s, lat=s.lat, lon=s.lon, commit=True)
    client = app.test_client()
    data = client.get(f"/api/track/{s.tracking_number}").get_json()
    assert data["geo_confirmation"] is not None
    assert data["geo_confirmation"]["verified"] is True


# --------------------------------------------------------------------------- #
#  Live feedback loop (model service stubbed)
# --------------------------------------------------------------------------- #
def _stub_service(monkeypatch):
    from app.ml import feedback

    class _Svc:
        is_trained = True

        def predict_all(self, shipment, when=None):
            return {
                "dropoff": {"minutes": 30.0},
                "pickup": {"minutes": 10.0},
                "late": {"probability": 0.2},
            }

        def _shipment_raw(self, shipment, when=None):
            return {"promised_minutes": 60.0}

    monkeypatch.setattr(feedback, "get_service", lambda: _Svc())
    monkeypatch.setattr("app.ml.registry.load_metrics", lambda: {"trained_at": "v-test"})
    return feedback


def test_feedback_log_and_resolve(app, monkeypatch):
    feedback = _stub_service(monkeypatch)
    s = _make_shipment(delivered=True)

    feedback.log_predictions(s)
    db.session.commit()
    logs = PredictionLog.query.filter_by(shipment_id=s.id).all()
    assert {l.kind for l in logs} == {"dropoff", "pickup", "late"}
    assert all(l.resolved_at is None for l in logs)
    assert logs[0].model_version == "v-test"

    feedback.resolve_predictions(s)
    db.session.commit()
    resolved = {l.kind: l for l in PredictionLog.query.filter_by(shipment_id=s.id).all()}
    # dropoff resolved with a numeric actual/error; late resolved as 0/1 outcome
    assert resolved["dropoff"].resolved_at is not None
    assert resolved["dropoff"].actual is not None
    assert resolved["late"].actual in (0.0, 1.0)
    # pickup is intentionally left unresolved (no clean live actual)
    assert resolved["pickup"].resolved_at is None
