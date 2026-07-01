from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from . import bp
from ...extensions import db
from ...forms import ShipmentForm
from ...models import Shipment, Hub, ShipmentStatus, Role
from ...utils import role_required, generate_tracking_number

merchant_only = role_required(Role.MERCHANT)


@bp.route("/")
@login_required
@merchant_only
def dashboard():
    q = Shipment.query.filter_by(merchant_id=current_user.id)
    stats = {
        "total": q.count(),
        "delivered": q.filter_by(status=ShipmentStatus.DELIVERED).count(),
        "in_transit": q.filter(Shipment.status.in_([
            ShipmentStatus.AT_WAREHOUSE, ShipmentStatus.OUT_FOR_DELIVERY,
        ])).count(),
        "pending": q.filter_by(status=ShipmentStatus.PENDING).count(),
    }
    recent = q.order_by(Shipment.created_at.desc()).limit(10).all()
    return render_template("merchant/dashboard.html", stats=stats, recent=recent)


@bp.route("/shipments")
@login_required
@merchant_only
def shipments():
    page = request.args.get("page", 1, type=int)
    pagination = (
        Shipment.query.filter_by(merchant_id=current_user.id)
        .order_by(Shipment.created_at.desc())
        .paginate(page=page, per_page=15, error_out=False)
    )
    return render_template(
        "merchant/shipments.html",
        pagination=pagination, shipments=pagination.items,
    )


@bp.route("/shipments/new", methods=["GET", "POST"])
@login_required
@merchant_only
def create_shipment():
    form = ShipmentForm()
    hubs = Hub.query.order_by(Hub.name).all()
    form.hub_id.choices = [(0, "Auto-assign nearest hub")] + [(h.id, h.name) for h in hubs]

    if form.validate_on_submit():
        coords = [form.lat.data, form.lon.data]
        hub_id = form.hub_id.data
        if not hub_id:  # auto-assign nearest hub by distance
            hub_id = _nearest_hub_id(coords)

        shipment = Shipment(
            tracking_number=_unique_tracking_number(),
            merchant_id=current_user.id,
            hub_id=hub_id,
            sender_name=current_user.display_name,
            sender_phone=current_user.phone,
            receiver_name=form.receiver_name.data,
            receiver_phone=form.receiver_phone.data,
            district=form.district.data,
            address=form.address.data,
            landmark=form.landmark.data,
            lat=form.lat.data,
            lon=form.lon.data,
            package_description=form.package_description.data,
            weight_kg=form.weight_kg.data or 1.0,
            cod_amount=form.cod_amount.data or 0.0,
            delivery_notes=(form.delivery_notes.data or "").strip() or None,
            status=ShipmentStatus.PENDING,
        )
        shipment.add_event(ShipmentStatus.PENDING, note="Shipment created", user=current_user)
        db.session.add(shipment)
        db.session.commit()
        flash(f"Shipment created. Tracking number: {shipment.tracking_number}", "success")
        return redirect(url_for("merchant.shipment_detail", shipment_id=shipment.id))

    return render_template("merchant/create_shipment.html", form=form, hubs=hubs)


@bp.route("/shipments/<int:shipment_id>")
@login_required
@merchant_only
def shipment_detail(shipment_id):
    s = db.get_or_404(Shipment, shipment_id)
    if s.merchant_id != current_user.id:
        flash("That shipment doesn't belong to your account.", "warning")
        return redirect(url_for("merchant.shipments"))
    return render_template("merchant/shipment_detail.html", s=s)


@bp.route("/shipments/<int:shipment_id>/cancel", methods=["POST"])
@login_required
@merchant_only
def cancel_shipment(shipment_id):
    s = db.get_or_404(Shipment, shipment_id)
    if s.merchant_id != current_user.id:
        flash("That shipment doesn't belong to your account.", "warning")
        return redirect(url_for("merchant.shipments"))
    if s.status in (ShipmentStatus.PENDING, ShipmentStatus.AT_WAREHOUSE):
        s.add_event(ShipmentStatus.CANCELLED, note="Cancelled by merchant", user=current_user)
        db.session.commit()
        flash("Shipment cancelled.", "info")
    else:
        flash("Only pending shipments can be cancelled.", "warning")
    return redirect(url_for("merchant.shipment_detail", shipment_id=shipment_id))


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _unique_tracking_number():
    for _ in range(10):
        tn = generate_tracking_number()
        if not Shipment.query.filter_by(tracking_number=tn).first():
            return tn
    return generate_tracking_number()


def _nearest_hub_id(coords):
    from ...routing import haversine_km
    hubs = Hub.query.all()
    if not hubs:
        return None
    return min(hubs, key=lambda h: haversine_km(coords, h.coords)).id
