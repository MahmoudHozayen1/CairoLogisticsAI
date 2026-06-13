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

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db, login_manager


def utcnow():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
#  Roles & statuses
# --------------------------------------------------------------------------- #
class Role:
    ADMIN = "admin"
    COURIER = "courier"
    MERCHANT = "merchant"
    ALL = (ADMIN, COURIER, MERCHANT)


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
