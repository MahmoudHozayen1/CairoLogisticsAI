import json
from datetime import datetime, timedelta, timezone

from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from . import bp
from ...extensions import db
from ...forms import HubForm, CourierForm, RoadClosureForm
from ...models import (
    User, Hub, Shipment, RouteStop, RoadClosure, ShipmentStatus, Role,
)
from ...utils import role_required
from ...routing import (
    optimize_and_persist, build_overlay, active_closure_dicts,
    compare_strategies, resolve_departure, STRATEGIES, STRATEGY_ORDER,
    WEEKDAY_LABELS, LEVEL_COLORS, LEVEL_LABELS,
)

admin_only = role_required(Role.ADMIN)

# Quick-pick departure times for dispatch planning (form value -> menu label).
TIME_PRESETS = [
    ("", "Now"),
    ("06:00", "06:00 · Early"),
    ("09:00", "09:00 · Morning rush"),
    ("13:00", "13:00 · Midday"),
    ("16:00", "16:00 · Afternoon"),
    ("18:00", "18:00 · Evening rush"),
    ("21:00", "21:00 · Night"),
]


def _planning_inputs():
    """Read the dispatch-planning controls (day / time / technique).

    Works for both the GET map (query string) and the POST optimise form, so a
    previewed plan and the dispatched plan stay in sync. Returns
    ``(departure, day, time_raw, strategy, label)``.
    """
    day = (request.values.get("day") or "today").strip()
    time_raw = (request.values.get("time") or "").strip()
    strategy = (request.values.get("strategy") or "auto").strip()

    hour = minute = None
    if ":" in time_raw:
        hh, _, mm = time_raw.partition(":")
        if hh.isdigit() and mm.isdigit():
            hour, minute = int(hh), int(mm)
    elif time_raw.isdigit():
        hour, minute = int(time_raw), 0

    departure = resolve_departure(day=day, hour=hour, minute=(minute or 0))
    if time_raw == "":
        label = "now (" + departure.strftime("%a %H:%M") + ")"
    else:
        label = departure.strftime("%A %H:%M")
    return departure, day, time_raw, strategy, label


# --------------------------------------------------------------------------- #
#  Dashboard & analytics
# --------------------------------------------------------------------------- #
@bp.route("/")
@login_required
@admin_only
def dashboard():
    totals = {
        "shipments": Shipment.query.count(),
        "delivered": Shipment.query.filter_by(status=ShipmentStatus.DELIVERED).count(),
        "in_transit": Shipment.query.filter(
            Shipment.status.in_([ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.AT_WAREHOUSE])
        ).count(),
        "pending": Shipment.query.filter_by(status=ShipmentStatus.PENDING).count(),
        "failed": Shipment.query.filter_by(status=ShipmentStatus.FAILED).count(),
        "hubs": Hub.query.count(),
        "couriers": User.query.filter_by(role=Role.COURIER, is_active=True).count(),
        "merchants": User.query.filter_by(role=Role.MERCHANT).count(),
    }
    delivered = totals["delivered"]
    closed = delivered + Shipment.query.filter(
        Shipment.status.in_([ShipmentStatus.RETURNED, ShipmentStatus.CANCELLED, ShipmentStatus.FAILED])
    ).count()
    totals["success_rate"] = round((delivered / closed) * 100, 1) if closed else 0.0

    # Status breakdown for the chart.
    status_counts = dict(
        db.session.query(Shipment.status, func.count(Shipment.id))
        .group_by(Shipment.status).all()
    )
    status_chart = {
        "labels": [ShipmentStatus.label(s) for s in status_counts],
        "data": list(status_counts.values()),
        "colors": [ShipmentStatus.color(s) for s in status_counts],
    }

    # Shipments per day for the last 7 days.
    today = datetime.now(timezone.utc).date()
    daily_labels, daily_data = [], []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        count = Shipment.query.filter(
            func.date(Shipment.created_at) == day.isoformat()
        ).count()
        daily_labels.append(day.strftime("%a"))
        daily_data.append(count)

    recent = Shipment.query.order_by(Shipment.created_at.desc()).limit(8).all()

    return render_template(
        "admin/dashboard.html",
        totals=totals,
        status_chart=status_chart,
        daily_labels=daily_labels,
        daily_data=daily_data,
        recent=recent,
    )


# --------------------------------------------------------------------------- #
#  Live map + optimisation
# --------------------------------------------------------------------------- #
@bp.route("/map")
@login_required
@admin_only
def live_map():
    departure, day, time_raw, strategy, departure_label = _planning_inputs()

    hubs = Hub.query.all()
    shipments = Shipment.query.filter(
        Shipment.status.in_([
            ShipmentStatus.AT_WAREHOUSE, ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.PENDING,
        ])
    ).all()
    stops = RouteStop.query.order_by(RouteStop.courier_id, RouteStop.sequence).all()

    closures = active_closure_dicts()
    routes = {}
    for st in stops:
        points = json.loads(st.path_json) if st.path_json else []
        # Colour the map for the planned dispatch time, not just "now".
        overlay = build_overlay(points, closures, when=departure)
        routes.setdefault(st.courier_id, []).append({
            "sequence": st.sequence,
            "segments": overlay["segments"],
            "blocked": overlay["blocked"],
            "tracking_number": st.shipment.tracking_number,
            "receiver": st.shipment.receiver_name,
            "coords": st.shipment.coords,
        })
    courier_names = {c.id: c.name for c in User.query.filter_by(role=Role.COURIER).all()}

    # Compare every optimisation technique for the chosen dispatch day & time.
    comparison = compare_strategies(departure=departure)

    return render_template(
        "admin/map.html",
        hubs=hubs, shipments=shipments, routes=routes, courier_names=courier_names,
        closures=closures, level_colors=LEVEL_COLORS, level_labels=LEVEL_LABELS,
        comparison=comparison, departure_label=departure_label,
        departure_day=day, departure_time=time_raw, selected_strategy=strategy,
        strategies=STRATEGIES, strategy_order=STRATEGY_ORDER,
        weekday_labels=WEEKDAY_LABELS, time_presets=TIME_PRESETS,
    )


@bp.route("/optimize", methods=["POST"])
@login_required
@admin_only
def optimize():
    hub_id = request.form.get("hub_id", type=int)
    hub = db.session.get(Hub, hub_id) if hub_id else None
    departure, day, time_raw, strategy, departure_label = _planning_inputs()
    summary = optimize_and_persist(hub, departure=departure, strategy=strategy)
    if summary["assigned"]:
        flash(
            f"Optimised with {summary['strategy_label']} for {departure_label}: "
            f"{len(summary['routes'])} route(s) · {summary['assigned']} stops · "
            f"{summary['total_distance_km']} km total.",
            "success",
        )
    else:
        flash("Nothing to optimise. Make sure parcels are marked 'At Warehouse' and couriers exist.", "info")
    return redirect(url_for("admin.live_map", day=day, time=time_raw, strategy=strategy))


# --------------------------------------------------------------------------- #
#  Hubs
# --------------------------------------------------------------------------- #
@bp.route("/hubs", methods=["GET", "POST"])
@login_required
@admin_only
def hubs():
    form = HubForm()
    if form.validate_on_submit():
        db.session.add(Hub(
            name=form.name.data, address=form.address.data,
            lat=form.lat.data, lon=form.lon.data,
        ))
        db.session.commit()
        flash("Hub created.", "success")
        return redirect(url_for("admin.hubs"))
    return render_template("admin/hubs.html", hubs=Hub.query.all(), form=form)


@bp.route("/hubs/<int:hub_id>/edit", methods=["POST"])
@login_required
@admin_only
def edit_hub(hub_id):
    hub = db.get_or_404(Hub, hub_id)
    form = HubForm()
    if form.validate_on_submit():
        hub.name, hub.address = form.name.data, form.address.data
        hub.lat, hub.lon = form.lat.data, form.lon.data
        db.session.commit()
        flash("Hub updated.", "success")
    else:
        flash("Could not update hub. Check the values.", "danger")
    return redirect(url_for("admin.hubs"))


@bp.route("/hubs/<int:hub_id>/delete", methods=["POST"])
@login_required
@admin_only
def delete_hub(hub_id):
    hub = db.get_or_404(Hub, hub_id)
    if hub.shipments.count() or hub.couriers:
        flash("Cannot delete a hub that still has shipments or couriers.", "warning")
    else:
        db.session.delete(hub)
        db.session.commit()
        flash("Hub deleted.", "info")
    return redirect(url_for("admin.hubs"))


# --------------------------------------------------------------------------- #
#  Couriers
# --------------------------------------------------------------------------- #
def _hub_choices():
    return [(h.id, h.name) for h in Hub.query.order_by(Hub.name).all()]


@bp.route("/couriers", methods=["GET", "POST"])
@login_required
@admin_only
def couriers():
    form = CourierForm()
    form.hub_id.choices = _hub_choices()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "warning")
        elif not form.password.data:
            flash("A password is required when creating a courier.", "warning")
        else:
            courier = User(
                name=form.name.data, email=email, phone=form.phone.data,
                role=Role.COURIER, hub_id=form.hub_id.data,
                vehicle_type=form.vehicle_type.data,
            )
            courier.set_password(form.password.data)
            db.session.add(courier)
            db.session.commit()
            flash("Courier created.", "success")
        return redirect(url_for("admin.couriers"))
    couriers_list = User.query.filter_by(role=Role.COURIER).order_by(User.name).all()
    return render_template(
        "admin/couriers.html",
        couriers=couriers_list, form=form, hubs=Hub.query.order_by(Hub.name).all(),
    )


@bp.route("/couriers/<int:courier_id>/edit", methods=["POST"])
@login_required
@admin_only
def edit_courier(courier_id):
    courier = db.get_or_404(User, courier_id)
    if courier.role != Role.COURIER:
        flash("That user is not a courier.", "warning")
        return redirect(url_for("admin.couriers"))

    form = CourierForm()
    form.hub_id.choices = _hub_choices()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        clash = User.query.filter_by(email=email).first()
        if clash and clash.id != courier.id:
            flash("Another user already uses that email.", "warning")
        else:
            courier.name = form.name.data
            courier.email = email
            courier.phone = form.phone.data
            courier.hub_id = form.hub_id.data
            courier.vehicle_type = form.vehicle_type.data
            if form.password.data:  # optional reset
                courier.set_password(form.password.data)
            db.session.commit()
            flash("Courier updated.", "success")
    else:
        flash("Could not update courier. Check the values (password, if set, needs 8+ chars).", "danger")
    return redirect(url_for("admin.couriers"))


@bp.route("/couriers/<int:courier_id>/toggle", methods=["POST"])
@login_required
@admin_only
def toggle_courier(courier_id):
    courier = db.get_or_404(User, courier_id)
    if courier.role == Role.COURIER:
        courier.is_active = not courier.is_active
        db.session.commit()
        flash(f"Courier {'activated' if courier.is_active else 'deactivated'}.", "info")
    return redirect(url_for("admin.couriers"))


# --------------------------------------------------------------------------- #
#  Road closures / traffic
# --------------------------------------------------------------------------- #
@bp.route("/closures", methods=["GET", "POST"])
@login_required
@admin_only
def closures():
    form = RoadClosureForm()
    if form.validate_on_submit():
        db.session.add(RoadClosure(
            name=form.name.data, reason=form.reason.data,
            lat=form.lat.data, lon=form.lon.data, radius_m=form.radius_m.data,
        ))
        db.session.commit()
        flash("Road closure added. Re-optimise routes to avoid it.", "success")
        return redirect(url_for("admin.closures"))
    items = RoadClosure.query.order_by(RoadClosure.is_active.desc(), RoadClosure.created_at.desc()).all()
    return render_template(
        "admin/closures.html",
        closures=items, form=form, hubs=Hub.query.all(),
        level_colors=LEVEL_COLORS, level_labels=LEVEL_LABELS,
    )


@bp.route("/closures/<int:closure_id>/toggle", methods=["POST"])
@login_required
@admin_only
def toggle_closure(closure_id):
    c = db.get_or_404(RoadClosure, closure_id)
    c.is_active = not c.is_active
    db.session.commit()
    flash(f"Closure {'re-activated' if c.is_active else 'lifted'}.", "info")
    return redirect(url_for("admin.closures"))


@bp.route("/closures/<int:closure_id>/delete", methods=["POST"])
@login_required
@admin_only
def delete_closure(closure_id):
    c = db.get_or_404(RoadClosure, closure_id)
    db.session.delete(c)
    db.session.commit()
    flash("Closure deleted.", "info")
    return redirect(url_for("admin.closures"))


# --------------------------------------------------------------------------- #
#  Shipments
# --------------------------------------------------------------------------- #
@bp.route("/shipments")
@login_required
@admin_only
def shipments():
    status = request.args.get("status")
    query = Shipment.query
    if status:
        query = query.filter_by(status=status)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Shipment.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "admin/shipments.html",
        pagination=pagination, shipments=pagination.items, current_status=status,
    )


@bp.route("/shipments/<int:shipment_id>")
@login_required
@admin_only
def shipment_detail(shipment_id):
    shipment = db.get_or_404(Shipment, shipment_id)
    couriers = User.query.filter_by(role=Role.COURIER, is_active=True).all()
    return render_template("admin/shipment_detail.html", s=shipment, couriers=couriers)


@bp.route("/shipments/<int:shipment_id>/status", methods=["POST"])
@login_required
@admin_only
def update_status(shipment_id):
    shipment = db.get_or_404(Shipment, shipment_id)
    new_status = request.form.get("status")
    note = request.form.get("note") or None
    if new_status in ShipmentStatus.LABELS:
        if new_status == ShipmentStatus.AT_WAREHOUSE and not shipment.picked_up_at:
            shipment.picked_up_at = datetime.now(timezone.utc)
        if new_status == ShipmentStatus.DELIVERED:
            shipment.delivered_at = datetime.now(timezone.utc)
        shipment.add_event(new_status, note=note, user=current_user)
        db.session.commit()
        flash("Status updated.", "success")
    else:
        flash("Invalid status.", "danger")
    return redirect(url_for("admin.shipment_detail", shipment_id=shipment_id))


@bp.route("/shipments/<int:shipment_id>/assign", methods=["POST"])
@login_required
@admin_only
def assign_courier(shipment_id):
    shipment = db.get_or_404(Shipment, shipment_id)
    courier_id = request.form.get("courier_id", type=int)
    courier = db.session.get(User, courier_id) if courier_id else None
    if courier and courier.role == Role.COURIER:
        shipment.courier_id = courier.id
        if shipment.status in (ShipmentStatus.PENDING, ShipmentStatus.AT_WAREHOUSE):
            shipment.add_event(
                ShipmentStatus.OUT_FOR_DELIVERY,
                note=f"Manually assigned to {courier.name}",
                user=current_user,
            )
        db.session.commit()
        flash(f"Assigned to {courier.name}.", "success")
    return redirect(url_for("admin.shipment_detail", shipment_id=shipment_id))
