from flask import Blueprint

bp = Blueprint("pool", __name__, url_prefix="/pool")

from app.blueprints.pool import routes  # noqa: E402, F401
