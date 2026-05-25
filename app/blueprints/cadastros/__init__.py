from flask import Blueprint

bp = Blueprint("cadastros", __name__, url_prefix="/cadastros")

from app.blueprints.cadastros import routes  # noqa: E402, F401
