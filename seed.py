"""Seed the database with realistic demo data.

Run with::

    flask --app run seed      # within the app context (preferred)
    python seed.py            # standalone

Creates an admin, two hubs, couriers, merchants and a spread of shipments in
various lifecycle states so every dashboard has something to show.
"""
import random
from datetime import datetime, timedelta, timezone

from app import create_app
from app.extensions import db
from app.models import User, Hub, Shipment, ShipmentStatus, Role
from app.utils import generate_tracking_number

# Demo delivery points around Maadi / Cairo.
DELIVERY_POINTS = [
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

FIRST_NAMES = ["Ahmed", "Sara", "Mohamed", "Nour", "Omar", "Mariam", "Youssef", "Laila", "Khaled", "Hana"]


def _maybe_clear():
    """Wipe existing rows so reseeding is idempotent."""
    db.session.query(Shipment).delete()
    from app.models import ShipmentEvent, RouteStop
    db.session.query(RouteStop).delete()
    db.session.query(ShipmentEvent).delete()
    db.session.query(User).delete()
    db.session.query(Hub).delete()
    db.session.commit()


def seed_data():
    _maybe_clear()

    # --- Admin -----------------------------------------------------------
    admin = User(name="Site Administrator", email="admin@swiftroute.app", role=Role.ADMIN, phone="0100000000")
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
            name=name, email=f"courier{i}@swiftroute.app", role=Role.COURIER,
            hub_id=hub.id, vehicle_type=vehicle, phone=f"0101111111{i}",
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
            name=name, email=f"merchant{i}@swiftroute.app", role=Role.MERCHANT,
            business_name=biz, phone=f"0102222222{i}",
        )
        m.set_password("merchant123")
        merchants.append(m)
        db.session.add(m)

    db.session.flush()

    # --- Shipments -------------------------------------------------------
    rng = random.Random(7)
    statuses_plan = (
        [ShipmentStatus.PENDING] * 4
        + [ShipmentStatus.AT_WAREHOUSE] * 6
        + [ShipmentStatus.OUT_FOR_DELIVERY] * 3
        + [ShipmentStatus.DELIVERED] * 5
        + [ShipmentStatus.FAILED] * 1
    )

    for idx, status in enumerate(statuses_plan):
        point = rng.choice(DELIVERY_POINTS)
        merchant = rng.choice(merchants)
        created = datetime.now(timezone.utc) - timedelta(days=rng.randint(0, 6), hours=rng.randint(0, 20))
        s = Shipment(
            tracking_number=generate_tracking_number(),
            merchant_id=merchant.id,
            hub_id=maadi.id if point[1] < 30.0 else nasr.id,
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
            created_at=created,
        )
        # Build a plausible event history up to the target status.
        s.add_event(ShipmentStatus.PENDING, note="Shipment created")
        if status in (ShipmentStatus.AT_WAREHOUSE, ShipmentStatus.OUT_FOR_DELIVERY,
                      ShipmentStatus.DELIVERED, ShipmentStatus.FAILED):
            s.add_event(ShipmentStatus.AT_WAREHOUSE, note="Received & sorted at hub", location="Hub")
            s.picked_up_at = created + timedelta(hours=2)
        if status in (ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.DELIVERED, ShipmentStatus.FAILED):
            courier = rng.choice(couriers)
            s.courier_id = courier.id
            s.add_event(ShipmentStatus.OUT_FOR_DELIVERY, note=f"Dispatched with {courier.name}")
        if status == ShipmentStatus.DELIVERED:
            s.add_event(ShipmentStatus.DELIVERED, note="Delivered successfully", location=point[0])
            s.delivered_at = created + timedelta(hours=6)
        if status == ShipmentStatus.FAILED:
            s.delivery_attempts = 1
            s.add_event(ShipmentStatus.FAILED, note="Recipient unavailable")
        s.status = status
        db.session.add(s)

    db.session.commit()
    print("Seeded:")
    print("  Admin     -> admin@swiftroute.app / admin12345")
    print("  Courier   -> courier1@swiftroute.app / courier123")
    print("  Merchant  -> merchant1@swiftroute.app / merchant123")
    print(f"  {len(statuses_plan)} shipments, 2 hubs, {len(couriers)} couriers.")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        db.create_all()
        seed_data()
