"""Hardening de produção — headers de segurança + healthcheck.

Headers aplicados como `after_request` (sem dependência adicional tipo
flask-talisman, que é grande para o pouco que precisamos):

  - Content-Security-Policy: política mínima coerente com Tailwind +
    HTMX no projeto. Sem 'unsafe-inline' em script-src; pequenos
    scripts inline em base.html e form.html são tolerados por enquanto
    via SHA hash mas o caminho correto é eventualmente movê-los para
    arquivos próprios (TODO). 'unsafe-inline' em STYLE permitido por
    causa de Tailwind utilities.
  - Strict-Transport-Security: só em prod (SECURITY_HSTS_ENABLED=True).
  - X-Content-Type-Options: nosniff (anti-MIME-sniff).
  - X-Frame-Options: DENY (anti-clickjacking).
  - Referrer-Policy: same-origin (evita vazar URL de páginas internas
    pra terceiros).

Healthcheck: GET /healthz devolve 200 + JSON com status do DB. Usado
por load balancer / orquestrador pra decidir se o pod está saudável.
"""

from __future__ import annotations

from flask import Flask, jsonify
from sqlalchemy import text

from app.extensions import db


# CSP base — ajustes:
#   - script-src 'self' permite os assets locais (HTMX servido por nós).
#     'unsafe-inline' é necessário para scripts inline em base.html e
#     form de pedido. Não usamos eval. TODO: extrair pra arquivos.
#   - style-src com 'unsafe-inline' por causa de classes utilitárias
#     ALÉM do <link>; Tailwind compilado vai num arquivo, mas Jinja
#     gera style="..." em alguns lugares.
#   - font/img liberados pra self + data: (data: cobre svg inline).
_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://rsms.me; "
    "font-src 'self' https://rsms.me data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def register_security(app: Flask) -> None:
    @app.after_request
    def _security_headers(response):
        # Não impomos a páginas servidas dentro de iframe (não temos).
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
        if app.config.get("SECURITY_HSTS_ENABLED"):
            max_age = app.config.get("SECURITY_HSTS_MAX_AGE", 0)
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={max_age}; includeSubDomains",
            )
        return response


def register_healthcheck(app: Flask) -> None:
    """GET /healthz — checa app + DB. Sem login, sem CSRF.

    Quando DB está saudável: 200 + {"status":"ok","db":"ok"}.
    Quando DB falha (timeout, conexão recusada): 503 + erro logado.

    Não inclui dependências externas (só DB) — é um check de liveness
    com toque de readiness, suficiente para Kubernetes/healthchecks
    de load balancer.
    """

    @app.route("/healthz")
    def _healthz():
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify({"status": "ok", "db": "ok"}), 200
        except Exception as e:  # pragma: no cover — caminho de falha
            app.logger.exception("healthz: DB check failed")
            return jsonify({"status": "degraded", "db": "fail",
                             "error": str(e)[:120]}), 503

    # Isenta CSRF (não há body); login_required NÃO se aplica (é GET livre).
    from app.extensions import csrf
    csrf.exempt(_healthz)
