import json
from datetime import datetime, timezone

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from . import bp
from ...extensions import db
from ...forms import DeliveryUpdateForm
from ...models import Shipment, RouteStop, ShipmentStatus, Role
from ...utils import role_required, save_proof_image

courier_only = role_required(Role.COURIER)


def _my_active_shipments():
    return (
        Shipment.query.filter(
            Shipment.courier_id == current_user.id,
            Shipment.status.in_([ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.AT_WAREHOUSE]),
        )
        .order_by(Shipment.route_sequence.asc().nullslast())
        .all()
    )


@bp.route("/")
@login_required
@courier_only
def dashboard():
    active = _my_active_shipments()
    delivered_today = Shipment.query.filter(
        Shipment.courier_id == current_user.id,
        Shipment.status == ShipmentStatus.DELIVERED,
    ).count()
    stats = {
        "active": len(active),
        "delivered_total": delivered_today,
        "cod_to_collect": round(sum(s.cod_amount or 0 for s in active), 2),
    }
    return render_template("courier/dashboard.html", shipments=active, stats=stats)


@bp.route("/route")
@login_required
@courier_only
def route_map():
    active = _my_active_shipments()
    stops = []
    for s in active:
        rs = RouteStop.query.filter_by(shipment_id=s.id).first()
        stops.append({
            "tracking_number": s.tracking_number,
            "receiver": s.receiver_name,
            "coords": s.coords,
            "sequence": s.route_sequence or 0,
            "path": json.loads(rs.path_json) if rs and rs.path_json else [],
            "eta": rs.eta_minutes if rs else None,
        })
    hub = current_user.hub
    return render_template("courier/route_map.html", stops=stops, hub=hub)


@bp.route("/shipment/<int:shipment_id>", methods=["GET", "POST"])
@login_required
@courier_only
def shipment_detail(shipment_id):
    s = db.get_or_404(Shipment, shipment_id)
    if s.courier_id != current_user.id:
        flash("This shipment is not assigned to you.", "warning")
        return redirect(url_for("courier.dashboard"))

    form = DeliveryUpdateForm()
    return render_template("courier/shipment_detail.html", s=s, form=form)


@bp.route("/shipment/<int:shipment_id>/deliver", methods=["POST"])
@login_required
@courier_only
def mark_delivered(shipment_id):
    s = db.get_or_404(Shipment, shipment_id)
    if s.courier_id != current_user.id:
        flash("This shipment is not assigned to you.", "warning")
        return redirect(url_for("courier.dashboard"))

    form = DeliveryUpdateForm()
    if form.validate_on_submit():
        filename = save_proof_image(form.proof.data, s.tracking_number)
        if filename:
            s.proof_image = filename
        s.delivered_at = datetime.now(timezone.utc)
        s.add_event(
            ShipmentStatus.DELIVERED,
            note=form.note.data or "Delivered successfully",
            location=s.district or s.address,
            user=current_user,
        )
        # Free up the route slot.
        RouteStop.query.filter_by(shipment_id=s.id).delete()
        db.session.commit()
        flash(f"{s.tracking_number} marked delivered.", "success")
    else:
        flash("Could not save delivery. Check the form.", "danger")
    return redirect(url_for("courier.dashboard"))


@bp.route("/shipment/<int:shipment_id>/fail", methods=["POST"])
@login_required
@courier_only
def mark_failed(shipment_id):
    s = db.get_or_404(Shipment, shipment_id)
    if s.courier_id != current_user.id:
        flash("This shipment is not assigned to you.", "warning")
        return redirect(url_for("courier.dashboard"))

    note = request.form.get("note") or "Recipient unavailable"
    s.delivery_attempts = (s.delivery_attempts or 0) + 1
    s.add_event(ShipmentStatus.FAILED, note=note, user=current_user)
    RouteStop.query.filter_by(shipment_id=s.id).delete()
    db.session.commit()
    flash(f"{s.tracking_number} marked as failed delivery.", "warning")
    return redirect(url_for("courier.dashboard"))


@bp.route("/availability", methods=["POST"])
@login_required
@courier_only
def toggle_availability():
    current_user.is_available = not current_user.is_available
    db.session.commit()
    flash(
        "You are now " + ("available" if current_user.is_available else "off duty") + ".",
        "info",
    )
    return redirect(url_for("courier.dashboard"))
