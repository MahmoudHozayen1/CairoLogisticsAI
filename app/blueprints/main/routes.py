from flask import render_template, redirect, url_for
from flask_login import current_user

from . import bp
from ...models import Role


@bp.route("/")
def index():
    return render_template("landing.html")


@bp.route("/dashboard")
def dashboard():
    """Send each signed-in user to their role's home."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.role == Role.ADMIN:
        return redirect(url_for("admin.dashboard"))
    if current_user.role == Role.COURIER:
        return redirect(url_for("courier.dashboard"))
    return redirect(url_for("merchant.dashboard"))
