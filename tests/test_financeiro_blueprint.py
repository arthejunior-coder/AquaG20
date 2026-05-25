"""Testes do blueprint /financeiro."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.auth.password import hash_password
from app.extensions import db
from app.models.cadastros import (
    CentroCusto,
    Cliente,
    Fornecedor,
    TipoCentroCusto,
    TipoCliente,
)
from app.models.financeiro import (
    Lancamento,
    NaturezaLancamento,
    StatusLancamento,
)
from app.models.tenant import PapelUsuario, Usuario
from app.services.financeiro_service import FinanceiroService


@pytest.fixture
def fin_bp_setup(app, two_tenants):
    """Adiciona usuário com papel='financeiro' em A + cli/forn/cc; cli em B."""
    with app.app_context():
        tid_a = two_tenants["a"]["tenant_id"]
        tid_b = two_tenants["b"]["tenant_id"]

        fin_user = Usuario(
            tenant_id=tid_a, nome="Fin A",
            email="fin@a.com", senha_hash=hash_password("senha-fin-123"),
            papel=PapelUsuario.financeiro,
        )
        # Usuário sem papel financeiro/admin — para testar 403
        att_user = Usuario(
            tenant_id=tid_a, nome="Atendente A",
            email="att@a.com", senha_hash=hash_password("senha-att-123"),
            papel=PapelUsuario.atendimento,
        )
        cli_a = Cliente(tenant_id=tid_a, nome="Cliente A", tipo=TipoCliente.varejo)
        forn_a = Fornecedor(tenant_id=tid_a, nome="Forn A")
        cc_a = CentroCusto(tenant_id=tid_a, nome="OpA",
                            tipo=TipoCentroCusto.operacional)
        cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_cli", tipo=TipoCliente.varejo)
        db.session.add_all([fin_user, att_user, cli_a, forn_a, cc_a, cli_b])
        db.session.commit()
        return {
            "tid_a": tid_a, "tid_b": tid_b,
            "cli_a": cli_a.id, "forn_a": forn_a.id, "cc_a": cc_a.id,
            "cli_b": cli_b.id,
        }


# ---------------------------------------------------------------------------


class TestAuth:
    def test_index_exige_login(self, client, fin_bp_setup):
        r = client.get("/financeiro/", follow_redirects=False)
        assert r.status_code == 302

    def test_papel_atendimento_nao_acessa(self, client, fin_bp_setup, login_as):
        login_as(client, "att@a.com", "senha-att-123")
        r = client.get("/financeiro/")
        assert r.status_code == 403

    def test_papel_financeiro_acessa(self, client, fin_bp_setup, login_as):
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.get("/financeiro/")
        assert r.status_code == 200

    def test_papel_admin_acessa(self, client, fin_bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/financeiro/")
        assert r.status_code == 200


# ---------------------------------------------------------------------------


class TestDashboard:
    def test_dashboard_vazio(self, client, fin_bp_setup, login_as):
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.get("/financeiro/")
        assert r.status_code == 200
        assert b"Fluxo de caixa" in r.data

    def test_dashboard_mostra_totais(self, client, app, fin_bp_setup, login_as):
        ctx = fin_bp_setup
        from datetime import timedelta
        with app.app_context():
            svc = FinanceiroService(db.session, ctx["tid_a"])
            # Lançamento a vencer em 10 dias
            svc.criar(natureza=NaturezaLancamento.receber,
                      descricao="prox 30d", valor=Decimal("500"),
                      vencimento=date.today() + timedelta(days=10),
                      cliente_id=ctx["cli_a"])
            db.session.commit()
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.get("/financeiro/")
        assert r.status_code == 200
        assert b"500.00" in r.data


# ---------------------------------------------------------------------------


class TestCRUD:
    def test_get_form_renderiza(self, client, fin_bp_setup, login_as):
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.get("/financeiro/lancamentos/novo")
        assert r.status_code == 200
        assert b"Novo lan" in r.data
        assert b"Cliente A" in r.data       # cliente do tenant A no select
        assert b"ZZZ_B_cli" not in r.data   # cliente de B NUNCA

    def test_cria_receber_via_form(self, client, app, fin_bp_setup, login_as):
        login_as(client, "fin@a.com", "senha-fin-123")
        ctx = fin_bp_setup
        r = client.post("/financeiro/lancamentos/novo", data={
            "natureza": "receber",
            "descricao": "Mensalidade",
            "valor": "250.00",
            "vencimento": "2026-06-15",
            "centro_custo_id": str(ctx["cc_a"]),
            "cliente_id": str(ctx["cli_a"]),
            "fornecedor_id": "",
            "pedido_id": "",
            "forma": "",
            "submit": "Salvar",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            l = db.session.scalar(db.select(Lancamento))
            assert l is not None
            assert l.tenant_id == ctx["tid_a"]
            assert l.natureza == NaturezaLancamento.receber
            assert l.cliente_id == ctx["cli_a"]
            assert l.valor == Decimal("250.00")

    def test_recusa_receber_com_fornecedor_via_form(
        self, client, app, fin_bp_setup, login_as
    ):
        login_as(client, "fin@a.com", "senha-fin-123")
        ctx = fin_bp_setup
        r = client.post("/financeiro/lancamentos/novo", data={
            "natureza": "receber",
            "descricao": "X",
            "valor": "10.00",
            "vencimento": "2026-06-15",
            "fornecedor_id": str(ctx["forn_a"]),   # incoerente
            "cliente_id": "",
            "submit": "Salvar",
        })
        assert r.status_code == 200
        assert b"receber" in r.data.lower()
        with app.app_context():
            assert db.session.scalar(db.select(db.func.count(Lancamento.id))) == 0

    def test_edita_lancamento(self, client, app, fin_bp_setup, login_as):
        login_as(client, "fin@a.com", "senha-fin-123")
        ctx = fin_bp_setup
        with app.app_context():
            l = FinanceiroService(db.session, ctx["tid_a"]).criar(
                natureza=NaturezaLancamento.pagar,
                descricao="Original", valor=Decimal("100"),
                vencimento=date(2026, 6, 1), fornecedor_id=ctx["forn_a"],
            )
            db.session.commit()
            lid = l.id
        r = client.post(f"/financeiro/lancamentos/{lid}", data={
            "natureza": "pagar",
            "descricao": "Editado",
            "valor": "150.00",
            "vencimento": "2026-06-15",
            "fornecedor_id": str(ctx["forn_a"]),
            "cliente_id": "",
            "centro_custo_id": "",
            "pedido_id": "",
            "forma": "",
            "submit": "Salvar",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            l = db.session.get(Lancamento, lid)
            assert l.descricao == "Editado"
            assert l.valor == Decimal("150.00")


# ---------------------------------------------------------------------------


class TestPagamento:
    def _criar(self, app, ctx):
        with app.app_context():
            l = FinanceiroService(db.session, ctx["tid_a"]).criar(
                natureza=NaturezaLancamento.receber,
                descricao="X", valor=Decimal("100"),
                vencimento=date(2026, 6, 1), cliente_id=ctx["cli_a"],
            )
            db.session.commit()
            return l.id

    def test_pagar_total_via_form(self, client, app, fin_bp_setup, login_as):
        lid = self._criar(app, fin_bp_setup)
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.post(f"/financeiro/lancamentos/{lid}/pagar", data={
            "pago_em": "2026-06-02",
            "valor_pago": "100.00",
            "forma": "pix",
            "submit": "Marcar pago",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            l = db.session.get(Lancamento, lid)
            assert l.status == StatusLancamento.pago
            assert l.valor_pago == Decimal("100.00")

    def test_pagar_parcial_via_form(self, client, app, fin_bp_setup, login_as):
        lid = self._criar(app, fin_bp_setup)
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.post(f"/financeiro/lancamentos/{lid}/pagar", data={
            "pago_em": "2026-06-02",
            "valor_pago": "40.00",
            "forma": "",
            "submit": "Marcar pago",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            l = db.session.get(Lancamento, lid)
            assert l.status == StatusLancamento.parcial

    def test_cancelar(self, client, app, fin_bp_setup, login_as):
        lid = self._criar(app, fin_bp_setup)
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.post(f"/financeiro/lancamentos/{lid}/cancelar",
                        follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            l = db.session.get(Lancamento, lid)
            assert l.status == StatusLancamento.cancelado

    def test_reabrir(self, client, app, fin_bp_setup, login_as):
        lid = self._criar(app, fin_bp_setup)
        login_as(client, "fin@a.com", "senha-fin-123")
        client.post(f"/financeiro/lancamentos/{lid}/pagar", data={
            "pago_em": "2026-06-02", "valor_pago": "100.00",
            "forma": "", "submit": "Marcar pago",
        })
        client.post(f"/financeiro/lancamentos/{lid}/reabrir")
        with app.app_context():
            l = db.session.get(Lancamento, lid)
            assert l.status == StatusLancamento.pendente
            assert l.pago_em is None


# ---------------------------------------------------------------------------


class TestIsolamentoTenant:
    def test_lista_de_a_nao_mostra_b(self, client, app, fin_bp_setup, login_as):
        ctx = fin_bp_setup
        with app.app_context():
            FinanceiroService(db.session, ctx["tid_b"]).criar(
                natureza=NaturezaLancamento.receber,
                descricao="ZZZ_B_lanc", valor=Decimal("999"),
                vencimento=date(2026, 6, 1), cliente_id=ctx["cli_b"],
            )
            db.session.commit()
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.get("/financeiro/lancamentos")
        assert r.status_code == 200
        assert b"ZZZ_B_lanc" not in r.data

    def test_404_lancamento_de_outro_tenant(self, client, app, fin_bp_setup, login_as):
        ctx = fin_bp_setup
        with app.app_context():
            l_b = FinanceiroService(db.session, ctx["tid_b"]).criar(
                natureza=NaturezaLancamento.receber,
                descricao="B", valor=Decimal("10"),
                vencimento=date(2026, 6, 1), cliente_id=ctx["cli_b"],
            )
            db.session.commit()
            lid = l_b.id
        login_as(client, "fin@a.com", "senha-fin-123")
        r = client.get(f"/financeiro/lancamentos/{lid}")
        assert r.status_code == 404
