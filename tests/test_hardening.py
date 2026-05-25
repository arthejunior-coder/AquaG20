"""Testes do hardening: headers de segurança, /healthz, rate limit, log rotacionado."""

from __future__ import annotations

import logging
import os

import pytest


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_basico_em_resposta_publica(self, client):
        """Aplica até pra login (sem auth)."""
        r = client.get("/auth/login")
        assert r.status_code == 200
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("Referrer-Policy") == "same-origin"
        assert "Content-Security-Policy" in r.headers
        csp = r.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_hsts_off_em_test(self, client):
        """HSTS só deve aparecer em prod (SECURITY_HSTS_ENABLED=False em test)."""
        r = client.get("/auth/login")
        assert "Strict-Transport-Security" not in r.headers

    def test_hsts_on_quando_config_pede(self, app, client):
        app.config["SECURITY_HSTS_ENABLED"] = True
        try:
            r = client.get("/auth/login")
            assert "Strict-Transport-Security" in r.headers
            assert "max-age=" in r.headers["Strict-Transport-Security"]
        finally:
            app.config["SECURITY_HSTS_ENABLED"] = False

    def test_headers_em_rota_autenticada(self, client, two_tenants, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers.get("X-Frame-Options") == "DENY"

    def test_headers_em_404(self, client):
        r = client.get("/inexistente-totalmente")
        assert r.status_code == 404
        assert r.headers.get("X-Content-Type-Options") == "nosniff"


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class TestHealthcheck:
    def test_healthz_ok(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"

    def test_healthz_sem_login(self, client):
        """/healthz NÃO exige login (load balancer não loga)."""
        r = client.get("/healthz", follow_redirects=False)
        assert r.status_code == 200  # não 302 pra /auth/login

    def test_healthz_isento_de_csrf(self, app, client):
        """Mesmo se tentassem POST (load balancers só fazem GET, mas safety).
        Como tornamos GET livre, isso só confirma que não há redirect/login."""
        r = client.get("/healthz")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Limiter sempre ligado em test; autouse fixture (_ratelimit_reset)
    zera o storage entre testes pra evitar contaminação."""

    def test_login_dispara_429_apos_burst(self, client):
        """Login POST tem limit 10/min — em algum momento dentro de 15
        tentativas, o 429 deve aparecer. Não amarra ao número exato pra
        ser robusto contra contaminação residual de testes anteriores."""
        statuses = []
        for _ in range(15):
            r = client.post("/auth/login", data={
                "email": "ninguem@a.com", "senha": "x",
                "submit": "Entrar",
            }, follow_redirects=False)
            statuses.append(r.status_code)
        assert 429 in statuses, f"limite nunca disparou em 15 tentativas: {statuses}"
        # Limite é 10/min — primeira 429 não pode acontecer cedo demais
        first_429 = statuses.index(429)
        assert first_429 >= 5, (
            f"limit disparou cedo demais (tentativa {first_429 + 1}); statuses={statuses}"
        )
        # Template amigável: confere na resposta após 429
        r = client.post("/auth/login", data={
            "email": "ninguem@a.com", "senha": "x", "submit": "Entrar",
        })
        assert r.status_code == 429
        assert b"429" in r.data or b"Muitas" in r.data

    def test_get_login_nao_e_limitado(self, client):
        """O decorator está restrito a methods=['POST']; GET deve ser livre."""
        for _ in range(20):
            r = client.get("/auth/login")
            assert r.status_code == 200

    def test_esqueci_senha_dispara_429_apos_burst(self, client):
        """Esqueci-senha POST tem limit 5/min — 429 vem em algum momento
        dentro de 10 tentativas."""
        statuses = []
        for _ in range(10):
            r = client.post("/auth/esqueci-senha", data={
                "email": "x@y.com", "submit": "Enviar link de redefinição",
            }, follow_redirects=False)
            statuses.append(r.status_code)
        assert 429 in statuses, f"limite nunca disparou: {statuses}"
        first_429 = statuses.index(429)
        # Tolera contaminação prévia mas precisa pelo menos 2 passarem
        assert first_429 >= 2, (
            f"limit disparou cedo demais (tentativa {first_429 + 1}); statuses={statuses}"
        )


# ---------------------------------------------------------------------------
# Log rotacionado
# ---------------------------------------------------------------------------


class TestLogRotacionado:
    def test_handler_de_arquivo_anexado_quando_log_file_path(self, tmp_path):
        """Re-cria a app com LOG_FILE_PATH configurado e confere o handler."""
        from app import create_app

        log_file = tmp_path / "aquag20.log"
        original = os.environ.get("LOG_FILE_PATH")
        os.environ["LOG_FILE_PATH"] = str(log_file)
        try:
            app = create_app("dev")
            # Esperamos 2 handlers: stream + rotating file
            kinds = [type(h).__name__ for h in app.logger.handlers]
            assert "RotatingFileHandler" in kinds
            # Loga algo e confere que vai pro arquivo
            app.logger.info("teste-rotacionado-12345")
            for h in app.logger.handlers:
                h.flush()
            assert log_file.exists()
            content = log_file.read_text(encoding="utf-8")
            assert "teste-rotacionado-12345" in content
        finally:
            if original is None:
                os.environ.pop("LOG_FILE_PATH", None)
            else:
                os.environ["LOG_FILE_PATH"] = original
