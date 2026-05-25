from flask import Blueprint

bp = Blueprint("financeiro", __name__, url_prefix="/financeiro")

from app.blueprints.financeiro import routes  # noqa: E402, F401
