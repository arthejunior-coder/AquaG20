"""Observabilidade: structured JSON logs + request-id middleware + métricas Prometheus.

Tudo opt-in via config — em dev/test, defaults preservam o comportamento
atual (logs human-readable, sem métricas) pra não atrapalhar.

  - `OBSERVABILITY_JSON_LOGS=True`  → stream handler usa JsonFormatter
  - `OBSERVABILITY_METRICS_ENABLED=True` → registra /metrics + middleware

Request-id está SEMPRE ligado (overhead trivial):
  - Lê `X-Request-Id` do request se vier (de reverse proxy / upstream)
  - Senão gera UUID4
  - Anexa em `flask.g.request_id` e em todo `LogRecord.request_id`
  - Espelha em response header `X-Request-Id` pra correlacionar client↔server

JsonFormatter inline pra evitar dependência (python-json-logger é
mais robusto, mas o que precisamos cabe em 30 linhas).
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from flask import Flask, Response, g, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


# ---------------------------------------------------------------------------
# JSON logging
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Formata cada LogRecord como uma linha JSON.

    Inclui campos do record + qualquer extra que o caller adicionou,
    + `request_id` se houver (injetado pelo RequestIdFilter).
    """

    # Campos do LogRecord que pulamos no payload (são internos).
    _SKIP = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "taskName",
        "asctime", "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Extra fields (e.g. request_id injetado pelo filter)
        for k, v in record.__dict__.items():
            if k not in self._SKIP and not k.startswith("_"):
                payload[k] = _safe_json(v)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_json(v):
    """Quase tudo serializa direto; objetos complexos viram str."""
    if isinstance(v, (str, int, float, bool, type(None), list, dict, tuple)):
        return v
    return str(v)


# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------


_REQUEST_ID_HEADER = "X-Request-Id"


class RequestIdFilter(logging.Filter):
    """Anexa `request_id` ao LogRecord quando há request context.

    Lê de `flask.g.request_id` (setado em `_before_request`). Fora de
    request context (ex.: log no startup), o campo vira "-"."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = getattr(g, "request_id", "-")
        except RuntimeError:
            record.request_id = "-"  # fora de request context
        return True


def register_request_id(app: Flask) -> None:
    """Middleware que garante request_id em `g` e na resposta."""

    @app.before_request
    def _before_request():
        rid = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
        g.request_id = rid
        g._req_start = time.perf_counter()

    @app.after_request
    def _after_request(response: Response) -> Response:
        rid = getattr(g, "request_id", None)
        if rid:
            response.headers[_REQUEST_ID_HEADER] = rid
        return response

    # Filter aplicado em todos os handlers do app.logger
    f = RequestIdFilter()
    for h in app.logger.handlers:
        h.addFilter(f)
    app.logger.addFilter(f)


# ---------------------------------------------------------------------------
# Métricas Prometheus
# ---------------------------------------------------------------------------


def register_metrics(app: Flask) -> None:
    """Instala middleware de métricas + endpoint `/metrics`.

    Métricas expostas:
      - `aquag20_requests_total{method, endpoint, status}` (Counter)
      - `aquag20_request_latency_seconds{method, endpoint}` (Histogram)

    Registry isolado (não usa o global) — útil pra resetar entre testes
    e evitar conflitos com outras libs.
    """
    registry = CollectorRegistry()

    req_count = Counter(
        "aquag20_requests_total",
        "Total de requests HTTP por endpoint e status",
        ["method", "endpoint", "status"],
        registry=registry,
    )
    req_latency = Histogram(
        "aquag20_request_latency_seconds",
        "Latência de requests HTTP em segundos",
        ["method", "endpoint"],
        registry=registry,
        # Buckets ajustados pra app web: 5ms até 10s
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )

    # Expõe registry pra que tests possam zerar entre cenários
    app.extensions["prometheus_registry"] = registry
    app.extensions["prometheus_metrics"] = {
        "req_count": req_count,
        "req_latency": req_latency,
    }

    @app.after_request
    def _record(response: Response) -> Response:
        # Endpoint pode ser None (404 sem match). Use raw path como fallback.
        endpoint = request.endpoint or "<unknown>"
        # Não conta o próprio /metrics pra evitar feedback loop ruidoso
        if endpoint == "metrics":
            return response
        elapsed = time.perf_counter() - getattr(g, "_req_start", time.perf_counter())
        req_count.labels(
            method=request.method, endpoint=endpoint,
            status=str(response.status_code),
        ).inc()
        req_latency.labels(
            method=request.method, endpoint=endpoint,
        ).observe(elapsed)
        return response

    @app.route("/metrics")
    def metrics():
        body = generate_latest(registry)
        return Response(body, mimetype=CONTENT_TYPE_LATEST)

    # Isenta CSRF e qualquer wrap de login (não tem)
    from app.extensions import csrf
    csrf.exempt(metrics)


# ---------------------------------------------------------------------------
# Setup orquestrado
# ---------------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    import os
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def configure_observability(app: Flask) -> None:
    """Chamado por `create_app` depois dos handlers básicos.

    Sempre liga request-id (custo trivial). Métricas e JSON logs vêm
    por config flag pra não atrapalhar dev/test.

    Lê env DIRETO além de app.config — configs Flask são class-attrs
    avaliados em import-time; mudar env depois não atualiza."""
    register_request_id(app)

    metrics_on = (
        app.config.get("OBSERVABILITY_METRICS_ENABLED")
        or _env_flag("OBSERVABILITY_METRICS_ENABLED")
    )
    if metrics_on:
        register_metrics(app)
