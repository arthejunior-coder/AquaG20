"""Testes do dashboard real (KPIs renderizados na página)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.pedidos import PoliticaPermuta, StatusPedido
from app.models.pool import (
    EstadoGarrafao,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.permuta_service import LinhaEntregaInput, PermutaService
from app.services.pool_service import PoolService


HOJE = date.today()
MES_FUTURO_1 = (HOJE.replace(day=1) + timedelta(days=45))


@pytest.fixture
def dash_setup(app, two_tenants):
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        tipo = TipoGarrafao(
            tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
            valor_reposicao=Decimal("35.00"),
        )
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito")
        veh = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão")
        cli = Cliente(tenant_id=tid, nome="Cliente Z", tipo=TipoCliente.atacado)
        db.session.add_all([tipo, cd, veh, cli])
        db.session.commit()
        return {"tid": tid, "tipo": tipo.id, "cd": cd.id, "veh": veh.id, "cli": cli.id}


class TestDashboardReal:
    def test_anon_redireciona(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_dashboard_vazio_renderiza(self, client, dash_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")
        assert r.status_code == 200
        assert b"Painel" in r.data
        # 3 cards de KPI presentes
        assert b"Envelhecimento" in r.data
        assert b"Taxa de casamento" in r.data
        assert b"Reposi" in r.data  # Reposição mensal

    def test_dashboard_mostra_envelhecimento(self, client, app, dash_setup, login_as):
        with app.app_context():
            ctx = dash_setup
            PoolService(db.session, ctx["tid"]).registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=42, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            db.session.commit()
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")
        assert r.status_code == 200
        assert b"42" in r.data

    def test_dashboard_mostra_taxa_e_custo(self, client, app, dash_setup, login_as):
        """Cria fluxo completo: seed → pedido → entrega → descarte.
        Dashboard deve mostrar taxa de casamento 100% e custo de descarte."""
        with app.app_context():
            ctx = dash_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=20, local_id=ctx["veh"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed veh",
            )
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=10, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed cd",
            )
            # Pedido + entrega casada
            ped = PedidoService(db.session, ctx["tid"]).criar_pedido(
                cliente_id=ctx["cli"], politica_permuta=PoliticaPermuta.casar,
                itens=[ItemPedidoInput(ctx["tipo"], 5, MES_FUTURO_1, Decimal("10.00"))],
            )
            PedidoService(db.session, ctx["tid"]).transicionar(ped, StatusPedido.roteirizado)
            PermutaService(db.session, ctx["tid"]).registrar_entrega(
                pedido=ped, veiculo_local_id=ctx["veh"],
                linhas=[LinhaEntregaInput(ctx["tipo"], 5, MES_FUTURO_1)],
            )
            # Descarte de 2 cheios (R$70)
            pool.registrar_descarte(
                tipo_garrafao_id=ctx["tipo"], quantidade=2,
                local_origem_id=ctx["cd"], estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, observacao="dia",
            )
            db.session.commit()

        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")
        assert r.status_code == 200
        # Taxa de casamento 100.0%
        assert b"100.0%" in r.data
        # Custo de reposição R$ 70.00
        assert b"70.00" in r.data

    def test_isolamento_tenant_dashboard(self, client, app, dash_setup, two_tenants, login_as):
        """B descarta 100 garrafões; dashboard de A deve continuar zerado."""
        with app.app_context():
            tid_b = two_tenants["b"]["tenant_id"]
            tipo_b = TipoGarrafao(
                tenant_id=tid_b, nome="ZZZ_B_tipo", material=MaterialGarrafao.PC,
                capacidade_litros=Decimal("20.00"),
                valor_reposicao=Decimal("9999.99"),
            )
            cd_b = LocalEstoque(tenant_id=tid_b, tipo=TipoLocal.cd, nome="CD B")
            db.session.add_all([tipo_b, cd_b])
            db.session.flush()
            pool_b = PoolService(db.session, tid_b)
            pool_b.registrar_ajuste(
                tipo_garrafao_id=tipo_b.id, quantidade=200, local_id=cd_b.id,
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            pool_b.registrar_descarte(
                tipo_garrafao_id=tipo_b.id, quantidade=100,
                local_origem_id=cd_b.id, estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, observacao="B",
            )
            db.session.commit()

        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")
        assert r.status_code == 200
        # 999_999.x não deve aparecer (custo de B)
        assert b"999999" not in r.data
        assert b"9999.99" not in r.data
        # marcador de B nunca
        assert b"ZZZ_B" not in r.data
