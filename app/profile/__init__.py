from flask import Blueprint

profile_bp = Blueprint("profile", __name__, url_prefix="/profile")

from . import routes  # noqa: E402, F401
