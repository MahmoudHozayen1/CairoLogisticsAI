from flask import Blueprint

bp = Blueprint("merchant", __name__)

from . import routes  # noqa: E402,F401
