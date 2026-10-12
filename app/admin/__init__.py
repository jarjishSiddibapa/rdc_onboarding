from flask import Blueprint

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

from . import routes  # noqa: E402, F401
from . import norms_routes  # noqa: E402, F401
