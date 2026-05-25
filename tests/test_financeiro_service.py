"""Testes do FinanceiroService."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import (
    CentroCusto,
    Cliente,
    Fornecedor,
    TipoCentroCusto,
    TipoCliente,
)
from app.models.financeiro import (
    FormaLancamento,
    Lancamento,
    NaturezaLancamento,
    StatusLancamento,
)
from app.services.financeiro_service import (
    FinanceiroService,
    LancamentoInvalidoError,
)


@pytest.fixture
def fin_setup(app, two_tenants):
    """Cliente + Fornecedor + CentroCusto no tenant A; cliente em B com marcador."""
    with app.app_context():
        tid_a = two_tenants["a"]["tenant_id"]
        tid_b = two_tenants["b"]["tenant_id"]
        cli_a = Cliente(tenant_id=tid_a, nome="Cliente A", tipo=TipoCliente.varejo)
        forn_a = Fornecedor(tenant_id=tid_a, nome="Forn A")
        cc_a = CentroCusto(tenant_id=tid_a, nome="Operacional A",
                            tipo=TipoCentroCusto.operacional)
        cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_cli", tipo=TipoCliente.varejo)
        db.session.add_all([cli_a, forn_a, cc_a, cli_b])
        db.session.commit()
        return {
            "tid_a": tid_a, "tid_b": tid_b,
            "cli_a": cli_a.id, "forn_a": forn_a.id, "cc_a": cc_a.id,
            "cli_b": cli_b.id,
        }


# ---------------------------------------------------------------------------


class TestCriar:
    def test_cria_receber_basico(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            svc = FinanceiroService(db.session, ctx["tid_a"])
            lanc = svc.criar(
                natureza=NaturezaLancamento.receber,
                descricao="Mensalidade cliente A",
                valor=Decimal("250.00"),
                vencimento=date(2026, 6, 15),
                cliente_id=ctx["cli_a"],
                centro_custo_id=ctx["cc_a"],
            )
            db.session.commit()
            assert lanc.id is not None
            assert lanc.status == StatusLancamento.pendente
            assert lanc.tenant_id == ctx["tid_a"]
            assert lanc.cliente_id == ctx["cli_a"]
            assert lanc.fornecedor_id is None

    def test_cria_pagar_basico(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            lanc = FinanceiroService(db.session, ctx["tid_a"]).criar(
                natureza=NaturezaLancamento.pagar,
                descricao="Água indústria",
                valor=Decimal("1500.00"),
                vencimento=date(2026, 6, 30),
                fornecedor_id=ctx["forn_a"],
            )
            db.session.commit()
            assert lanc.natureza == NaturezaLancamento.pagar
            assert lanc.fornecedor_id == ctx["forn_a"]

    def test_recusa_receber_com_fornecedor(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            with pytest.raises(LancamentoInvalidoError, match="receber"):
                FinanceiroService(db.session, ctx["tid_a"]).criar(
                    natureza=NaturezaLancamento.receber,
                    descricao="X", valor=Decimal("10"), vencimento=date(2026, 6, 1),
                    fornecedor_id=ctx["forn_a"],
                )

    def test_recusa_pagar_com_cliente(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            with pytest.raises(LancamentoInvalidoError, match="pagar"):
                FinanceiroService(db.session, ctx["tid_a"]).criar(
                    natureza=NaturezaLancamento.pagar,
                    descricao="X", valor=Decimal("10"), vencimento=date(2026, 6, 1),
                    cliente_id=ctx["cli_a"],
                )

    def test_recusa_descricao_vazia(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            with pytest.raises(LancamentoInvalidoError, match="descricao"):
                FinanceiroService(db.session, ctx["tid_a"]).criar(
                    natureza=NaturezaLancamento.receber,
                    descricao="   ", valor=Decimal("10"),
                    vencimento=date(2026, 6, 1),
                )

    def test_recusa_valor_zero(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            with pytest.raises(LancamentoInvalidoError, match="valor"):
                FinanceiroService(db.session, ctx["tid_a"]).criar(
                    natureza=NaturezaLancamento.receber,
                    descricao="X", valor=Decimal("0"),
                    vencimento=date(2026, 6, 1),
                )

    def test_recusa_cliente_de_outro_tenant(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            with pytest.raises(LancamentoInvalidoError, match="cliente"):
                FinanceiroService(db.session, ctx["tid_a"]).criar(
                    natureza=NaturezaLancamento.receber,
                    descricao="X", valor=Decimal("10"),
                    vencimento=date(2026, 6, 1),
                    cliente_id=ctx["cli_b"],  # cliente de B
                )


# ---------------------------------------------------------------------------


class TestPagar:
    def _make(self, ctx):
        return FinanceiroService(db.session, ctx["tid_a"]).criar(
            natureza=NaturezaLancamento.receber,
            descricao="X", valor=Decimal("100.00"),
            vencimento=date(2026, 6, 1), cliente_id=ctx["cli_a"],
        )

    def test_pagar_total(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            l = self._make(ctx)
            svc = FinanceiroService(db.session, ctx["tid_a"])
            svc.marcar_pago(l, pago_em=date(2026, 6, 2),
                            forma=FormaLancamento.pix)
            db.session.commit()
            assert l.status == StatusLancamento.pago
            assert l.valor_pago == Decimal("100.00")
            assert l.pago_em == date(2026, 6, 2)
            assert l.forma == FormaLancamento.pix

    def test_pagar_parcial(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            l = self._make(ctx)
            FinanceiroService(db.session, ctx["tid_a"]).marcar_pago(
                l, pago_em=date(2026, 6, 5), valor_pago=Decimal("40.00"),
            )
            db.session.commit()
            assert l.status == StatusLancamento.parcial
            assert l.valor_pago == Decimal("40.00")

    def test_recusa_pagar_mais_que_devido(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            l = self._make(ctx)
            with pytest.raises(LancamentoInvalidoError, match="maior"):
                FinanceiroService(db.session, ctx["tid_a"]).marcar_pago(
                    l, pago_em=date(2026, 6, 1), valor_pago=Decimal("200.00"),
                )

    def test_recusa_pagar_cancelado(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            l = self._make(ctx)
            svc = FinanceiroService(db.session, ctx["tid_a"])
            svc.cancelar(l)
            db.session.commit()
            with pytest.raises(LancamentoInvalidoError, match="cancelado"):
                svc.marcar_pago(l, pago_em=date(2026, 6, 1))


class TestCancelarReabrir:
    def test_cancelar_pendente(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            svc = FinanceiroService(db.session, ctx["tid_a"])
            l = svc.criar(natureza=NaturezaLancamento.pagar,
                          descricao="X", valor=Decimal("10"),
                          vencimento=date(2026, 6, 1))
            svc.cancelar(l)
            assert l.status == StatusLancamento.cancelado

    def test_nao_cancela_pago(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            svc = FinanceiroService(db.session, ctx["tid_a"])
            l = svc.criar(natureza=NaturezaLancamento.pagar,
                          descricao="X", valor=Decimal("10"),
                          vencimento=date(2026, 6, 1))
            svc.marcar_pago(l, pago_em=date(2026, 6, 1))
            with pytest.raises(LancamentoInvalidoError, match="pendente"):
                svc.cancelar(l)

    def test_reabrir_zera_pagamento(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            svc = FinanceiroService(db.session, ctx["tid_a"])
            l = svc.criar(natureza=NaturezaLancamento.pagar,
                          descricao="X", valor=Decimal("10"),
                          vencimento=date(2026, 6, 1))
            svc.marcar_pago(l, pago_em=date(2026, 6, 1))
            svc.reabrir(l)
            assert l.status == StatusLancamento.pendente
            assert l.pago_em is None
            assert l.valor_pago is None


# ---------------------------------------------------------------------------


class TestFluxoMensal:
    def test_agrega_receber_e_pagar_por_mes(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            svc = FinanceiroService(db.session, ctx["tid_a"])

            # 2026-06: 250 receber previsto + 100 realizado
            l1 = svc.criar(natureza=NaturezaLancamento.receber,
                           descricao="A", valor=Decimal("250"),
                           vencimento=date(2026, 6, 10), cliente_id=ctx["cli_a"])
            l2 = svc.criar(natureza=NaturezaLancamento.receber,
                           descricao="B", valor=Decimal("100"),
                           vencimento=date(2026, 6, 15), cliente_id=ctx["cli_a"])
            svc.marcar_pago(l2, pago_em=date(2026, 6, 20))

            # 2026-06: 500 pagar previsto + 0 realizado
            svc.criar(natureza=NaturezaLancamento.pagar,
                      descricao="C", valor=Decimal("500"),
                      vencimento=date(2026, 6, 25), fornecedor_id=ctx["forn_a"])

            # 2026-07: 300 receber previsto, 0 realizado
            svc.criar(natureza=NaturezaLancamento.receber,
                      descricao="D", valor=Decimal("300"),
                      vencimento=date(2026, 7, 1), cliente_id=ctx["cli_a"])
            db.session.commit()

            fluxos = svc.fluxo_mensal(inicio=date(2026, 1, 1), fim=date(2026, 12, 31))
            # Esperado: (2026, 6, receber): 350 prev, 100 real
            #            (2026, 6, pagar): 500 prev, 0 real
            #            (2026, 7, receber): 300 prev, 0 real
            mapa = {(f.ano, f.mes, f.natureza): f for f in fluxos}
            jun_rec = mapa[(2026, 6, NaturezaLancamento.receber)]
            assert jun_rec.previsto == Decimal("350")
            assert jun_rec.realizado == Decimal("100")
            jun_pag = mapa[(2026, 6, NaturezaLancamento.pagar)]
            assert jun_pag.previsto == Decimal("500")
            assert jun_pag.realizado == Decimal("0")
            jul_rec = mapa[(2026, 7, NaturezaLancamento.receber)]
            assert jul_rec.previsto == Decimal("300")
            assert jul_rec.realizado == Decimal("0")

    def test_cancelados_nao_entram_no_previsto(self, app, fin_setup):
        with app.app_context():
            ctx = fin_setup
            svc = FinanceiroService(db.session, ctx["tid_a"])
            svc.criar(natureza=NaturezaLancamento.receber,
                      descricao="OK", valor=Decimal("100"),
                      vencimento=date(2026, 6, 1), cliente_id=ctx["cli_a"])
            cancelado = svc.criar(natureza=NaturezaLancamento.receber,
                                   descricao="X", valor=Decimal("999"),
                                   vencimento=date(2026, 6, 1), cliente_id=ctx["cli_a"])
            svc.cancelar(cancelado)
            db.session.commit()

            fluxos = svc.fluxo_mensal(inicio=date(2026, 6, 1), fim=date(2026, 6, 30))
            assert len(fluxos) == 1
            assert fluxos[0].previsto == Decimal("100")


class TestIsolamentoTenant:
    def test_fluxo_de_a_nao_inclui_b(self, app, fin_setup, two_tenants):
        with app.app_context():
            ctx = fin_setup
            # B cria 1 lançamento
            svc_b = FinanceiroService(db.session, ctx["tid_b"])
            svc_b.criar(natureza=NaturezaLancamento.receber,
                        descricao="ZZZ_B_lanc", valor=Decimal("9999"),
                        vencimento=date(2026, 6, 1), cliente_id=ctx["cli_b"])
            db.session.commit()

            # A: fluxo deve ser vazio
            svc_a = FinanceiroService(db.session, ctx["tid_a"])
            fluxos = svc_a.fluxo_mensal(inicio=date(2026, 1, 1), fim=date(2026, 12, 31))
            assert fluxos == []

    def test_pagar_lancamento_de_outro_tenant_levanta(self, app, fin_setup, two_tenants):
        with app.app_context():
            ctx = fin_setup
            lanc_b = FinanceiroService(db.session, ctx["tid_b"]).criar(
                natureza=NaturezaLancamento.receber,
                descricao="B", valor=Decimal("50"),
                vencimento=date(2026, 6, 1), cliente_id=ctx["cli_b"],
            )
            db.session.commit()
            svc_a = FinanceiroService(db.session, ctx["tid_a"])
            with pytest.raises(PermissionError):
                svc_a.marcar_pago(lanc_b, pago_em=date(2026, 6, 1))
