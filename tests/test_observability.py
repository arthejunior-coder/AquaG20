"""Testes de observabilidade: request-id, JSON logs, métricas Prometheus."""

from __future__ import annotations

import json
import logging
import os

import pytest


# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------


class TestRequestId:
    def test_response_inclui_x_request_id(self, client):
        r = client.get("/auth/login")
        assert "X-Request-Id" in r.headers
        assert len(r.headers["X-Request-Id"]) >= 8  # uuid hex tem 32

    def test_aceita_request_id_do_cliente(self, client):
        """Reverse proxy passa X-Request-Id próprio — app deve ecoar."""
        rid = "test-rid-correlation-12345"
        r = client.get("/auth/login", headers={"X-Request-Id": rid})
        assert r.headers["X-Request-Id"] == rid

    def test_request_id_unico_por_request(self, client):
        r1 = client.get("/auth/login")
        r2 = client.get("/auth/login")
        assert r1.headers["X-Request-Id"] != r2.headers["X-Request-Id"]


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def test_formata_record_basico(self):
        from app.observability import JsonFormatter

        f = JsonFormatter()
        record = logging.LogRecord(
            name="aquag20.test", level=logging.INFO,
            pathname="x.py", lineno=10,
            msg="hello %s", args=("world",),
            exc_info=None,
        )
        out = f.format(record)
        payload = json.loads(out)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "aquag20.test"
        assert payload["msg"] == "hello world"
        assert "ts" in payload

    def test_inclui_extra_fields(self):
        from app.observability import JsonFormatter

        f = JsonFormatter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="x.py", lineno=1,
            msg="msg", args=(), exc_info=None,
        )
        record.request_id = "abc123"
        record.user_id = 42
        payload = json.loads(f.format(record))
        assert payload["request_id"] == "abc123"
        assert payload["user_id"] == 42

    def test_serializa_exc_info(self):
        from app.observability import JsonFormatter

        f = JsonFormatter()
        try:
            raise RuntimeError("boom de teste")
        except RuntimeError:
            import sys
            record = logging.LogRecord(
                name="x", level=logging.ERROR, pathname="x.py", lineno=1,
                msg="error", args=(), exc_info=sys.exc_info(),
            )
        payload = json.loads(f.format(record))
        assert "exc" in payload
        assert "RuntimeError" in payload["exc"]
        assert "boom de teste" in payload["exc"]


class TestJsonLogsToggle:
    def test_off_default_em_test(self, app):
        """OBSERVABILITY_JSON_LOGS é False por default (TestConfig herda BaseConfig)."""
        assert not app.config.get("OBSERVABILITY_JSON_LOGS")
        # Handler usa Formatter padrão, não JsonFormatter
        from app.observability import JsonFormatter
        for h in app.logger.handlers:
            assert not isinstance(h.formatter, JsonFormatter)

    def test_on_via_env_aplica_json_formatter(self, tmp_path):
        from app import create_app

        original = os.environ.get("OBSERVABILITY_JSON_LOGS")
        log_file = tmp_path / "log.jsonl"
        original_log = os.environ.get("LOG_FILE_PATH")
        os.environ["OBSERVABILITY_JSON_LOGS"] = "true"
        os.environ["LOG_FILE_PATH"] = str(log_file)
        try:
            app2 = create_app("dev")
            app2.logger.setLevel(logging.INFO)  # garante INFO em dev

            from app.observability import JsonFormatter
            kinds = [type(h.formatter).__name__ for h in app2.logger.handlers]
            assert "JsonFormatter" in kinds

            # Loga e confere que sai JSON parseável
            app2.logger.info("evento de teste")
            for h in app2.logger.handlers:
                h.flush()
            content = log_file.read_text(encoding="utf-8")
            # Pelo menos uma linha deve ser JSON válido com nosso campo
            linhas = [l for l in content.splitlines() if l.strip()]
            payloads = [json.loads(l) for l in linhas]
            assert any(p.get("msg") == "evento de teste" for p in payloads)
        finally:
            if original is None:
                os.environ.pop("OBSERVABILITY_JSON_LOGS", None)
            else:
                os.environ["OBSERVABILITY_JSON_LOGS"] = original
            if original_log is None:
                os.environ.pop("LOG_FILE_PATH", None)
            else:
                os.environ["LOG_FILE_PATH"] = original_log


# ---------------------------------------------------------------------------
# Métricas Prometheus
# ---------------------------------------------------------------------------


class TestMetricsToggle:
    def test_endpoint_metrics_404_quando_desligado(self, client, app):
        """Por padrão (TestConfig), métricas estão desligadas → /metrics não existe."""
        assert not app.config.get("OBSERVABILITY_METRICS_ENABLED")
        r = client.get("/metrics")
        assert r.status_code == 404

    def test_endpoint_metrics_funciona_quando_ligado(self, tmp_path):
        from app import create_app

        original = os.environ.get("OBSERVABILITY_METRICS_ENABLED")
        os.environ["OBSERVABILITY_METRICS_ENABLED"] = "true"
        try:
            app2 = create_app("dev")
            client = app2.test_client()
            # Faz uma request pra gerar pelo menos uma métrica
            client.get("/auth/login")
            r = client.get("/metrics")
            assert r.status_code == 200
            body = r.data.decode("utf-8")
            assert "aquag20_requests_total" in body
            assert "aquag20_request_latency_seconds" in body
            # Endpoint auth.login deve aparecer
            assert 'endpoint="auth.login"' in body
        finally:
            if original is None:
                os.environ.pop("OBSERVABILITY_METRICS_ENABLED", None)
            else:
                os.environ["OBSERVABILITY_METRICS_ENABLED"] = original

    def test_metrics_nao_conta_a_si_mesma(self, tmp_path):
        """Endpoint /metrics não deve aparecer nos próprios contadores
        (senão fica feedback loop de scrape)."""
        from app import create_app

        original = os.environ.get("OBSERVABILITY_METRICS_ENABLED")
        os.environ["OBSERVABILITY_METRICS_ENABLED"] = "true"
        try:
            app2 = create_app("dev")
            client = app2.test_client()
            client.get("/metrics")
            r = client.get("/metrics")
            body = r.data.decode("utf-8")
            assert 'endpoint="metrics"' not in body
        finally:
            if original is None:
                os.environ.pop("OBSERVABILITY_METRICS_ENABLED", None)
            else:
                os.environ["OBSERVABILITY_METRICS_ENABLED"] = original

    def test_metrics_isento_de_csrf(self, tmp_path):
        """/metrics deve responder sem cookie de CSRF (Prometheus scraper
        é stateless)."""
        from app import create_app

        original = os.environ.get("OBSERVABILITY_METRICS_ENABLED")
        os.environ["OBSERVABILITY_METRICS_ENABLED"] = "true"
        try:
            # Re-cria app COM CSRF ligado pra valer (default em dev)
            app2 = create_app("dev")
            client = app2.test_client()
            r = client.get("/metrics")
            assert r.status_code == 200
        finally:
            if original is None:
                os.environ.pop("OBSERVABILITY_METRICS_ENABLED", None)
            else:
                os.environ["OBSERVABILITY_METRICS_ENABLED"] = original
