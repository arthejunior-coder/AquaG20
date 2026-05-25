"""Sanity check do dashboard Grafana — parseável, estrutura esperada,
queries referenciando métricas que a app realmente expõe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_DASH = Path(__file__).resolve().parent.parent / "dashboards" / "grafana" / "aquag20-operations.json"


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(_DASH.read_text(encoding="utf-8"))


class TestEstrutura:
    def test_arquivo_existe_e_parseavel(self, dashboard):
        assert "title" in dashboard
        assert dashboard["title"] == "AquaG20 — Operations"

    def test_uid_estavel(self, dashboard):
        """uid é o que liga URL/links → não pode mudar leve."""
        assert dashboard["uid"] == "aquag20-ops"

    def test_schema_version_grafana_10(self, dashboard):
        # Grafana 10+ usa schemaVersion >= 38
        assert dashboard["schemaVersion"] >= 38

    def test_tem_painels(self, dashboard):
        assert len(dashboard["panels"]) >= 5
        # Tipos: cobre operacional básico
        kinds = {p["type"] for p in dashboard["panels"]}
        assert "stat" in kinds       # KPIs no topo
        assert "timeseries" in kinds # latência, requests/s
        assert "table" in kinds      # top endpoints

    def test_inputs_pede_datasource_prometheus(self, dashboard):
        """Importar pede que o usuário escolha um datasource Prometheus."""
        inputs = dashboard.get("__inputs", [])
        assert any(
            i.get("pluginId") == "prometheus" and i.get("name") == "DS_PROMETHEUS"
            for i in inputs
        )


class TestQueriesEsperadas:
    """Cada métrica usada nos painéis deve corresponder ao que a app expõe.

    Se renomearmos um Counter/Histogram, os painéis quebram silenciosamente —
    aqui pegamos isso no CI antes do deploy."""

    METRICAS_EXPOSTAS = {
        "aquag20_requests_total",
        "aquag20_request_latency_seconds_bucket",  # gerada pelo histogram
        "aquag20_request_latency_seconds_count",
        "aquag20_request_latency_seconds_sum",
    }

    def _todas_queries(self, dashboard):
        out = []
        for panel in dashboard["panels"]:
            for tgt in panel.get("targets", []):
                expr = tgt.get("expr")
                if expr:
                    out.append(expr)
        return out

    def test_so_referencia_metricas_aquag20(self, dashboard):
        import re
        # Pega cada nome de métrica `aquag20_*` ignorando label selectors {…}
        # e range vector selectors [duration].
        pattern = re.compile(r"\b(aquag20_\w+)\b")
        queries = self._todas_queries(dashboard)
        for q in queries:
            for base in pattern.findall(q):
                assert base in self.METRICAS_EXPOSTAS, (
                    f"Painel referencia métrica desconhecida {base!r} "
                    f"em query: {q!r}"
                )

    def test_tem_query_de_request_rate(self, dashboard):
        queries = self._todas_queries(dashboard)
        assert any("rate(aquag20_requests_total" in q for q in queries)

    def test_tem_query_de_latencia_percentil(self, dashboard):
        queries = self._todas_queries(dashboard)
        assert any(
            "histogram_quantile" in q and "aquag20_request_latency_seconds_bucket" in q
            for q in queries
        )

    def test_tem_query_de_erro(self, dashboard):
        queries = self._todas_queries(dashboard)
        assert any('status=~"5..' in q or 'status=~"[45]..' in q for q in queries)


class TestPrometheusExample:
    def test_arquivo_yaml_existe(self):
        p = _DASH.parent / "prometheus.yml.example"
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert "scrape_configs" in content
        assert "/metrics" in content
        assert "aquag20" in content
