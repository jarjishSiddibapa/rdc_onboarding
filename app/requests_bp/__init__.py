from flask import Blueprint

requests_bp = Blueprint("requests_bp", __name__, url_prefix="/requests")

from . import routes  # noqa: E402, F401
