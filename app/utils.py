"""Helper utilities: access-control decorators, tracking numbers, uploads."""
import os
import secrets
from functools import wraps

from flask import abort, current_app, flash, redirect, url_for
from flask_login import current_user
from werkzeug.utils import secure_filename


def role_required(*roles):
    """Restrict a view to users whose ``role`` is in ``roles``.

    Unauthenticated users are redirected to the login page; authenticated users
    with the wrong role get a 403.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                flash("Please sign in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def generate_tracking_number():
    """Human-friendly, collision-resistant tracking code, e.g. ``SR-7F3K9Q2A``."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars (0/O, 1/I)
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"SR-{body}"


def allowed_image(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]


def save_proof_image(file_storage, tracking_number):
    """Persist an uploaded proof-of-delivery image and return its filename."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_image(file_storage.filename):
        return None
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(f"pod_{tracking_number}_{secrets.token_hex(4)}.{ext}")
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    file_storage.save(os.path.join(upload_dir, filename))
    return filename
