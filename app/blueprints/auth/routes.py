from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from urllib.parse import urlparse

from . import bp
from ...extensions import db
from ...forms import LoginForm, RegisterForm
from ...models import User, Role


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower().strip()).first()
        if user is None or not user.check_password(form.password.data):
            flash("Invalid email or password.", "danger")
        elif not user.is_active:
            flash("This account has been deactivated.", "warning")
        else:
            login_user(user, remember=form.remember.data)
            flash(f"Welcome back, {user.display_name}!", "success")
            next_page = request.args.get("next")
            # Open-redirect protection: only allow local paths.
            if not next_page or urlparse(next_page).netloc != "":
                next_page = url_for("main.dashboard")
            return redirect(next_page)
    return render_template("auth/login.html", form=form)


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Public self-service registration for merchants, couriers and admins.

    WARNING: admin self-registration grants full administrative access to anyone
    who can reach this page. The seed script still provisions the first admin.
    """
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        # Guard against a race between validation and commit; the form already
        # rejects duplicates, and the column has a UNIQUE constraint.
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "warning")
        else:
            role = form.role.data if form.role.data in Role.ALL else Role.MERCHANT
            user = User(
                name=form.name.data.strip(),
                email=email,
                phone=form.phone.data,
                role=role,
            )
            if role == Role.MERCHANT:
                user.business_name = form.business_name.data or None
            elif role == Role.COURIER:
                user.vehicle_type = form.vehicle_type.data or "Motorcycle"
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            if role == Role.ADMIN:
                flash("Your admin account is ready.", "success")
                return redirect(url_for("admin.dashboard"))
            if role == Role.COURIER:
                flash("Your courier account is ready. An admin will assign your hub.", "success")
                return redirect(url_for("courier.dashboard"))
            flash("Your merchant account is ready.", "success")
            return redirect(url_for("merchant.dashboard"))
    return render_template("auth/register.html", form=form)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.index"))
