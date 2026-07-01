"""Seed the database with realistic demo data.

Run with::

    flask --app run seed      # within the app context (preferred)
    python seed.py            # standalone

Creates an admin, two hubs, couriers, merchants and a spread of shipments in
various lifecycle states so every dashboard has something to show. The dispatch
workload is intentionally larger and scrambled so the map and route comparison
make optimisation savings easy to see.
"""
import random
from datetime import datetime, timedelta, timezone

from app import create_app
from app.extensions import db
from app.models import User, Hub, Shipment, ShipmentStatus, Role
from app.routing import optimize_and_persist, resolve_departure
from app.utils import generate_tracking_number

# Demo delivery points around Maadi / Cairo.
MAADI_DELIVERY_POINTS = [
    ("Grand Mall", 29.9575, 31.2650, "Near the cinema entrance"),
    ("Degla Square", 29.9650, 31.2600, "Beside the metro"),
    ("Corniche Maadi", 29.9500, 31.2400, "Riverside building 12"),
    ("Victory College", 29.9700, 31.2700, "Main gate"),
    ("Road 9", 29.9605, 31.2585, "Above the pharmacy"),
    ("Maadi Sarayat", 29.9630, 31.2520, "Villa 4, second floor"),
    ("Zahraa El Maadi", 29.9420, 31.2900, "Block 7, flat 21"),
    ("New Maadi", 29.9480, 31.2780, "Next to the bank"),
    ("Thakanat", 29.9550, 31.2480, "Opposite the school"),
    ("Hadayek El Maadi", 29.9700, 31.2820, "Tower B, apartment 9"),
]

NASR_DELIVERY_POINTS = [
    ("City Stars", 30.0735, 31.3460, "Gate 4"),
    ("Abbas El Akkad", 30.0566, 31.3300, "Office tower lobby"),
    ("Makram Ebeid", 30.0612, 31.3447, "Mall entrance"),
    ("Tenth District", 30.0438, 31.3650, "Building 18"),
    ("Al Ahly Club", 30.0674, 31.3155, "Main reception"),
    ("Child Park", 30.0644, 31.3255, "North gate"),
    ("Heliopolis Square", 30.0915, 31.3220, "Pharmacy corner"),
    ("Roxy", 30.0919, 31.3117, "Cinema building"),
    ("Rabaa Square", 30.0661, 31.3383, "Clinic entrance"),
    ("Mostafa El Nahas", 30.0497, 31.3480, "Tower C"),
]

DELIVERY_POINTS = MAADI_DELIVERY_POINTS + NASR_DELIVERY_POINTS

# A deliberately scrambled dispatch workload. Insertion order is the FIFO
# baseline, so alternating near/far points makes the optimisation savings clear.
DISPATCH_STOPS = [
    ("maadi", "Corniche Maadi", 29.9490, 31.2385, "Riverside building 7"),
    ("maadi", "Zahraa El Maadi", 29.9410, 31.2920, "Block 3"),
    ("maadi", "Thakanat", 29.9560, 31.2470, "Opposite the school"),
    ("maadi", "Hadayek El Maadi", 29.9720, 31.2840, "Tower B"),
    ("maadi", "Road 9", 29.9600, 31.2580, "Above the pharmacy"),
    ("maadi", "New Maadi", 29.9470, 31.2795, "Bank entrance"),
    ("maadi", "Maadi Sarayat", 29.9640, 31.2515, "Villa 4"),
    ("maadi", "Grand Mall", 29.9570, 31.2665, "Cinema entrance"),
    ("maadi", "Corniche Maadi", 29.9520, 31.2415, "Building 19"),
    ("maadi", "Zahraa El Maadi", 29.9440, 31.2865, "Block 12"),
    ("maadi", "Degla Square", 29.9660, 31.2605, "Coffee shop"),
    ("maadi", "Victory College", 29.9710, 31.2715, "Main gate"),
    ("maadi", "Thakanat", 29.9540, 31.2495, "School side street"),
    ("maadi", "Hadayek El Maadi", 29.9680, 31.2810, "Tower D"),
    ("maadi", "Road 9", 29.9620, 31.2560, "Bookstore"),
    ("nasr", "City Stars", 30.0745, 31.3470, "Gate 4"),
    ("nasr", "Tenth District", 30.0425, 31.3665, "Building 18"),
    ("nasr", "Roxy", 30.0925, 31.3105, "Cinema building"),
    ("nasr", "Mostafa El Nahas", 30.0485, 31.3490, "Tower C"),
    ("nasr", "Al Ahly Club", 30.0680, 31.3150, "Main reception"),
    ("nasr", "Makram Ebeid", 30.0620, 31.3455, "Mall entrance"),
    ("nasr", "Heliopolis Square", 30.0905, 31.3230, "Pharmacy corner"),
    ("nasr", "Abbas El Akkad", 30.0560, 31.3315, "Office tower lobby"),
    ("nasr", "Rabaa Square", 30.0650, 31.3375, "Clinic entrance"),
    ("nasr", "City Stars", 30.0715, 31.3445, "Food court side"),
    ("nasr", "Tenth District", 30.0450, 31.3630, "Building 22"),
    ("nasr", "Child Park", 30.0635, 31.3265, "North gate"),
    ("nasr", "Roxy", 30.0895, 31.3130, "Side street"),
    ("nasr", "Makram Ebeid", 30.0590, 31.3420, "Clinic tower"),
    ("nasr", "Mostafa El Nahas", 30.0515, 31.3510, "Tower A"),
]

FIRST_NAMES = [
    "Ahmed", "Sara", "Mohamed", "Nour", "Omar",
    "Mariam", "Youssef", "Laila", "Khaled", "Hana",
]

# Free-text delivery instructions used to demo the NLP handling-notes model.
# Deliberately varied phrasing (not just the training fragments) so the learned
# classifier — not only the regexes — is exercised end to end.
DEMO_NOTES = [
    "Fragile, please handle with care — contains glass.",
    "Don't stack anything on top of this box.",
    "Deliver between 6 and 9 pm only, I'm at work before that.",
    "Leave with the doorman if I don't answer.",
    "Please call before arriving, the bell is broken.",
    "4th floor, no lift — sorry for the climb!",
    "Cash ready, exact change for the COD.",
    "Meet me at the building gate, I'll come down.",
    "Fragile items inside, deliver in the morning and ring the bell twice.",
    "Concierge at reception can receive it, do not stack.",
    "",  # some parcels carry no notes
    "",
]


def _maybe_clear():
    """Wipe existing rows so reseeding is idempotent."""
    from app.models import (
        ShipmentEvent, RouteStop, RoadClosure,
        HandoffRecord, DeliveryConfirmation, PredictionLog,
    )

    # Child rows referencing shipments must go before the shipments themselves.
    db.session.query(PredictionLog).delete()
    db.session.query(DeliveryConfirmation).delete()
    db.session.query(HandoffRecord).delete()
    db.session.query(RouteStop).delete()
    db.session.query(ShipmentEvent).delete()
    db.session.query(RoadClosure).delete()
    db.session.query(Shipment).delete()
    db.session.query(User).delete()
    db.session.query(Hub).delete()
    db.session.commit()


def seed_data():
    _maybe_clear()

    # --- Admin -----------------------------------------------------------
    admin = User(
        name="Site Administrator",
        email="admin@swiftroute.app",
        role=Role.ADMIN,
        phone="0100000000",
    )
    admin.set_password("admin12345")
    db.session.add(admin)

    # --- Hubs ------------------------------------------------------------
    maadi = Hub(name="Maadi Hub", address="Maadi Station, Cairo", lat=29.9602, lon=31.2569)
    nasr = Hub(name="Nasr City Hub", address="Abbas El Akkad, Cairo", lat=30.0566, lon=31.3300)
    db.session.add_all([maadi, nasr])
    db.session.flush()

    # --- Couriers --------------------------------------------------------
    couriers = []
    for i, (name, hub, vehicle) in enumerate([
        ("Mostafa Driver", maadi, "Motorcycle"),
        ("Aya Rider", maadi, "Car"),
        ("Tarek Wheels", nasr, "Van"),
    ], start=1):
        c = User(
            name=name,
            email=f"courier{i}@swiftroute.app",
            role=Role.COURIER,
            hub_id=hub.id,
            vehicle_type=vehicle,
            phone=f"0101111111{i}",
        )
        c.set_password("courier123")
        couriers.append(c)
        db.session.add(c)

    # --- Merchants -------------------------------------------------------
    merchants = []
    for i, (name, biz) in enumerate([
        ("Jumia Seller", "Jumia Store"),
        ("Noon Vendor", "Noon Marketplace"),
    ], start=1):
        m = User(
            name=name,
            email=f"merchant{i}@swiftroute.app",
            role=Role.MERCHANT,
            business_name=biz,
            phone=f"0102222222{i}",
        )
        m.set_password("merchant123")
        merchants.append(m)
        db.session.add(m)

    db.session.flush()

    # --- Historical / dashboard shipments -------------------------------
    rng = random.Random(7)
    history_statuses = (
        [ShipmentStatus.PENDING] * 6
        + [ShipmentStatus.DELIVERED] * 8
        + [ShipmentStatus.FAILED] * 2
    )

    delivered_shipments = []
    for status in history_statuses:
        point = rng.choice(DELIVERY_POINTS)
        merchant = rng.choice(merchants)
        hub = maadi if point in MAADI_DELIVERY_POINTS else nasr
        created = datetime.now(timezone.utc) - timedelta(
            days=rng.randint(0, 6),
            hours=rng.randint(0, 20),
        )
        s = Shipment(
            tracking_number=generate_tracking_number(),
            merchant_id=merchant.id,
            hub_id=hub.id,
            sender_name=merchant.business_name,
            sender_phone=merchant.phone,
            receiver_name=rng.choice(FIRST_NAMES),
            receiver_phone=f"0109{rng.randint(1000000, 9999999)}",
            district=point[0],
            address=f"{point[0]}, Cairo",
            landmark=point[3],
            lat=point[1] + rng.uniform(-0.004, 0.004),
            lon=point[2] + rng.uniform(-0.004, 0.004),
            package_description=rng.choice(["Electronics", "Clothing", "Books", "Cosmetics", "Documents"]),
            weight_kg=round(rng.uniform(0.3, 6.0), 1),
            cod_amount=rng.choice([0, 0, 150, 320, 480, 1200]),
            delivery_notes=(rng.choice(DEMO_NOTES) or None),
            created_at=created,
        )
        s.add_event(ShipmentStatus.PENDING, note="Shipment created")
        if status in (ShipmentStatus.DELIVERED, ShipmentStatus.FAILED):
            s.add_event(ShipmentStatus.AT_WAREHOUSE, note="Received & sorted at hub", location=hub.name)
            s.picked_up_at = created + timedelta(hours=2)
            courier_pool = [c for c in couriers if c.hub_id == hub.id]
            courier = rng.choice(courier_pool)
            s.courier_id = courier.id
            s.add_event(ShipmentStatus.OUT_FOR_DELIVERY, note=f"Dispatched with {courier.name}")
        if status == ShipmentStatus.DELIVERED:
            s.add_event(ShipmentStatus.DELIVERED, note="Delivered successfully", location=point[0])
            s.delivered_at = created + timedelta(hours=6)
            delivered_shipments.append(s)
        if status == ShipmentStatus.FAILED:
            s.delivery_attempts = 1
            s.add_event(ShipmentStatus.FAILED, note="Recipient unavailable")
        s.status = status
        db.session.add(s)

    # --- Optimizable dispatch workload ----------------------------------
    dispatch_created = datetime.now(timezone.utc) - timedelta(hours=3)
    for idx, (hub_key, district, lat, lon, landmark) in enumerate(DISPATCH_STOPS):
        merchant = merchants[idx % len(merchants)]
        hub = maadi if hub_key == "maadi" else nasr
        s = Shipment(
            tracking_number=generate_tracking_number(),
            merchant_id=merchant.id,
            hub_id=hub.id,
            sender_name=merchant.business_name,
            sender_phone=merchant.phone,
            receiver_name=FIRST_NAMES[idx % len(FIRST_NAMES)],
            receiver_phone=f"0108{rng.randint(1000000, 9999999)}",
            district=district,
            address=f"{district}, Cairo",
            landmark=landmark,
            lat=lat,
            lon=lon,
            package_description=rng.choice(["Electronics", "Clothing", "Books", "Cosmetics", "Documents"]),
            weight_kg=round(rng.uniform(0.4, 7.5), 1),
            cod_amount=rng.choice([0, 150, 240, 320, 480, 850, 1200]),
            delivery_notes=(DEMO_NOTES[idx % len(DEMO_NOTES)] or None),
            created_at=dispatch_created + timedelta(minutes=idx),
        )
        s.add_event(ShipmentStatus.PENDING, note="Shipment created")
        s.add_event(ShipmentStatus.AT_WAREHOUSE, note="Received & sorted at hub", location=hub.name)
        s.picked_up_at = dispatch_created + timedelta(hours=1, minutes=idx)
        s.status = ShipmentStatus.AT_WAREHOUSE
        db.session.add(s)

    # --- A demo road closure near a busy delivery area -------------------
    from app.models import RoadClosure
    db.session.add(RoadClosure(
        name="Road 9 - construction",
        reason="Road works, expect detours",
        lat=29.9608,
        lon=31.2588,
        radius_m=180,
    ))

    db.session.commit()

    # --- GIS delivery confirmations for delivered demo shipments ---------
    from app.audit import confirm_delivery_location
    for s in delivered_shipments:
        confirm_delivery_location(s)  # simulates a point near the destination
    db.session.commit()

    # Pre-populate the map and courier dashboards. Tuesday morning produces
    # rush-hour ETAs, and auto picks the fastest technique for the workload.
    departure = resolve_departure(day="Tuesday", hour=9)
    summary = optimize_and_persist(departure=departure, strategy="auto")

    print("Seeded:")
    print("  Admin     -> admin@swiftroute.app / admin12345")
    print("  Courier   -> courier1@swiftroute.app / courier123")
    print("  Merchant  -> merchant1@swiftroute.app / merchant123")
    print(
        f"  {len(history_statuses) + len(DISPATCH_STOPS)} shipments, 2 hubs, "
        f"{len(couriers)} couriers, 1 road closure."
    )
    print(
        f"  Pre-optimised {summary['assigned']} stops across {len(summary['routes'])} routes "
        f"with {summary['strategy_label']}."
    )


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        db.create_all()
        seed_data()
