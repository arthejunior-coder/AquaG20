"""Testes do RotaService — CRUD, paradas, transições."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.frota import Entregador, TipoVeiculo, Veiculo
from app.models.logistica import (
    Rota,
    RotaParada,
    StatusParada,
    StatusRota,
)
from app.models.pedidos import Pedido, PoliticaPermuta, StatusPedido
from app.models.pool import MaterialGarrafao, TipoGarrafao
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.rota_service import RotaInvalidaError, RotaService


VAL_2029 = date(2029, 6, 1)
HOJE = date.today()


@pytest.fixture
def rota_setup(app, two_tenants):
    """Universo: cliente + tipo + veículo + entregador + pedido aberto."""
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        cli = Cliente(tenant_id=tid, nome="Cliente Rota", tipo=TipoCliente.varejo)
        tipo = TipoGarrafao(
            tenant_id=tid, nome="20L", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
        )
        veh = Veiculo(tenant_id=tid, tipo=TipoVeiculo.caminhao, placa="ABC1234",
                      capacidade_garrafoes=200)
        ent = Entregador(tenant_id=tid, nome="João Motorista")
        db.session.add_all([cli, tipo, veh, ent])
        db.session.flush()
        ped = PedidoService(db.session, tid).criar_pedido(
            cliente_id=cli.id,
            itens=[ItemPedidoInput(tipo.id, 10, VAL_2029, Decimal("15.00"))],
        )
        db.session.commit()
        return {
            "tid": tid, "cli": cli.id, "tipo": tipo.id,
            "veh": veh.id, "ent": ent.id, "pedido": ped.id,
        }


# ---------------------------------------------------------------------------


class TestCriacao:
    def test_cria_rota_basica(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            r = RotaService(db.session, ctx["tid"]).criar_rota(
                data_rota=HOJE, veiculo_id=ctx["veh"], entregador_id=ctx["ent"],
            )
            db.session.commit()
            assert r.id is not None
            assert r.status == StatusRota.planejada
            assert r.tenant_id == ctx["tid"]
            assert r.veiculo_id == ctx["veh"]

    def test_sem_data_levanta(self, app, rota_setup):
        with app.app_context():
            with pytest.raises(RotaInvalidaError, match="data_rota"):
                RotaService(db.session, rota_setup["tid"]).criar_rota(
                    data_rota=None,
                )

    def test_veiculo_de_outro_tenant_levanta(self, app, rota_setup, two_tenants):
        with app.app_context():
            ctx = rota_setup
            veh_b = Veiculo(tenant_id=two_tenants["b"]["tenant_id"],
                             tipo=TipoVeiculo.caminhao, placa="XYZ9999",
                             capacidade_garrafoes=100)
            db.session.add(veh_b)
            db.session.commit()
            with pytest.raises(RotaInvalidaError, match="veículo"):
                RotaService(db.session, ctx["tid"]).criar_rota(
                    data_rota=HOJE, veiculo_id=veh_b.id,
                )


# ---------------------------------------------------------------------------


class TestParadas:
    def test_adiciona_parada_e_promove_pedido(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            parada = svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            db.session.commit()
            # Parada criada
            assert parada.rota_id == r.id
            assert parada.pedido_id == ctx["pedido"]
            assert parada.ordem == 0
            assert parada.status == StatusParada.pendente
            # Pedido foi promovido a roteirizado automaticamente
            ped = db.session.get(Pedido, ctx["pedido"])
            assert ped.status == StatusPedido.roteirizado

    def test_ordem_auto_incrementa(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            tid = ctx["tid"]
            # Cria mais um pedido pra ter 2 paradas
            cli2 = Cliente(tenant_id=tid, nome="Cli 2", tipo=TipoCliente.varejo)
            db.session.add(cli2)
            db.session.flush()
            ped2 = PedidoService(db.session, tid).criar_pedido(
                cliente_id=cli2.id,
                itens=[ItemPedidoInput(ctx["tipo"], 5, VAL_2029, Decimal("10.00"))],
            )
            svc = RotaService(db.session, tid)
            r = svc.criar_rota(data_rota=HOJE)
            p1 = svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            p2 = svc.adicionar_parada(r, pedido_id=ped2.id)
            db.session.commit()
            assert p1.ordem == 0
            assert p2.ordem == 1

    def test_nao_duplica_pedido_na_mesma_rota(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            with pytest.raises(RotaInvalidaError, match="já é parada"):
                svc.adicionar_parada(r, pedido_id=ctx["pedido"])

    def test_recusa_pedido_em_entrega(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            ped = db.session.get(Pedido, ctx["pedido"])
            ped.status = StatusPedido.em_entrega
            db.session.commit()

            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            with pytest.raises(RotaInvalidaError, match="aberto"):
                svc.adicionar_parada(r, pedido_id=ctx["pedido"])

    def test_remove_parada_em_rota_planejada(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            parada = svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            db.session.flush()
            pid = parada.id
            svc.remover_parada(parada)
            db.session.commit()
            assert db.session.get(RotaParada, pid) is None

    def test_nao_remove_parada_em_rota_iniciada(self, app, rota_setup):
        """Adiciona parada, inicia rota, tenta remover → recusa."""
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            parada = svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            with pytest.raises(RotaInvalidaError, match="planejada"):
                svc.remover_parada(parada)


# ---------------------------------------------------------------------------


class TestTransicoes:
    def test_iniciar_promove_pedidos_para_em_entrega(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            db.session.commit()
            assert r.status == StatusRota.em_andamento
            ped = db.session.get(Pedido, ctx["pedido"])
            assert ped.status == StatusPedido.em_entrega

    def test_iniciar_sem_paradas_levanta(self, app, rota_setup):
        with app.app_context():
            svc = RotaService(db.session, rota_setup["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            with pytest.raises(RotaInvalidaError, match="sem paradas"):
                svc.iniciar(r)

    def test_concluir_em_andamento(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            svc.concluir(r)
            assert r.status == StatusRota.concluida

    def test_cancelar_planejada(self, app, rota_setup):
        with app.app_context():
            svc = RotaService(db.session, rota_setup["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            svc.cancelar(r)
            assert r.status == StatusRota.cancelada

    def test_cancelar_em_andamento(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            svc.cancelar(r)
            assert r.status == StatusRota.cancelada

    def test_iniciar_concluida_levanta(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            svc.concluir(r)
            with pytest.raises(RotaInvalidaError, match="inválida"):
                svc.iniciar(r)


# ---------------------------------------------------------------------------


class TestMarcarEntregue:
    def test_marca_parada_entregue_com_quantidades(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            parada = svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            svc.marcar_parada_entregue(parada, qtd_entregue=10, qtd_recolhido=10)
            db.session.commit()
            assert parada.status == StatusParada.entregue
            assert parada.entregue_em is not None
            assert parada.qtd_entregue == 10
            assert parada.qtd_recolhido == 10

    def test_marca_falhou(self, app, rota_setup):
        with app.app_context():
            ctx = rota_setup
            svc = RotaService(db.session, ctx["tid"])
            r = svc.criar_rota(data_rota=HOJE)
            parada = svc.adicionar_parada(r, pedido_id=ctx["pedido"])
            svc.iniciar(r)
            svc.marcar_parada_falhou(parada)
            assert parada.status == StatusParada.falhou


# ---------------------------------------------------------------------------


class TestIsolamentoTenant:
    def test_outro_tenant_recusa(self, app, two_tenants, rota_setup):
        with app.app_context():
            ctx = rota_setup
            r_a = RotaService(db.session, ctx["tid"]).criar_rota(data_rota=HOJE)
            db.session.commit()
            svc_b = RotaService(db.session, two_tenants["b"]["tenant_id"])
            with pytest.raises(PermissionError):
                svc_b.editar_cabecalho(r_a, data_rota=HOJE)

    def test_pedido_de_outro_tenant_recusa(self, app, two_tenants, rota_setup):
        """Pedido de B não pode entrar em rota de A."""
        with app.app_context():
            ctx = rota_setup
            tid_b = two_tenants["b"]["tenant_id"]
            cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B", tipo=TipoCliente.varejo)
            tipo_b = TipoGarrafao(tenant_id=tid_b, nome="ZZZ_B_tipo",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            db.session.add_all([cli_b, tipo_b])
            db.session.flush()
            ped_b = PedidoService(db.session, tid_b).criar_pedido(
                cliente_id=cli_b.id,
                itens=[ItemPedidoInput(tipo_b.id, 1, VAL_2029, Decimal("10.00"))],
            )
            db.session.commit()
            svc_a = RotaService(db.session, ctx["tid"])
            r_a = svc_a.criar_rota(data_rota=HOJE)
            with pytest.raises(RotaInvalidaError, match="não existe"):
                svc_a.adicionar_parada(r_a, pedido_id=ped_b.id)
