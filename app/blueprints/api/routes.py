"""Lightweight JSON API.

* ``GET /api/track/<tracking_number>`` – public parcel status + timeline.
* ``GET /api/shipments``               – the signed-in user's shipments (session auth).
* ``GET /api/stats``                   – network KPIs (admin only).

The API is CSRF-exempt (registered in the app factory) because it is meant for
programmatic JSON access rather than browser forms.
"""
from flask import jsonify
from flask_login import login_required, current_user

from . import bp
from ...models import Shipment, ShipmentStatus, Role


def _shipment_public(s):
    return {
        "tracking_number": s.tracking_number,
        "status": s.status,
        "status_label": s.status_label,
        "receiver_name": s.receiver_name,
        "district": s.district,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "delivered_at": s.delivered_at.isoformat() if s.delivered_at else None,
        "timeline": [
            {
                "status": e.status,
                "label": e.status_label,
                "note": e.note,
                "location": e.location,
                "at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in s.events
        ],
    }


@bp.get("/track/<tracking_number>")
def track(tracking_number):
    s = Shipment.query.filter_by(tracking_number=tracking_number.strip().upper()).first()
    if not s:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_shipment_public(s))


@bp.get("/shipments")
@login_required
def my_shipments():
    if current_user.role == Role.MERCHANT:
        items = Shipment.query.filter_by(merchant_id=current_user.id).all()
    elif current_user.role == Role.COURIER:
        items = Shipment.query.filter_by(courier_id=current_user.id).all()
    else:
        items = Shipment.query.all()
    return jsonify({"count": len(items), "shipments": [_shipment_public(s) for s in items]})


@bp.get("/stats")
@login_required
def stats():
    if current_user.role != Role.ADMIN:
        return jsonify({"error": "forbidden"}), 403
    return jsonify({
        "total": Shipment.query.count(),
        "delivered": Shipment.query.filter_by(status=ShipmentStatus.DELIVERED).count(),
        "out_for_delivery": Shipment.query.filter_by(status=ShipmentStatus.OUT_FOR_DELIVERY).count(),
        "pending": Shipment.query.filter_by(status=ShipmentStatus.PENDING).count(),
    })
