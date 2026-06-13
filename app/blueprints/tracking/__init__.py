from flask import Blueprint

bp = Blueprint("tracking", __name__)

from . import routes  # noqa: E402,F401
