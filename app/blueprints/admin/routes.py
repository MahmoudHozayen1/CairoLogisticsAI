import json
from datetime import datetime, timedelta, timezone

from flask import render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import func

from . import bp
from ...extensions import db
from ...forms import HubForm, CourierForm, RoadClosureForm, AdminShipmentForm
from ...models import (
    User, Hub, Shipment, RouteStop, RoadClosure, ShipmentStatus, Role,
    DeliveryConfirmation,
)
from ...utils import role_required, generate_tracking_number
from ...routing import (
    optimize_and_persist, build_overlay, active_closure_dicts,
    compare_strategies, resolve_departure, STRATEGIES, STRATEGY_ORDER,
    WEEKDAY_LABELS, LEVEL_COLORS, LEVEL_LABELS, haversine_km,
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
        # Compare against a [start, next-day) range instead of func.date(...) == str:
        # the latter throws on PostgreSQL ("operator does not exist: date = text")
        # while a range comparison is portable across SQLite and Postgres.
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        count = Shipment.query.filter(
            Shipment.created_at >= day_start,
            Shipment.created_at < day_start + timedelta(days=1),
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
            "eta_minutes": st.eta_minutes,
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
        msg = (
            f"Optimised with {summary['strategy_label']} for {departure_label}: "
            f"{len(summary['routes'])} route(s) · {summary['assigned']} stops · "
            f"{summary['total_distance_km']} km total."
        )
        if summary.get("unassigned"):
            msg += (
                f" {summary['unassigned']} parcel(s) left unassigned — the fleet is at "
                f"capacity. Add couriers or a larger vehicle, then re-optimise."
            )
        flash(msg, "success")
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
    status = request.args.get("status") or ""
    merchant_id = request.args.get("merchant_id", type=int)
    district = request.args.get("district") or ""
    q = (request.args.get("q") or "").strip()

    query = Shipment.query
    if status:
        query = query.filter_by(status=status)
    if merchant_id:
        query = query.filter_by(merchant_id=merchant_id)
    if district:
        query = query.filter(Shipment.district == district)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            Shipment.tracking_number.ilike(like),
            Shipment.receiver_name.ilike(like),
            Shipment.receiver_phone.ilike(like),
        ))

    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Shipment.created_at.desc()).paginate(page=page, per_page=20, error_out=False)

    # Filter options.
    merchants = User.query.filter_by(role=Role.MERCHANT).order_by(User.name).all()
    districts = [
        d[0] for d in db.session.query(Shipment.district)
        .filter(Shipment.district.isnot(None), Shipment.district != "")
        .distinct().order_by(Shipment.district).all()
    ]
    return render_template(
        "admin/shipments.html",
        pagination=pagination, shipments=pagination.items, current_status=status,
        merchants=merchants, districts=districts,
        current_merchant=merchant_id, current_district=district, search_q=q,
    )


@bp.route("/shipments/new", methods=["GET", "POST"])
@login_required
@admin_only
def create_shipment():
    form = AdminShipmentForm()
    merchants = User.query.filter_by(role=Role.MERCHANT).order_by(User.name).all()
    hubs = Hub.query.order_by(Hub.name).all()
    form.merchant_id.choices = [(m.id, m.display_name) for m in merchants]
    form.hub_id.choices = [(0, "Auto-assign nearest hub")] + [(h.id, h.name) for h in hubs]

    if not merchants:
        flash("Create a merchant account first — a shipment must belong to a merchant.", "warning")
        return redirect(url_for("admin.shipments"))

    if form.validate_on_submit():
        merchant = db.session.get(User, form.merchant_id.data)
        coords = [form.lat.data, form.lon.data]
        hub_id = form.hub_id.data or _nearest_hub_id(coords)

        shipment = Shipment(
            tracking_number=_unique_tracking_number(),
            merchant_id=merchant.id,
            hub_id=hub_id,
            sender_name=merchant.display_name,
            sender_phone=merchant.phone,
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
        shipment.add_event(ShipmentStatus.PENDING, note="Shipment created by admin", user=current_user)
        db.session.add(shipment)
        db.session.commit()
        flash(f"Shipment created for {merchant.display_name}. Tracking: {shipment.tracking_number}", "success")
        return redirect(url_for("admin.shipment_detail", shipment_id=shipment.id))

    return render_template("admin/create_shipment.html", form=form, hubs=hubs)


def _unique_tracking_number():
    for _ in range(10):
        tn = generate_tracking_number()
        if not Shipment.query.filter_by(tracking_number=tn).first():
            return tn
    return generate_tracking_number()


def _nearest_hub_id(coords):
    hubs = Hub.query.all()
    if not hubs:
        return None
    return min(hubs, key=lambda h: haversine_km(coords, h.coords)).id


def _log_dispatch(shipment):
    """Log predictions when a shipment goes out for delivery (feedback loop)."""
    try:
        from ...ml.feedback import log_predictions
        log_predictions(shipment)
    except Exception:  # pragma: no cover - never block dispatch
        db.session.rollback()


def _resolve_delivery(shipment):
    """Close the feedback loop and record a GIS confirmation on completion."""
    try:
        from ...ml.feedback import resolve_predictions
        resolve_predictions(shipment)
    except Exception:  # pragma: no cover
        db.session.rollback()
    try:
        from ...audit import confirm_delivery_location
        if shipment.delivery_confirmation is None:
            confirm_delivery_location(shipment)  # no admin GPS -> simulated
    except Exception:  # pragma: no cover
        db.session.rollback()


@bp.route("/shipments/<int:shipment_id>")
@login_required
@admin_only
def shipment_detail(shipment_id):
    shipment = db.get_or_404(Shipment, shipment_id)
    couriers = User.query.filter_by(role=Role.COURIER, is_active=True).all()
    predictions = None
    from ...ml import get_service
    svc = get_service()
    if svc.is_trained:
        try:
            predictions = svc.predict_all(shipment)
        except Exception as exc:  # pragma: no cover - defensive, never block the page
            current_app.logger.warning("AI prediction failed for %s: %s",
                                       shipment.tracking_number, exc)
    note_analysis = None
    if shipment.delivery_notes:
        try:
            note_analysis = svc.analyze_note(shipment.delivery_notes)
        except Exception as exc:  # pragma: no cover - defensive
            current_app.logger.warning("Note analysis failed for %s: %s",
                                       shipment.tracking_number, exc)
    from ...audit import verify_chain
    chain = verify_chain(shipment)
    return render_template("admin/shipment_detail.html", s=shipment,
                           couriers=couriers, predictions=predictions,
                           chain=chain, confirmation=shipment.delivery_confirmation,
                           note_analysis=note_analysis)


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
        db.session.flush()
        if new_status == ShipmentStatus.OUT_FOR_DELIVERY:
            _log_dispatch(shipment)
        elif new_status == ShipmentStatus.DELIVERED:
            _resolve_delivery(shipment)
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
            db.session.flush()
            _log_dispatch(shipment)
        db.session.commit()
        flash(f"Assigned to {courier.name}.", "success")
    return redirect(url_for("admin.shipment_detail", shipment_id=shipment_id))


# --------------------------------------------------------------------------- #
#  AI / Data-science dashboard
# --------------------------------------------------------------------------- #
def _forecast_chart(fc, window=60):
    """Assemble Chart.js-friendly actual + forecast series (orders & cost)."""
    hist_dates = fc["history"]["dates"][-window:]
    hist_orders = fc["history"]["orders"][-window:]
    hist_cost = fc["history"]["cost"][-window:]
    future = fc["future_dates"]
    n_hist, n_future = len(hist_dates), len(future)

    labels = [d[5:] for d in hist_dates] + [d[5:] for d in future]  # MM-DD
    pad = [None] * n_hist

    def series(actual, block):
        forecast = pad[:] + block["point"]
        # bridge the dashed forecast line to the last actual point
        if n_hist:
            forecast[n_hist - 1] = actual[-1]
        return {
            "actual": actual + [None] * n_future,
            "forecast": forecast,
            "lower": pad[:] + block["lower"],
            "upper": pad[:] + block["upper"],
        }

    return {
        "labels": labels,
        "orders": series(hist_orders, fc["orders"]),
        "cost": series(hist_cost, fc["cost"]),
    }


@bp.route("/ai")
@login_required
@admin_only
def ai_overview():
    from ...ml import get_service
    svc = get_service()
    trained = svc.is_trained
    metrics = forecast = chart = None
    if trained:
        metrics = svc.model_cards()
        forecast = svc.forecast(horizon=14)
        chart = _forecast_chart(forecast)
    return render_template(
        "admin/ai.html", trained=trained, metrics=metrics,
        forecast=forecast, chart=chart,
    )


@bp.route("/ai/train", methods=["POST"])
@login_required
@admin_only
def ai_train():
    from ...ml import get_service
    regenerate = bool(request.form.get("regenerate"))
    try:
        get_service().ensure_trained(force=regenerate)
        flash("Predictive models trained successfully.", "success")
    except Exception as exc:  # pragma: no cover - surfaced to the admin
        current_app.logger.exception("Model training failed")
        flash(f"Training failed: {exc}", "danger")
    return redirect(url_for("admin.ai_overview"))


def _feedback_chart(report):
    """Chart.js series for the drift line and late-risk calibration curve."""
    drift = report["drift"]
    calib = report["calibration"]["bins"]
    return {
        "drift_labels": drift["weeks"],
        "drift_mae": drift["mae"],
        "drift_baseline": drift["baseline_mae"],
        "residual_centers": report["residuals"]["centers"],
        "residual_counts": report["residuals"]["counts"],
        "calib_predicted": [b["predicted"] for b in calib],
        "calib_observed": [b["observed"] for b in calib],
    }


@bp.route("/ai/feedback")
@login_required
@admin_only
def ai_feedback():
    from ...ml import get_service
    svc = get_service()
    trained = svc.is_trained
    report = chart = None
    if trained:
        try:
            from ...ml.feedback import feedback_report
            report = feedback_report()
            chart = _feedback_chart(report)
        except Exception as exc:  # pragma: no cover
            current_app.logger.exception("Feedback report failed")
            flash(f"Feedback report unavailable: {exc}", "warning")
    return render_template(
        "admin/feedback.html", trained=trained, report=report, chart=chart,
    )


# --------------------------------------------------------------------------- #
#  Chain-of-custody audit
# --------------------------------------------------------------------------- #
@bp.route("/audit")
@login_required
@admin_only
def audit_overview():
    from ...audit import verify_chain
    recent = (
        Shipment.query.filter(Shipment.status.in_([
            ShipmentStatus.DELIVERED, ShipmentStatus.OUT_FOR_DELIVERY,
            ShipmentStatus.RETURNED, ShipmentStatus.FAILED,
        ]))
        .order_by(Shipment.id.desc())
        .limit(40)
        .all()
    )
    rows, intact, broken, total_handoffs = [], 0, 0, 0
    for s in recent:
        chain = verify_chain(s)
        if chain["count"] == 0:
            continue
        total_handoffs += chain["count"]
        if chain["ok"]:
            intact += 1
        else:
            broken += 1
        rows.append({"shipment": s, "chain": chain})
    confirmed = DeliveryConfirmation.query.filter_by(verified=True).count()
    unverified = DeliveryConfirmation.query.filter_by(verified=False).count()
    stats = {
        "chains": len(rows),
        "intact": intact,
        "broken": broken,
        "handoffs": total_handoffs,
        "confirmed": confirmed,
        "unverified": unverified,
    }
    return render_template("admin/audit.html", rows=rows, stats=stats)


# --------------------------------------------------------------------------- #
#  Operations assistant (chatbot)
# --------------------------------------------------------------------------- #
@bp.route("/assistant")
@login_required
@admin_only
def assistant():
    from ...ml.assistant import SUGGESTIONS
    llm = current_app.config.get("ASSISTANT_USE_LLM")
    return render_template(
        "admin/assistant.html", suggestions=SUGGESTIONS, llm_enabled=llm)


@bp.route("/assistant/ask", methods=["POST"])
@login_required
@admin_only
def assistant_ask():
    from ...ml.assistant import get_assistant
    question = (request.json or {}).get("question", "") if request.is_json \
        else request.form.get("question", "")
    try:
        result = get_assistant().answer(question, dict(current_app.config))
    except Exception as exc:  # pragma: no cover - surfaced to the admin
        current_app.logger.exception("Assistant failed")
        return jsonify({"answer": f"Sorry, I hit an error: {exc}",
                        "intent": "error", "sources": [], "used_llm": False}), 200
    return jsonify(result)


# --------------------------------------------------------------------------- #
#  Learning-to-Route (neural pointer policy) demo
# --------------------------------------------------------------------------- #
@bp.route("/ai/router")
@login_required
@admin_only
def ai_router():
    """Run the learned pointer policy on a hub's warehouse queue and explain it."""
    hubs = Hub.query.order_by(Hub.name).all()
    hub_id = request.args.get("hub_id", type=int)
    hub = db.session.get(Hub, hub_id) if hub_id else (hubs[0] if hubs else None)

    plan = ordered = None
    metrics = None
    error = None
    stops = []
    if hub is not None:
        stops = (
            Shipment.query
            .filter(
                Shipment.hub_id == hub.id,
                Shipment.status.in_([
                    ShipmentStatus.AT_WAREHOUSE, ShipmentStatus.OUT_FOR_DELIVERY]),
            )
            .order_by(Shipment.id)
            .limit(18)
            .all()
        )
        if len(stops) >= 3:
            try:
                from ...ml import get_router
                router = get_router()
                points = [s.coords for s in stops]
                plan = router.route(points, hub.coords)
                metrics = router.metrics()
                ordered = []
                for seq, step in enumerate(plan["steps"], start=1):
                    s = stops[step["stop_index"]]
                    ordered.append({
                        "sequence": seq,
                        "shipment": s,
                        "leg_km": step["leg_km"],
                        "probability": step["probability"],
                        "reasons": step["reasons"],
                    })
            except Exception as exc:  # pragma: no cover - surfaced to the admin
                current_app.logger.exception("Neural router failed")
                error = str(exc)
        else:
            error = ("Need at least 3 parcels out for delivery or at this hub to plan "
                     "a route. Seed more data or receive parcels at the warehouse.")

    return render_template(
        "admin/router.html",
        hubs=hubs, hub=hub, stops=stops, plan=plan, ordered=ordered,
        metrics=metrics, error=error,
    )


# --------------------------------------------------------------------------- #
#  Courier behaviour modelling (persona clustering) demo
# --------------------------------------------------------------------------- #
@bp.route("/ai/behavior")
@login_required
@admin_only
def ai_behavior():
    """Simulate each courier's shift, cluster into personas and explain it."""
    from ...ml import get_behavior_model
    from ...ml import behavior as bh

    couriers = (
        User.query
        .filter_by(role=Role.COURIER, is_active=True)
        .order_by(User.name)
        .all()
    )
    sel_id = request.args.get("courier_id", type=int)

    fleet = []
    detail = None
    metrics = None
    error = None
    try:
        model = get_behavior_model()
        metrics = model.metrics()
        for c in couriers:
            archetype = bh.ARCHETYPE_ORDER[c.id % len(bh.ARCHETYPE_ORDER)]
            hub_coords = c.hub.coords if c.hub else bh.HUBS[0]
            shift = bh.simulate_shift(archetype, seed=1000 + c.id, hub=hub_coords)
            res = model.analyze(shift["trace"], shift["ideal_km"])
            row = {
                "courier": c,
                "persona": res["persona"],
                "score": res["productivity_score"],
                "confidence": res["persona_confidence"],
                "deliveries": res["summary"]["n_deliveries"],
                "flags": res["flags"],
            }
            fleet.append(row)
            if (sel_id and c.id == sel_id) or (not sel_id and detail is None):
                detail = {"courier": c, "result": res}
    except Exception as exc:  # pragma: no cover - surfaced to the admin
        current_app.logger.exception("Behaviour model failed")
        error = str(exc)

    # Persona distribution across the demo fleet (for the donut chart).
    distribution = {}
    for row in fleet:
        name = row["persona"]["name"]
        distribution[name] = distribution.get(name, 0) + 1

    return render_template(
        "admin/behavior.html",
        couriers=couriers, fleet=fleet, detail=detail,
        distribution=distribution, metrics=metrics, error=error,
    )


