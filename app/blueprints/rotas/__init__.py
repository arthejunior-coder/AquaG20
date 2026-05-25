from flask import Blueprint

bp = Blueprint("rotas", __name__, url_prefix="/rotas")

from app.blueprints.rotas import routes  # noqa: E402, F401
