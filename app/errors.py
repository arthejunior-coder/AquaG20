"""Handlers globais para erros HTTP comuns.

Anteriormente o Flask exibia o stack trace ou a página padrão feia para
qualquer 404/403/500. Aqui centralizamos:

  - 404: rota desconhecida OU `abort(404)` (inclui o caso de "ID de outro
    tenant" que retorna 404 propositalmente para não confirmar existência).
  - 403: papel insuficiente (decorator `papel_requerido`).
  - 500: exception não tratada. Faz `db.session.rollback()` para evitar
    deixar a sessão pendurada num estado quebrado.

500 também loga a exception no logger da app para investigação.
"""

from flask import render_template
from werkzeug.exceptions import HTTPException

from app.extensions import db


def register_error_handlers(app):
    @app.errorhandler(403)
    def _403(_e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def _404(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(429)
    def _429(_e):
        # Rate limit estourado — template amigável (sem stack trace).
        # flask-limiter adiciona header Retry-After automaticamente.
        return render_template("errors/429.html"), 429

    @app.errorhandler(500)
    def _500(e):
        db.session.rollback()
        app.logger.exception("Erro 500 em %s", _request_summary())
        return render_template("errors/500.html"), 500

    # Outros HTTPException (405, 413, etc) caem aqui com o template 500
    # mas sem rollback nem log de stack trace (não é exception nossa).
    @app.errorhandler(HTTPException)
    def _http_exc(e):
        return render_template(
            "errors/generic.html",
            code=e.code, name=e.name, description=e.description,
        ), e.code


def _request_summary() -> str:
    """Resumo curto da request para log — evita injetar PII no log."""
    from flask import request

    try:
        return f"{request.method} {request.path}"
    except RuntimeError:
        return "(no request context)"
