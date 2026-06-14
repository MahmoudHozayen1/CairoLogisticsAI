"""End-to-end tests covering the full shipment lifecycle.

Run with::

    pytest -q
"""
import pytest

from app import create_app
from app.extensions import db
from app.models import User, Hub, Shipment, ShipmentStatus, Role
from app.utils import generate_tracking_number


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        _seed_minimal()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_minimal():
    admin = User(name="Admin", email="admin@test.io", role=Role.ADMIN)
    admin.set_password("admin12345")
    hub = Hub(name="Test Hub", lat=29.9602, lon=31.2569)
    merchant = User(name="Merchant", email="m@test.io", role=Role.MERCHANT, business_name="Shop")
    merchant.set_password("merchant123")
    db.session.add_all([admin, hub, merchant])
    db.session.flush()
    courier = User(name="Courier", email="c@test.io", role=Role.COURIER, hub_id=hub.id)
    courier.set_password("courier123")
    db.session.add(courier)
    db.session.commit()


def login(client, email, password):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


# --------------------------------------------------------------------------- #
def test_landing_and_track_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/track/").status_code == 200


def test_demo_email_domain_can_log_in(client, app):
    """Regression: reserved TLDs (.test/.example) fail WTForms Email() validation.

    Demo/seed accounts must use a real TLD or nobody can sign in via the form.
    A successful login issues a 302 redirect; a validation failure re-renders (200).
    """
    with app.app_context():
        u = User(name="Demo Admin", email="admin@swiftroute.app", role=Role.ADMIN)
        u.set_password("admin12345")
        db.session.add(u)
        db.session.commit()
    r = client.post(
        "/auth/login",
        data={"email": "admin@swiftroute.app", "password": "admin12345"},
        follow_redirects=False,
    )
    assert r.status_code == 302, "Demo email domain was rejected by the login form"


def test_register_creates_merchant(client):
    r = client.post("/auth/register", data={
        "name": "New Merchant", "business_name": "Biz", "email": "new@test.io",
        "phone": "0100000000", "password": "supersecret", "confirm": "supersecret",
    }, follow_redirects=True)
    assert r.status_code == 200
    with client.application.app_context():
        assert User.query.filter_by(email="new@test.io").first() is not None


def test_role_protection(client):
    # Unauthenticated -> redirected to login.
    assert client.get("/admin/").status_code == 302
    # Merchant cannot access admin -> 403.
    login(client, "m@test.io", "merchant123")
    assert client.get("/admin/").status_code == 403


def test_full_lifecycle(client, app):
    # Merchant creates a shipment.
    login(client, "m@test.io", "merchant123")
    r = client.post("/merchant/shipments/new", data={
        "receiver_name": "Receiver", "receiver_phone": "0111111111",
        "district": "Maadi", "address": "Road 9", "landmark": "Pharmacy",
        "lat": 29.961, "lon": 31.258, "package_description": "Box",
        "weight_kg": 2, "cod_amount": 100, "hub_id": 0,
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        s = Shipment.query.first()
        assert s is not None
        assert s.status == ShipmentStatus.PENDING
        sid = s.id

    # Admin moves it to the warehouse, then optimises.
    client.get("/auth/logout")
    login(client, "admin@test.io", "admin12345")
    client.post(f"/admin/shipments/{sid}/status", data={"status": ShipmentStatus.AT_WAREHOUSE}, follow_redirects=True)
    client.post("/admin/optimize", data={}, follow_redirects=True)

    with app.app_context():
        s = db.session.get(Shipment, sid)
        assert s.status == ShipmentStatus.OUT_FOR_DELIVERY
        assert s.courier_id is not None
        tracking = s.tracking_number

    # Courier delivers it.
    client.get("/auth/logout")
    login(client, "c@test.io", "courier123")
    client.post(f"/courier/shipment/{sid}/deliver", data={"note": "Done"}, follow_redirects=True)

    with app.app_context():
        s = db.session.get(Shipment, sid)
        assert s.status == ShipmentStatus.DELIVERED
        assert s.delivered_at is not None

    # Public + API tracking reflect the delivered state.
    client.get("/auth/logout")
    assert client.get(f"/track/?tracking_number={tracking}").status_code == 200
    api = client.get(f"/api/track/{tracking}")
    assert api.status_code == 200
    assert api.get_json()["status"] == ShipmentStatus.DELIVERED


def test_optimizer_assigns_routes(app):
    from app.routing import optimize_and_persist
    with app.app_context():
        hub = Hub.query.first()
        merchant = User.query.filter_by(role=Role.MERCHANT).first()
        for i in range(5):
            s = Shipment(
                tracking_number=generate_tracking_number(),
                merchant_id=merchant.id, hub_id=hub.id,
                sender_name="Shop", receiver_name=f"R{i}", receiver_phone="010",
                lat=29.96 + i * 0.001, lon=31.25 + i * 0.001,
                status=ShipmentStatus.AT_WAREHOUSE,
            )
            db.session.add(s)
        db.session.commit()
        summary = optimize_and_persist()
        assert summary["assigned"] == 5
        assert summary["total_distance_km"] >= 0


# --------------------------------------------------------------------------- #
#  Courier editing (admin)
# --------------------------------------------------------------------------- #
def test_admin_can_edit_courier(client, app):
    login(client, "admin@test.io", "admin12345")
    with app.app_context():
        courier = User.query.filter_by(role=Role.COURIER).first()
        hub = Hub.query.first()
        cid = courier.id

    r = client.post(f"/admin/couriers/{cid}/edit", data={
        "name": "Renamed Courier", "email": "renamed@test.io", "phone": "0102223333",
        "hub_id": hub.id, "vehicle_type": "Van",
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        c = db.session.get(User, cid)
        assert c.name == "Renamed Courier"
        assert c.email == "renamed@test.io"
        assert c.vehicle_type == "Van"


def test_edit_courier_rejects_duplicate_email(client, app):
    login(client, "admin@test.io", "admin12345")
    with app.app_context():
        courier = User.query.filter_by(role=Role.COURIER).first()
        cid = courier.id
        hub_id = Hub.query.first().id
    # Try to take the admin's email.
    client.post(f"/admin/couriers/{cid}/edit", data={
        "name": "X", "email": "admin@test.io", "hub_id": hub_id,
        "vehicle_type": "Car",
    }, follow_redirects=True)
    with app.app_context():
        c = db.session.get(User, cid)
        assert c.email != "admin@test.io"


# --------------------------------------------------------------------------- #
#  Traffic model & road closures
# --------------------------------------------------------------------------- #
def test_congestion_levels_and_overlay():
    from app.routing.street_router import build_overlay, congestion_for, LEVEL_COLORS
    from datetime import datetime

    # Rush hour is busier than the dead of night for the same place.
    busy = congestion_for(29.96, 31.25, datetime(2026, 1, 1, 9, 0))
    calm = congestion_for(29.96, 31.25, datetime(2026, 1, 1, 3, 0))
    order = ["free", "moderate", "heavy", "severe"]
    assert order.index(busy) >= order.index(calm)

    pts = [[29.96 + i * 0.001, 31.25 + i * 0.001] for i in range(12)]
    overlay = build_overlay(pts, closures=[])
    assert overlay["segments"]
    assert overlay["blocked"] is False
    assert all(seg["color"] in LEVEL_COLORS.values() for seg in overlay["segments"])


def test_overlay_flags_closure_as_blocked():
    from app.routing.street_router import build_overlay
    pts = [[29.960, 31.258], [29.9608, 31.2588], [29.961, 31.259]]
    closures = [{"id": 1, "name": "X", "reason": None, "lat": 29.9608, "lon": 31.2588, "radius_m": 200}]
    overlay = build_overlay(pts, closures=closures)
    assert overlay["blocked"] is True
    assert any(seg["blocked"] for seg in overlay["segments"])


def test_admin_closure_crud(client, app):
    login(client, "admin@test.io", "admin12345")
    r = client.post("/admin/closures", data={
        "name": "Test Closure", "reason": "Accident",
        "lat": 29.9608, "lon": 31.2588, "radius_m": 150,
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        from app.models import RoadClosure
        c = RoadClosure.query.filter_by(name="Test Closure").first()
        assert c is not None and c.is_active
        cid = c.id

    client.post(f"/admin/closures/{cid}/toggle", follow_redirects=True)
    with app.app_context():
        from app.models import RoadClosure
        assert db.session.get(RoadClosure, cid).is_active is False

    client.post(f"/admin/closures/{cid}/delete", follow_redirects=True)
    with app.app_context():
        from app.models import RoadClosure
        assert db.session.get(RoadClosure, cid) is None


def test_route_geometry_straight_provider(app):
    """With the testing provider 'straight', geometry is a direct 2-point line."""
    from app.routing import route_geometry
    with app.app_context():
        geom = route_geometry([29.96, 31.25], [29.97, 31.26], closures=[])
        assert geom["points"] == [[29.96, 31.25], [29.97, 31.26]]
        assert geom["blocked"] is False
        assert geom["distance_km"] > 0
