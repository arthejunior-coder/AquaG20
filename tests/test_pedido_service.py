"""Testes do PedidoService — criação por faixa de validade + máquina de estados."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.pedidos import (
    CanalPedido,
    FormaPagamento,
    Pedido,
    PedidoItem,
    PoliticaPermuta,
    StatusPedido,
)
from app.models.pool import MaterialGarrafao, TipoGarrafao
from app.services.pedido_service import (
    ItemPedidoInput,
    PedidoInvalidoError,
    PedidoService,
    TransicaoInvalidaError,
)


VAL_2027 = date(2027, 6, 1)
VAL_2028 = date(2028, 6, 1)
VAL_2029 = date(2029, 6, 1)


@pytest.fixture
def pedido_setup(app, two_tenants):
    """1 cliente atacado + 2 tipos de garrafão, ambos do tenant A."""
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        cli = Cliente(tenant_id=tid, nome="Atacadista X", tipo=TipoCliente.atacado)
        t20 = TipoGarrafao(
            tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
        )
        t10 = TipoGarrafao(
            tenant_id=tid, nome="10L PET", material=MaterialGarrafao.PET,
            capacidade_litros=Decimal("10.00"),
        )
        db.session.add_all([cli, t20, t10])
        db.session.commit()
        return {
            "tenant_id": tid, "cliente_id": cli.id,
            "tipo_20": t20.id, "tipo_10": t10.id,
        }


# ---------------------------------------------------------------------------


class TestCriacao:
    def test_cria_pedido_atacado_3_linhas(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                politica_permuta=PoliticaPermuta.casar,
                forma_pagamento=FormaPagamento.prazo,
                canal=CanalPedido.whatsapp,
                itens=[
                    ItemPedidoInput(ctx["tipo_20"], 10, VAL_2027, Decimal("15.00")),
                    ItemPedidoInput(ctx["tipo_20"], 15, VAL_2028, Decimal("15.00")),
                    ItemPedidoInput(ctx["tipo_20"], 35, VAL_2029, Decimal("15.00")),
                ],
            )
            db.session.commit()

            assert pedido.id is not None
            assert pedido.status == StatusPedido.aberto
            assert pedido.qtd_total == 60
            assert pedido.valor_total == Decimal("900.00")  # 60 * 15
            assert len(pedido.itens) == 3
            assert [i.validade_solicitada for i in pedido.itens] == [
                VAL_2027, VAL_2028, VAL_2029
            ]

    def test_cria_pedido_varejo_validade_null(self, app, pedido_setup):
        """Varejo: 1 linha, validade NULL — operação 'qualquer validade'."""
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                politica_permuta=PoliticaPermuta.flexivel,
                itens=[ItemPedidoInput(ctx["tipo_20"], 2, None, Decimal("18.00"))],
            )
            db.session.commit()
            assert pedido.qtd_total == 2
            assert pedido.valor_total == Decimal("36.00")
            assert pedido.itens[0].validade_solicitada is None
            assert pedido.politica_permuta == PoliticaPermuta.flexivel

    def test_recusa_pedido_sem_itens(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            with pytest.raises(PedidoInvalidoError, match="pelo menos 1 item"):
                svc.criar_pedido(cliente_id=ctx["cliente_id"], itens=[])

    def test_recusa_item_com_qtd_zero(self, app, pedido_setup):
        with pytest.raises(PedidoInvalidoError, match="quantidade"):
            ItemPedidoInput(1, 0, VAL_2029)

    def test_recusa_item_preco_negativo(self, app, pedido_setup):
        with pytest.raises(PedidoInvalidoError, match="preco_unitario"):
            ItemPedidoInput(1, 10, VAL_2029, Decimal("-1.00"))

    def test_recusa_cliente_inexistente(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            with pytest.raises(PedidoInvalidoError, match="cliente"):
                svc.criar_pedido(
                    cliente_id=99999,
                    itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
                )

    def test_recusa_tipo_garrafao_inexistente(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            with pytest.raises(PedidoInvalidoError, match="tipos de garrafão"):
                svc.criar_pedido(
                    cliente_id=ctx["cliente_id"],
                    itens=[ItemPedidoInput(99999, 1, VAL_2029)],
                )


class TestTotaisDenormalizados:
    def test_adicionar_item_recalcula_totais(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 10, VAL_2029, Decimal("10.00"))],
            )
            db.session.flush()
            assert pedido.qtd_total == 10
            assert pedido.valor_total == Decimal("100.00")

            svc.adicionar_item(
                pedido, ItemPedidoInput(ctx["tipo_10"], 5, VAL_2029, Decimal("8.00"))
            )
            db.session.commit()
            assert pedido.qtd_total == 15
            assert pedido.valor_total == Decimal("140.00")  # 100 + 40

    def test_remover_item_recalcula_totais(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[
                    ItemPedidoInput(ctx["tipo_20"], 10, VAL_2029, Decimal("10.00")),
                    ItemPedidoInput(ctx["tipo_10"], 5, VAL_2029, Decimal("8.00")),
                ],
            )
            db.session.flush()
            assert pedido.qtd_total == 15

            svc.remover_item(pedido, pedido.itens[1])
            db.session.commit()
            assert pedido.qtd_total == 10
            assert pedido.valor_total == Decimal("100.00")
            assert len(pedido.itens) == 1


class TestMaquinaDeEstados:
    def test_fluxo_feliz_aberto_ate_entregue(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            svc.transicionar(pedido, StatusPedido.roteirizado)
            svc.transicionar(pedido, StatusPedido.em_entrega)
            svc.transicionar(pedido, StatusPedido.entregue)
            db.session.commit()
            assert pedido.status == StatusPedido.entregue

    def test_pode_cancelar_aberto(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            svc.cancelar(pedido)
            db.session.commit()
            assert pedido.status == StatusPedido.cancelado

    def test_pode_cancelar_roteirizado(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            svc.transicionar(pedido, StatusPedido.roteirizado)
            svc.cancelar(pedido)
            assert pedido.status == StatusPedido.cancelado

    def test_nao_pode_cancelar_em_entrega(self, app, pedido_setup):
        """Em entrega → não cancela (entregador na rua)."""
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            svc.transicionar(pedido, StatusPedido.roteirizado)
            svc.transicionar(pedido, StatusPedido.em_entrega)
            with pytest.raises(TransicaoInvalidaError):
                svc.cancelar(pedido)

    def test_pular_estado_levanta(self, app, pedido_setup):
        """aberto → em_entrega direto não é permitido."""
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            with pytest.raises(TransicaoInvalidaError):
                svc.transicionar(pedido, StatusPedido.em_entrega)

    def test_estado_final_nao_transita(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            svc.cancelar(pedido)
            with pytest.raises(TransicaoInvalidaError):
                svc.transicionar(pedido, StatusPedido.roteirizado)

    def test_nao_pode_editar_apos_roteirizado(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[ItemPedidoInput(ctx["tipo_20"], 1, VAL_2029)],
            )
            svc.transicionar(pedido, StatusPedido.roteirizado)
            with pytest.raises(TransicaoInvalidaError, match="aberto"):
                svc.adicionar_item(
                    pedido, ItemPedidoInput(ctx["tipo_10"], 1, VAL_2029)
                )


class TestIsolamentoTenant:
    def test_cliente_de_b_nao_aceito_em_pedido_de_a(self, app, two_tenants):
        with app.app_context():
            tid_a = two_tenants["a"]["tenant_id"]
            tid_b = two_tenants["b"]["tenant_id"]
            # Cliente em B
            cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_only_cliente",
                             tipo=TipoCliente.varejo)
            # Tipo em A
            tipo_a = TipoGarrafao(tenant_id=tid_a, nome="20L A",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            db.session.add_all([cli_b, tipo_a])
            db.session.commit()

            svc_a = PedidoService(db.session, tid_a)
            with pytest.raises(PedidoInvalidoError, match="cliente"):
                svc_a.criar_pedido(
                    cliente_id=cli_b.id,  # cliente de B
                    itens=[ItemPedidoInput(tipo_a.id, 1, VAL_2029)],
                )

    def test_tipo_garrafao_de_b_nao_aceito_em_pedido_de_a(self, app, two_tenants):
        with app.app_context():
            tid_a = two_tenants["a"]["tenant_id"]
            tid_b = two_tenants["b"]["tenant_id"]
            cli_a = Cliente(tenant_id=tid_a, nome="Cliente A", tipo=TipoCliente.varejo)
            tipo_b = TipoGarrafao(tenant_id=tid_b, nome="ZZZ_B_only_tipo",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            db.session.add_all([cli_a, tipo_b])
            db.session.commit()

            svc_a = PedidoService(db.session, tid_a)
            with pytest.raises(PedidoInvalidoError, match="tipos"):
                svc_a.criar_pedido(
                    cliente_id=cli_a.id,
                    itens=[ItemPedidoInput(tipo_b.id, 1, VAL_2029)],
                )

    def test_transicao_em_pedido_de_outro_tenant_levanta(self, app, two_tenants):
        with app.app_context():
            tid_a = two_tenants["a"]["tenant_id"]
            tid_b = two_tenants["b"]["tenant_id"]
            cli_b = Cliente(tenant_id=tid_b, nome="Cli B", tipo=TipoCliente.varejo)
            tipo_b = TipoGarrafao(tenant_id=tid_b, nome="20L B",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            db.session.add_all([cli_b, tipo_b])
            db.session.commit()

            ped_b = PedidoService(db.session, tid_b).criar_pedido(
                cliente_id=cli_b.id,
                itens=[ItemPedidoInput(tipo_b.id, 1, VAL_2029)],
            )
            db.session.commit()

            svc_a = PedidoService(db.session, tid_a)
            with pytest.raises(PermissionError):
                svc_a.transicionar(ped_b, StatusPedido.roteirizado)


class TestPersistencia:
    def test_itens_persistem_com_tenant_id_correto(self, app, pedido_setup):
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[
                    ItemPedidoInput(ctx["tipo_20"], 10, VAL_2027),
                    ItemPedidoInput(ctx["tipo_20"], 15, VAL_2028),
                ],
            )
            db.session.commit()

            itens = db.session.scalars(
                select(PedidoItem).where(PedidoItem.pedido_id == pedido.id)
            ).all()
            assert len(itens) == 2
            assert all(i.tenant_id == ctx["tenant_id"] for i in itens)
            assert all(i.qtd_atendida == 0 for i in itens)  # ainda não separado

    def test_cascade_delete_remove_itens(self, app, pedido_setup):
        """Deletar o Pedido remove itens (relationship cascade)."""
        with app.app_context():
            ctx = pedido_setup
            svc = PedidoService(db.session, ctx["tenant_id"])
            pedido = svc.criar_pedido(
                cliente_id=ctx["cliente_id"],
                itens=[
                    ItemPedidoInput(ctx["tipo_20"], 10, VAL_2027),
                    ItemPedidoInput(ctx["tipo_20"], 15, VAL_2028),
                ],
            )
            db.session.commit()
            pedido_id = pedido.id

            db.session.delete(pedido)
            db.session.commit()

            assert db.session.get(Pedido, pedido_id) is None
            itens = db.session.scalars(
                select(PedidoItem).where(PedidoItem.pedido_id == pedido_id)
            ).all()
            assert itens == []
