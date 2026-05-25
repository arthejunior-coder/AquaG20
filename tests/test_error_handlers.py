"""Testes dos handlers globais 403/404/500."""

from __future__ import annotations

import pytest

from app.auth.password import hash_password
from app.extensions import db
from app.models.tenant import PapelUsuario, Usuario


@pytest.fixture
def att_user(app, two_tenants):
    """Cria usuário com papel='atendimento' em A (sem acesso a /financeiro)."""
    with app.app_context():
        u = Usuario(
            tenant_id=two_tenants["a"]["tenant_id"],
            nome="Att A", email="att@a.com",
            senha_hash=hash_password("senha-att-123"),
            papel=PapelUsuario.atendimento,
        )
        db.session.add(u)
        db.session.commit()


class TestErrorHandlers:
    def test_404_renderiza_template_amigavel(self, client, two_tenants, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/rota-que-nao-existe-mesmo")
        assert r.status_code == 404
        assert b"P" in r.data and b"404" in r.data
        # Deve incluir o link "Voltar ao painel"
        assert b"painel" in r.data.lower() or b"Painel" in r.data

    def test_404_em_recurso_id_inexistente(self, client, two_tenants, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/cadastros/clientes/99999")
        assert r.status_code == 404
        assert b"404" in r.data

    def test_403_papel_insuficiente(self, client, att_user, login_as):
        """Atendimento tenta acessar /financeiro/ → 403 + template amigável."""
        login_as(client, "att@a.com", "senha-att-123")
        r = client.get("/financeiro/")
        assert r.status_code == 403
        assert b"403" in r.data
        assert b"papel" in r.data.lower() or b"Acesso" in r.data

    def test_500_renderiza_template_quando_servico_levanta(
        self, app, client, two_tenants, login_as, monkeypatch
    ):
        """Monkeypatch IndicadoresService.snapshot pra estourar; handler
        deve devolver 500 + template amigável, sem vazar a mensagem.

        Em TestConfig, exceções propagam por padrão; trocamos para que
        o handler do Flask seja chamado, exatamente como em produção.
        """
        from app.services import indicadores_service

        def _explode(self):
            raise RuntimeError("explosão de teste")

        monkeypatch.setattr(
            indicadores_service.IndicadoresService, "snapshot", _explode,
        )

        app.config["PROPAGATE_EXCEPTIONS"] = False
        app.config["TESTING"] = False
        try:
            login_as(client, "admin@a.com", "senha-A-123")
            r = client.get("/")
            assert r.status_code == 500
            assert b"500" in r.data
            assert b"explos" not in r.data    # mensagem não vaza
            assert b"Traceback" not in r.data
        finally:
            app.config["PROPAGATE_EXCEPTIONS"] = None
            app.config["TESTING"] = True
