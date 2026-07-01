"""Database models for the SwiftRoute logistics platform.

Entities
--------
* ``User``          – a single table holding admins, couriers and merchants
                      (differentiated by ``role``).
* ``Hub``           – a warehouse / distribution centre with a fleet of couriers.
* ``Shipment``      – a parcel moving through the network, with a full lifecycle.
* ``ShipmentEvent`` – an immutable audit-trail entry for a shipment (the tracking
                      timeline the customer sees).
* ``RouteStop``     – a persisted stop in an optimised courier route, including the
                      polyline geometry used to draw the route on the map.
"""
from datetime import datetime, timezone

import hashlib

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db, login_manager


def utcnow():
    return datetime.now(timezone.utc)


def naive_utcnow():
    """Naive UTC timestamp (tz stripped) for values that must hash-compare
    identically before and after a database round-trip (SQLite drops tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
#  Chain-of-custody handoff helpers
# --------------------------------------------------------------------------- #
# Which status transitions represent a custody handoff, and the default parties.
HANDOFF_MAP = {
    "at_warehouse": ("merchant_to_hub", "Merchant", "Hub"),
    "out_for_delivery": ("hub_to_courier", "Hub", "Courier"),
    "delivered": ("courier_to_customer", "Courier", "Customer"),
    "returned": ("courier_to_hub_return", "Courier", "Hub"),
}


def handoff_payload(tracking_number, sequence, stage, from_party, to_party,
                    created_at, prev_hash):
    """Canonical string hashed for a handoff record (used by create + verify)."""
    ts = created_at.isoformat(timespec="microseconds") if created_at else ""
    return f"{tracking_number}|{sequence}|{stage}|{from_party}|{to_party}|{ts}|{prev_hash}"


def handoff_hash(*args):
    return hashlib.sha256(handoff_payload(*args).encode("utf-8")).hexdigest()



# --------------------------------------------------------------------------- #
#  Roles & statuses
# --------------------------------------------------------------------------- #
class Role:
    ADMIN = "admin"
    COURIER = "courier"
    MERCHANT = "merchant"
    ALL = (ADMIN, COURIER, MERCHANT)


class Vehicle:
    """Courier vehicle types and how many parcels each can carry per route.

    Capacity caps are enforced by the route optimiser: a courier is never
    assigned more stops than their vehicle can hold, and parcels beyond the total
    fleet capacity are left unassigned (rather than silently overloading a bike).
    """
    MOTORCYCLE = "Motorcycle"
    CAR = "Car"
    VAN = "Van"
    BICYCLE = "Bicycle"

    CAPACITY = {
        BICYCLE: 3,
        MOTORCYCLE: 5,
        CAR: 10,
        VAN: 15,
    }
    DEFAULT_CAPACITY = 5

    # (value, label) pairs for select inputs, in ascending-capacity order.
    CHOICES = [
        (BICYCLE, f"Bicycle (max {CAPACITY[BICYCLE]})"),
        (MOTORCYCLE, f"Motorcycle (max {CAPACITY[MOTORCYCLE]})"),
        (CAR, f"Car (max {CAPACITY[CAR]})"),
        (VAN, f"Van (max {CAPACITY[VAN]})"),
    ]

    @classmethod
    def capacity(cls, vehicle_type) -> int:
        return cls.CAPACITY.get((vehicle_type or "").strip(), cls.DEFAULT_CAPACITY)


class ShipmentStatus:
    """Lifecycle of a parcel, modelled on Bosta/DHL milestones."""
    PENDING = "pending"            # created, awaiting pickup from merchant
    AT_WAREHOUSE = "at_warehouse"  # received & sorted at a hub
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    FAILED = "failed"              # delivery attempt failed
    RETURNED = "returned"          # returned to merchant
    CANCELLED = "cancelled"

    ORDER = [PENDING, AT_WAREHOUSE, OUT_FOR_DELIVERY, DELIVERED]

    LABELS = {
        PENDING: "Pending Pickup",
        AT_WAREHOUSE: "At Warehouse",
        OUT_FOR_DELIVERY: "Out for Delivery",
        DELIVERED: "Delivered",
        FAILED: "Delivery Failed",
        RETURNED: "Returned to Sender",
        CANCELLED: "Cancelled",
    }

    # Bootstrap colour for badges in the UI.
    COLORS = {
        PENDING: "secondary",
        AT_WAREHOUSE: "info",
        OUT_FOR_DELIVERY: "primary",
        DELIVERED: "success",
        FAILED: "danger",
        RETURNED: "dark",
        CANCELLED: "dark",
    }

    @classmethod
    def label(cls, status):
        return cls.LABELS.get(status, status.title() if status else "Unknown")

    @classmethod
    def color(cls, status):
        return cls.COLORS.get(status, "secondary")


# --------------------------------------------------------------------------- #
#  User
# --------------------------------------------------------------------------- #
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(32))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.MERCHANT, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    # Merchant-specific
    business_name = db.Column(db.String(160))

    # Courier-specific
    hub_id = db.Column(db.Integer, db.ForeignKey("hubs.id"))
    vehicle_type = db.Column(db.String(40), default="Motorcycle")
    is_available = db.Column(db.Boolean, default=True, nullable=False)

    hub = db.relationship("Hub", backref="couriers", foreign_keys=[hub_id])
    shipments_created = db.relationship(
        "Shipment", backref="merchant", lazy="dynamic",
        foreign_keys="Shipment.merchant_id",
    )
    shipments_assigned = db.relationship(
        "Shipment", backref="courier", lazy="dynamic",
        foreign_keys="Shipment.courier_id",
    )

    # --- helpers ---
    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_courier(self):
        return self.role == Role.COURIER

    @property
    def is_merchant(self):
        return self.role == Role.MERCHANT

    @property
    def display_name(self):
        if self.is_merchant and self.business_name:
            return self.business_name
        return self.name

    @property
    def route_capacity(self):
        """Maximum parcels this courier can carry on one route (by vehicle)."""
        return Vehicle.capacity(self.vehicle_type)

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --------------------------------------------------------------------------- #
#  Hub (warehouse)
# --------------------------------------------------------------------------- #
class Hub(db.Model):
    __tablename__ = "hubs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(255))
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    shipments = db.relationship("Shipment", backref="hub", lazy="dynamic")

    @property
    def coords(self):
        return [self.lat, self.lon]

    @property
    def courier_count(self):
        return len([c for c in self.couriers if c.is_active])

    def __repr__(self):
        return f"<Hub {self.name}>"


# --------------------------------------------------------------------------- #
#  Shipment
# --------------------------------------------------------------------------- #
class Shipment(db.Model):
    __tablename__ = "shipments"

    id = db.Column(db.Integer, primary_key=True)
    tracking_number = db.Column(db.String(24), unique=True, nullable=False, index=True)

    merchant_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    hub_id = db.Column(db.Integer, db.ForeignKey("hubs.id"), index=True)
    courier_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)

    # Sender (merchant) snapshot
    sender_name = db.Column(db.String(120), nullable=False)
    sender_phone = db.Column(db.String(32))

    # Receiver
    receiver_name = db.Column(db.String(120), nullable=False)
    receiver_phone = db.Column(db.String(32), nullable=False)
    district = db.Column(db.String(120))
    address = db.Column(db.String(255))
    landmark = db.Column(db.String(255))
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)

    # Parcel
    package_description = db.Column(db.String(255))
    weight_kg = db.Column(db.Float, default=1.0)
    cod_amount = db.Column(db.Float, default=0.0)  # cash on delivery (EGP)

    status = db.Column(db.String(30), default=ShipmentStatus.PENDING, index=True)
    delivery_attempts = db.Column(db.Integer, default=0)
    delivery_notes = db.Column(db.Text)
    proof_image = db.Column(db.String(255))  # filename under static/uploads

    # Optimised route ordering (set by the AI optimizer)
    route_sequence = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    picked_up_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)

    events = db.relationship(
        "ShipmentEvent", backref="shipment", lazy="dynamic",
        cascade="all, delete-orphan", order_by="ShipmentEvent.created_at",
    )
    route_stop = db.relationship(
        "RouteStop", backref="shipment", uselist=False,
        cascade="all, delete-orphan",
    )
    handoffs = db.relationship(
        "HandoffRecord", backref="shipment", lazy="select",
        cascade="all, delete-orphan", order_by="HandoffRecord.sequence",
    )
    delivery_confirmation = db.relationship(
        "DeliveryConfirmation", backref="shipment", uselist=False,
        cascade="all, delete-orphan",
    )
    predictions = db.relationship(
        "PredictionLog", backref="shipment", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def coords(self):
        return [self.lat, self.lon]

    @property
    def status_label(self):
        return ShipmentStatus.label(self.status)

    @property
    def status_color(self):
        return ShipmentStatus.color(self.status)

    @property
    def is_active(self):
        return self.status not in (
            ShipmentStatus.DELIVERED,
            ShipmentStatus.RETURNED,
            ShipmentStatus.CANCELLED,
        )

    def add_event(self, status, note=None, location=None, user=None):
        """Append an immutable timeline entry and update the current status."""
        self.status = status
        self.events.append(
            ShipmentEvent(
                status=status,
                note=note,
                location=location,
                created_by=(user.name if user else "System"),
            )
        )
        self._record_handoff(status, location, user)

    def _handoff_parties(self, stage, default_from, default_to):
        """Resolve human-readable custody parties from the shipment context."""
        frm, to = default_from, default_to

        def _hub():
            if self.hub:
                return self.hub
            return db.session.get(Hub, self.hub_id) if self.hub_id else None

        def _courier():
            if self.courier:
                return self.courier
            return db.session.get(User, self.courier_id) if self.courier_id else None

        def _merchant():
            if self.merchant:
                return self.merchant
            return db.session.get(User, self.merchant_id) if self.merchant_id else None

        try:
            if stage == "merchant_to_hub":
                m, h = _merchant(), _hub()
                frm = (m.display_name if m else None) or self.sender_name or default_from
                to = h.name if h else default_to
            elif stage == "hub_to_courier":
                h, c = _hub(), _courier()
                frm = h.name if h else default_from
                to = c.name if c else default_to
            elif stage == "courier_to_customer":
                c = _courier()
                frm = c.name if c else default_from
                to = self.receiver_name or default_to
            elif stage == "courier_to_hub_return":
                c, h = _courier(), _hub()
                frm = c.name if c else default_from
                to = h.name if h else default_to
        except Exception:  # pragma: no cover - never block a status change
            pass
        return frm, to

    def _record_handoff(self, status, location, user):
        """Append a tamper-evident chain-of-custody record for custody transfers."""
        mapping = HANDOFF_MAP.get(status)
        if not mapping:
            return
        stage, default_from, default_to = mapping
        existing = list(self.handoffs)
        seq = len(existing) + 1
        prev_hash = existing[-1].record_hash if existing else "GENESIS"
        from_party, to_party = self._handoff_parties(stage, default_from, default_to)
        ts = naive_utcnow()
        rec_hash = handoff_hash(self.tracking_number, seq, stage, from_party,
                                to_party, ts, prev_hash)
        self.handoffs.append(HandoffRecord(
            sequence=seq, stage=stage, from_party=from_party, to_party=to_party,
            location=location, actor=(user.name if user else "System"),
            created_at=ts, prev_hash=prev_hash, record_hash=rec_hash,
            verified=bool(location),
        ))

    def __repr__(self):
        return f"<Shipment {self.tracking_number} {self.status}>"


# --------------------------------------------------------------------------- #
#  Shipment timeline event
# --------------------------------------------------------------------------- #
class ShipmentEvent(db.Model):
    __tablename__ = "shipment_events"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(255))
    location = db.Column(db.String(160))
    created_by = db.Column(db.String(120), default="System")
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def status_label(self):
        return ShipmentStatus.label(self.status)

    @property
    def status_color(self):
        return ShipmentStatus.color(self.status)


# --------------------------------------------------------------------------- #
#  Persisted optimised route stop (map geometry)
# --------------------------------------------------------------------------- #
class RouteStop(db.Model):
    __tablename__ = "route_stops"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False, index=True)
    hub_id = db.Column(db.Integer, db.ForeignKey("hubs.id"))
    courier_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    sequence = db.Column(db.Integer, default=0)
    # JSON list of [lat, lon] points describing the path from the previous stop.
    path_json = db.Column(db.Text)
    eta_minutes = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)


# --------------------------------------------------------------------------- #
#  Road closure / blocked road (admin-managed)
# --------------------------------------------------------------------------- #
class RoadClosure(db.Model):
    """A circular area an admin marks as closed/blocked.

    Closures are shown on every map and are actively avoided by the route
    optimiser (it re-routes the drawn geometry around them). Any route leg that
    still passes through an active closure is flagged in red on the map.
    """
    __tablename__ = "road_closures"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    reason = db.Column(db.String(255))
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    radius_m = db.Column(db.Integer, default=150, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def coords(self):
        return [self.lat, self.lon]

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "reason": self.reason,
            "lat": self.lat,
            "lon": self.lon,
            "radius_m": self.radius_m,
        }

    @classmethod
    def active(cls):
        return cls.query.filter_by(is_active=True).all()

    def __repr__(self):
        return f"<RoadClosure {self.name} r={self.radius_m}m active={self.is_active}>"


# --------------------------------------------------------------------------- #
#  Chain-of-custody handoff (tamper-evident ledger)
# --------------------------------------------------------------------------- #
class HandoffRecord(db.Model):
    """One custody transfer in a shipment's chain of custody.

    Each record stores a SHA-256 ``record_hash`` computed over its own fields and
    the previous record's hash (``prev_hash``), forming a hash chain. Altering any
    earlier record breaks every subsequent hash, so tampering is detectable via
    :func:`app.audit.verify_chain`.
    """
    __tablename__ = "handoff_records"

    STAGE_LABELS = {
        "merchant_to_hub": "Merchant → Hub",
        "hub_to_courier": "Hub → Courier",
        "courier_to_customer": "Courier → Customer",
        "courier_to_hub_return": "Courier → Hub (return)",
    }

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    stage = db.Column(db.String(40), nullable=False)
    from_party = db.Column(db.String(120))
    to_party = db.Column(db.String(120))
    location = db.Column(db.String(160))
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    actor = db.Column(db.String(120), default="System")
    prev_hash = db.Column(db.String(64), nullable=False, default="GENESIS")
    record_hash = db.Column(db.String(64), nullable=False)
    verified = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=naive_utcnow)

    @property
    def stage_label(self):
        return self.STAGE_LABELS.get(self.stage, self.stage.replace("_", " ").title())

    def __repr__(self):
        return f"<HandoffRecord {self.stage} seq={self.sequence}>"


# --------------------------------------------------------------------------- #
#  GIS delivery confirmation (geofence check by tracking number)
# --------------------------------------------------------------------------- #
class DeliveryConfirmation(db.Model):
    """Where a parcel was actually confirmed delivered, vs. its destination.

    ``distance_m`` is the great-circle gap between the confirmed point and the
    receiver's location; ``verified`` is True when that gap is within ``radius_m``.
    """
    __tablename__ = "delivery_confirmations"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), unique=True, nullable=False, index=True)
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    dest_lat = db.Column(db.Float, nullable=False)
    dest_lon = db.Column(db.Float, nullable=False)
    distance_m = db.Column(db.Float, nullable=False)
    radius_m = db.Column(db.Integer, default=200, nullable=False)
    verified = db.Column(db.Boolean, default=False, nullable=False)
    source = db.Column(db.String(20), default="gps")  # gps | simulated | manual
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def coords(self):
        return [self.lat, self.lon]

    def __repr__(self):
        return f"<DeliveryConfirmation ship={self.shipment_id} verified={self.verified}>"


# --------------------------------------------------------------------------- #
#  Predicted-vs-actual feedback log (the learning feedback loop)
# --------------------------------------------------------------------------- #
class PredictionLog(db.Model):
    """A prediction made for a shipment, resolved against the actual outcome.

    ``kind`` is one of ``dropoff`` / ``pickup`` / ``late``. ``predicted`` is the
    model output at dispatch time; ``actual`` and ``error`` are filled in when the
    shipment completes, closing the feedback loop.
    """
    __tablename__ = "prediction_logs"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, index=True)
    predicted = db.Column(db.Float, nullable=False)
    actual = db.Column(db.Float)
    error = db.Column(db.Float)
    model_version = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=utcnow)
    resolved_at = db.Column(db.DateTime)

    @property
    def resolved(self):
        return self.resolved_at is not None

    def __repr__(self):
        return f"<PredictionLog {self.kind} pred={self.predicted} actual={self.actual}>"
