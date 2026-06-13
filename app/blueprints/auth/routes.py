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
    """Public self-service registration creates merchant accounts only.

    Couriers are created by admins; the first admin is created via the
    ``flask create-admin`` CLI command or the seed script.
    """
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "warning")
        else:
            user = User(
                name=form.name.data.strip(),
                email=email,
                phone=form.phone.data,
                business_name=form.business_name.data or None,
                role=Role.MERCHANT,
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash("Your merchant account is ready.", "success")
            return redirect(url_for("merchant.dashboard"))
    return render_template("auth/register.html", form=form)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.index"))
