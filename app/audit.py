"""Chain-of-custody verification and GIS delivery confirmation.

* :func:`verify_chain` recomputes a shipment's handoff hash chain and reports
  whether it is intact (tamper-evident audit).
* :func:`confirm_delivery_location` records where a parcel was confirmed
  delivered and whether that point falls inside the destination geofence.
"""
from __future__ import annotations

import math
import random

from .extensions import db
from .models import DeliveryConfirmation, handoff_hash

DEFAULT_RADIUS_M = 200


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres between two lat/lon points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))



def verify_chain(shipment) -> dict:
    """Recompute the handoff hash chain and report its integrity.

    Returns ``{"ok", "count", "records": [{"record", "valid"}...]}``. ``ok`` is
    True only if every record's stored hash matches a fresh recomputation and
    each ``prev_hash`` links correctly to the record before it.
    """
    records = sorted(shipment.handoffs, key=lambda r: r.sequence)
    prev = "GENESIS"
    ok = True
    results = []
    for r in records:
        expected = handoff_hash(shipment.tracking_number, r.sequence, r.stage,
                                r.from_party, r.to_party, r.created_at, prev)
        valid = (expected == r.record_hash) and (r.prev_hash == prev)
        ok = ok and valid
        results.append({"record": r, "valid": valid})
        prev = r.record_hash
    return {"ok": ok, "count": len(records), "records": results}


def confirm_delivery_location(shipment, lat=None, lon=None, radius_m=DEFAULT_RADIUS_M,
                              commit=False):
    """Create (or replace) the GIS delivery confirmation for a shipment.

    If ``lat``/``lon`` are omitted (e.g. the courier device gave no GPS fix), a
    plausible point is simulated near the destination so the confirmation still
    demonstrates the geofence check; the record is flagged ``source="simulated"``.
    """
    dest_lat, dest_lon = shipment.lat, shipment.lon
    source = "gps"
    if lat is None or lon is None:
        # Simulate a delivery fix: usually within the geofence, occasionally out.
        jitter = 0.0016 if random.random() < 0.82 else 0.0045
        lat = dest_lat + random.uniform(-jitter, jitter)
        lon = dest_lon + random.uniform(-jitter, jitter)
        source = "simulated"

    distance_m = _haversine_km(lat, lon, dest_lat, dest_lon) * 1000.0
    verified = distance_m <= radius_m

    existing = shipment.delivery_confirmation
    if existing:
        db.session.delete(existing)
        db.session.flush()

    conf = DeliveryConfirmation(
        shipment_id=shipment.id, lat=lat, lon=lon,
        dest_lat=dest_lat, dest_lon=dest_lon,
        distance_m=round(distance_m, 1), radius_m=radius_m,
        verified=verified, source=source,
    )
    db.session.add(conf)
    if commit:
        db.session.commit()
    return conf
