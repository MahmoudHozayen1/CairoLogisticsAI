from flask import Blueprint

bp = Blueprint("courier", __name__)

from . import routes  # noqa: E402,F401
