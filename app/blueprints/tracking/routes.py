from flask import render_template, request

from . import bp
from ...models import Shipment, RouteStop
from ...routing import haversine_km


@bp.route("/", methods=["GET", "POST"])
def track():
    """Public, no-login tracking by tracking number."""
    shipment = None
    not_found = False
    eta = None

    code = (request.values.get("tracking_number") or "").strip().upper()
    if code:
        shipment = Shipment.query.filter_by(tracking_number=code).first()
        if shipment is None:
            not_found = True
        else:
            rs = RouteStop.query.filter_by(shipment_id=shipment.id).first()
            if rs and rs.eta_minutes:
                low = max(5, rs.eta_minutes - 10)
                eta = f"{low}–{rs.eta_minutes + 10} min"

    return render_template(
        "tracking/track.html",
        shipment=shipment, not_found=not_found, code=code, eta=eta,
    )
