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


# --------------------------------------------------------------------------- #
#  Dispatch-time planning & multiple optimisation techniques
# --------------------------------------------------------------------------- #
def test_weekend_traffic_is_lighter():
    """Cairo's weekend (Fri/Sat) should carry less simulated traffic than mid-week."""
    from app.routing.street_router import _day_factor
    assert _day_factor(4) < _day_factor(1)   # Friday < Tuesday
    assert _day_factor(5) < _day_factor(1)   # Saturday < Tuesday


def test_resolve_departure_picks_weekday_and_time():
    from app.routing import resolve_departure
    from datetime import datetime

    base = datetime(2026, 1, 5, 8, 0)  # a Monday
    dep = resolve_departure(day="Friday", hour=18, minute=0, now=base)
    assert dep.weekday() == 4               # Friday
    assert (dep.hour, dep.minute) == (18, 0)
    assert dep >= base                      # rolls forward, never into the past
    # No hour given -> "right now".
    assert resolve_departure(day="today", hour=None, now=base) == base


def test_strategy_comparison_recommends(app):
    """Every technique is estimated and the recommendation never loses to FIFO."""
    from app.routing import compare_strategies, STRATEGY_ORDER
    from datetime import datetime

    with app.app_context():
        hub = Hub.query.first()
        merchant = User.query.filter_by(role=Role.MERCHANT).first()
        # Give the courier a high-capacity vehicle so capacity isn't the variable
        # under test here (a Van holds 15; we add 8 parcels).
        User.query.filter_by(role=Role.COURIER).first().vehicle_type = "Van"
        # A deliberately scrambled set of drop-offs so FIFO is a poor order.
        offsets = [
            (0.010, 0.010), (-0.008, 0.006), (0.009, -0.007), (-0.006, -0.009),
            (0.004, 0.011), (-0.011, 0.003), (0.007, 0.002), (-0.003, -0.004),
        ]
        for i, (dlat, dlon) in enumerate(offsets):
            db.session.add(Shipment(
                tracking_number=generate_tracking_number(),
                merchant_id=merchant.id, hub_id=hub.id,
                sender_name="Shop", receiver_name=f"R{i}", receiver_phone="010",
                lat=29.96 + dlat, lon=31.25 + dlon,
                status=ShipmentStatus.AT_WAREHOUSE,
            ))
        db.session.commit()

        cmp = compare_strategies(departure=datetime(2026, 1, 6, 9, 0))
        assert [r["key"] for r in cmp["results"]] == STRATEGY_ORDER
        assert cmp["stops"] == 8
        assert all(r["duration_min"] > 0 and r["distance_km"] > 0 for r in cmp["results"])
        durations = {r["key"]: r["duration_min"] for r in cmp["results"]}
        assert durations[cmp["recommended"]] <= durations["fifo"]
        assert cmp["recommended"] in durations


def test_all_strategies_produce_valid_routes():
    """Every technique returns a valid permutation and beats the FIFO order.

    Covers the NetworkX-backed Christofides and Simulated Annealing strategies as
    well as the pure-Python heuristics, on a scrambled synthetic instance.
    """
    import random
    from app.routing.optimizer import STRATEGIES, STRATEGY_ORDER, _route_distance

    class _Stop:
        def __init__(self, coords):
            self.coords = coords

    rng = random.Random(7)
    depot = [29.96, 31.25]
    stops = [_Stop([29.96 + rng.uniform(-0.04, 0.04), 31.25 + rng.uniform(-0.04, 0.04)])
             for _ in range(12)]
    ids = sorted(id(s) for s in stops)

    fifo_dist = _route_distance(depot, STRATEGIES["fifo"]["func"](depot, stops))
    for key in STRATEGY_ORDER:
        ordered = STRATEGIES[key]["func"](depot, stops)
        assert sorted(id(s) for s in ordered) == ids, f"{key} dropped or duplicated a stop"
        if key != "fifo":
            assert _route_distance(depot, ordered) <= fifo_dist + 1e-9, f"{key} lost to FIFO"


def test_optimize_with_strategy_and_departure(app):
    """Persisting a chosen technique at a chosen time yields increasing ETAs."""
    from app.routing import optimize_and_persist
    from app.models import RouteStop
    from datetime import datetime

    with app.app_context():
        hub = Hub.query.first()
        merchant = User.query.filter_by(role=Role.MERCHANT).first()
        # Van capacity (15) comfortably holds the 6 parcels below.
        User.query.filter_by(role=Role.COURIER).first().vehicle_type = "Van"
        for i in range(6):
            db.session.add(Shipment(
                tracking_number=generate_tracking_number(),
                merchant_id=merchant.id, hub_id=hub.id,
                sender_name="Shop", receiver_name=f"R{i}", receiver_phone="010",
                lat=29.96 + i * 0.002, lon=31.25 + i * 0.0015,
                status=ShipmentStatus.AT_WAREHOUSE,
            ))
        db.session.commit()

        summary = optimize_and_persist(strategy="nearest", departure=datetime(2026, 1, 6, 18, 0))
        assert summary["assigned"] == 6
        assert summary["strategy"] == "nearest"
        assert summary["strategy_label"]
        etas = [rs.eta_minutes for rs in RouteStop.query.order_by(RouteStop.sequence).all()]
        assert etas == sorted(etas)         # ETAs accumulate along the route
        assert etas[-1] > 0


def test_vehicle_capacity_caps_assignment(app):
    """A courier is never assigned more parcels than their vehicle can carry."""
    from app.routing import optimize_and_persist
    from app.models import Vehicle
    from datetime import datetime

    with app.app_context():
        hub = Hub.query.first()
        merchant = User.query.filter_by(role=Role.MERCHANT).first()
        courier = User.query.filter_by(role=Role.COURIER).first()
        courier.vehicle_type = Vehicle.MOTORCYCLE          # capacity 5
        # 8 parcels, one motorcycle -> 5 assigned, 3 left for next round.
        for i in range(8):
            db.session.add(Shipment(
                tracking_number=generate_tracking_number(),
                merchant_id=merchant.id, hub_id=hub.id,
                sender_name="Shop", receiver_name=f"R{i}", receiver_phone="010",
                lat=29.96 + i * 0.001, lon=31.25 + i * 0.001,
                status=ShipmentStatus.AT_WAREHOUSE,
            ))
        db.session.commit()

        summary = optimize_and_persist(departure=datetime(2026, 1, 6, 11, 0))
        assert summary["assigned"] == Vehicle.CAPACITY[Vehicle.MOTORCYCLE]   # 5
        assert summary["unassigned"] == 3
        # No single courier route exceeds the vehicle capacity.
        for route in summary["routes"]:
            assert len(route["stops"]) <= Vehicle.CAPACITY[Vehicle.MOTORCYCLE]


# --------------------------------------------------------------------------- #
#  Admin: create shipment + filter the shipments table
# --------------------------------------------------------------------------- #
def test_admin_can_create_shipment(client, app):
    login(client, "admin@test.io", "admin12345")
    with app.app_context():
        merchant = User.query.filter_by(role=Role.MERCHANT).first()
        mid = merchant.id

    r = client.post("/admin/shipments/new", data={
        "merchant_id": mid, "receiver_name": "Admin Receiver", "receiver_phone": "0123456789",
        "district": "Maadi", "address": "Road 9", "landmark": "Pharmacy",
        "lat": 29.962, "lon": 31.259, "package_description": "Box",
        "weight_kg": 1, "cod_amount": 50, "hub_id": 0,
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        s = Shipment.query.filter_by(receiver_name="Admin Receiver").first()
        assert s is not None
        assert s.merchant_id == mid
        assert s.hub_id is not None          # auto-assigned nearest hub
        assert s.status == ShipmentStatus.PENDING


def test_admin_shipments_filter_by_merchant_and_zone(client, app):
    login(client, "admin@test.io", "admin12345")
    with app.app_context():
        hub = Hub.query.first()
        m1 = User.query.filter_by(role=Role.MERCHANT).first()
        m2 = User(name="Other", email="other@test.io", role=Role.MERCHANT, business_name="Other Co")
        m2.set_password("merchant123")
        db.session.add(m2)
        db.session.flush()
        db.session.add(Shipment(
            tracking_number=generate_tracking_number(), merchant_id=m1.id, hub_id=hub.id,
            sender_name="A", receiver_name="Zamalek Person", receiver_phone="011",
            district="Zamalek", lat=29.96, lon=31.22, status=ShipmentStatus.PENDING,
        ))
        db.session.add(Shipment(
            tracking_number=generate_tracking_number(), merchant_id=m2.id, hub_id=hub.id,
            sender_name="B", receiver_name="Maadi Person", receiver_phone="012",
            district="Maadi", lat=29.96, lon=31.25, status=ShipmentStatus.PENDING,
        ))
        db.session.commit()
        m1_id, m2_id = m1.id, m2.id

    # Filter by merchant 2 -> only their parcel.
    r = client.get(f"/admin/shipments?merchant_id={m2_id}")
    assert b"Maadi Person" in r.data and b"Zamalek Person" not in r.data
    # Filter by zone -> only that district.
    r = client.get("/admin/shipments?district=Zamalek")
    assert b"Zamalek Person" in r.data and b"Maadi Person" not in r.data
    # Search by receiver name.
    r = client.get("/admin/shipments?q=Maadi+Person")
    assert b"Maadi Person" in r.data and b"Zamalek Person" not in r.data
