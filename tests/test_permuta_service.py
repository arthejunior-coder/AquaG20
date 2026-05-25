"""Testes do PermutaService.

Cobre as garantias críticas:
  - Swap atômico no pool: -cheio veiculo / +vazio veiculo (delta_total=0)
  - Casado/concessão calculados (nunca confiar no input)
  - Política 'flexivel' → descasar NÃO é concessão
  - qtd_atendida é atualizada nos PedidoItem por (tipo + validade) e tem
    fallback para item com validade NULL (varejo)
  - Desbalanço → cliente.saldo_garrafoes (não cria movimento no pool)
  - Isolamento por tenant (PermissionError em pedido de outro tenant)
  - Estoque insuficiente propaga + rollback
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.pedidos import (
    Pedido,
    PedidoItem,
    Permuta,
    PoliticaPermuta,
    StatusPedido,
)
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoSaldo,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.permuta_service import (
    EntregaInvalidaError,
    LinhaEntregaInput,
    PermutaService,
)
from app.services.pool_service import EstoqueInsuficienteError, PoolService


VAL_2027 = date(2027, 6, 1)
VAL_2028 = date(2028, 6, 1)
VAL_2029 = date(2029, 6, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _saldo(session, tenant_id, tipo_id, local_id, estado, validade):
    s = session.scalar(
        select(GarrafaoSaldo).where(
            GarrafaoSaldo.tenant_id == tenant_id,
            GarrafaoSaldo.tipo_garrafao_id == tipo_id,
            GarrafaoSaldo.local_id == local_id,
            GarrafaoSaldo.estado == estado,
            GarrafaoSaldo.validade == validade,
        )
    )
    return s.quantidade if s else 0


@pytest.fixture
def entrega_setup(app, two_tenants):
    """Universo de entrega:
    - cliente atacado em A (politica casar default)
    - tipo 20L
    - veículo (LocalEstoque)
    - 100 cheios no veículo, validade 2027
    - pedido roteirizado com 1 item (30, validade 2027)
    """
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        cli = Cliente(tenant_id=tid, nome="Atacadista X", tipo=TipoCliente.atacado,
                       saldo_garrafoes=0)
        tipo = TipoGarrafao(tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
                             capacidade_litros=Decimal("20.00"))
        veiculo = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão 1")
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito")
        db.session.add_all([cli, tipo, veiculo, cd])
        db.session.flush()

        # Seed: 100 cheios no veículo, validade 2027
        pool = PoolService(db.session, tid)
        pool.registrar_ajuste(
            tipo_garrafao_id=tipo.id, quantidade=100, local_id=veiculo.id,
            estado=EstadoGarrafao.cheio, validade=VAL_2027, sinal=+1,
            observacao="seed teste",
        )

        # Pedido roteirizado
        ped_svc = PedidoService(db.session, tid)
        pedido = ped_svc.criar_pedido(
            cliente_id=cli.id, politica_permuta=PoliticaPermuta.casar,
            itens=[ItemPedidoInput(tipo.id, 30, VAL_2027, Decimal("15.00"))],
        )
        ped_svc.transicionar(pedido, StatusPedido.roteirizado)
        db.session.commit()

        return {
            "tenant_id": tid, "cliente_id": cli.id, "tipo_id": tipo.id,
            "veiculo_id": veiculo.id, "cd_id": cd.id, "pedido_id": pedido.id,
        }


# ---------------------------------------------------------------------------


class TestEntregaCasada:
    def test_swap_decresce_cheio_cresce_vazio_no_veiculo(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            svc = PermutaService(db.session, ctx["tenant_id"])
            permutas = svc.registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=30,
                    validade_entregue=VAL_2027,
                )],
            )
            db.session.commit()

            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["veiculo_id"], EstadoGarrafao.cheio, VAL_2027) == 70
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["veiculo_id"], EstadoGarrafao.vazio, VAL_2027) == 30

            assert len(permutas) == 1
            p = permutas[0]
            assert p.quantidade == 30
            assert bool(p.casado) is True
            assert bool(p.concessao) is False
            assert p.validade_entregue == VAL_2027
            assert p.validade_recebida == VAL_2027

    def test_pool_invariante_delta_zero(self, app, entrega_setup):
        """Permuta deve manter o TAMANHO TOTAL do pool inalterado."""
        with app.app_context():
            ctx = entrega_setup
            total_antes = db.session.scalar(
                select(db.func.coalesce(db.func.sum(GarrafaoSaldo.quantidade), 0))
                .where(GarrafaoSaldo.tenant_id == ctx["tenant_id"])
            )
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
            )
            db.session.commit()
            total_depois = db.session.scalar(
                select(db.func.coalesce(db.func.sum(GarrafaoSaldo.quantidade), 0))
                .where(GarrafaoSaldo.tenant_id == ctx["tenant_id"])
            )
            assert total_antes == total_depois

    def test_qtd_atendida_atualizada_no_item(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
            )
            db.session.commit()
            item = db.session.scalar(select(PedidoItem))
            assert item.qtd_atendida == 30
            assert item.qtd_solicitada == 30


class TestConcessao:
    def test_descasar_em_politica_casar_vira_concessao(self, app, entrega_setup):
        """Pedido com politica='casar', entrega cheio 2027 mas recebe vazio
        2028 → casado=False, concessao=True."""
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            assert pedido.politica_permuta == PoliticaPermuta.casar

            svc = PermutaService(db.session, ctx["tenant_id"])
            # Seed: precisa de vazios 2028 voltando? NÃO — vazio entra no
            # veículo a partir de NADA (sentido do pool: vazio veio do
            # cliente, é INPUT no veículo). Pool aceita +vazio sem saldo prévio.
            permutas = svc.registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=30,
                    validade_entregue=VAL_2027,
                    validade_recebida=VAL_2028,
                )],
            )
            db.session.commit()
            p = permutas[0]
            assert bool(p.casado) is False
            assert bool(p.concessao) is True
            assert p.validade_entregue == VAL_2027
            assert p.validade_recebida == VAL_2028

    def test_descasar_em_politica_flexivel_nao_e_concessao(self, app, entrega_setup):
        """politica='flexivel' → descasar é o NORMAL, não concessão."""
        with app.app_context():
            ctx = entrega_setup
            # Recria o pedido com política flexivel
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            pedido.politica_permuta = PoliticaPermuta.flexivel
            db.session.commit()

            svc = PermutaService(db.session, ctx["tenant_id"])
            permutas = svc.registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=30,
                    validade_entregue=VAL_2027, validade_recebida=VAL_2028,
                )],
            )
            db.session.commit()
            p = permutas[0]
            assert bool(p.casado) is False
            assert bool(p.concessao) is False

    def test_casado_e_calculado_nao_lido_do_input(self, app, entrega_setup):
        """Mesmo se o caller passasse casado=True num input falso (e ele
        nem está no DTO), o service calcula a verdade."""
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            permutas = PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027,
                                          validade_recebida=VAL_2027)],
            )
            db.session.commit()
            assert bool(permutas[0].casado) is True

    def test_validade_recebida_default_iguala_entregue(self, app, entrega_setup):
        """Sem validade_recebida → default = validade_entregue (casado)."""
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            permutas = PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
            )
            db.session.commit()
            assert permutas[0].validade_recebida == VAL_2027
            assert bool(permutas[0].casado) is True


class TestDesbalanco:
    def test_desbalanco_positivo_aumenta_saldo_garrafoes_cliente(self, app, entrega_setup):
        """Cliente devolveu MENOS do que levou: ele passa a DEVER (saldo>0)."""
        with app.app_context():
            ctx = entrega_setup
            cli = db.session.get(Cliente, ctx["cliente_id"])
            assert cli.saldo_garrafoes == 0

            pedido = db.session.get(Pedido, ctx["pedido_id"])
            PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
                desbalanco_garrafoes=2,
            )
            db.session.commit()
            db.session.refresh(cli)
            assert cli.saldo_garrafoes == 2

    def test_desbalanco_zero_nao_mexe_no_cliente(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            cli = db.session.get(Cliente, ctx["cliente_id"])
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
                desbalanco_garrafoes=0,
            )
            db.session.commit()
            db.session.refresh(cli)
            assert cli.saldo_garrafoes == 0


class TestValidacoes:
    def test_pedido_em_aberto_recusado(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            # Volta para aberto manualmente (não há transição reversa, fazemos no banco)
            pedido.status = StatusPedido.aberto
            db.session.commit()

            with pytest.raises(EntregaInvalidaError, match="aberto"):
                PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                    pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                    linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
                )

    def test_local_nao_veiculo_recusado(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            with pytest.raises(EntregaInvalidaError, match="veiculo"):
                PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                    pedido=pedido, veiculo_local_id=ctx["cd_id"],  # CD, não veículo
                    linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
                )

    def test_local_inexistente_recusado(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            with pytest.raises(EntregaInvalidaError, match="não existe"):
                PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                    pedido=pedido, veiculo_local_id=99999,
                    linhas=[LinhaEntregaInput(ctx["tipo_id"], 30, VAL_2027)],
                )

    def test_lista_vazia_recusada(self, app, entrega_setup):
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            with pytest.raises(EntregaInvalidaError, match="pelo menos 1 linha"):
                PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                    pedido=pedido, veiculo_local_id=ctx["veiculo_id"], linhas=[],
                )

    def test_quantidade_zero_no_dto_recusada(self, app, entrega_setup):
        with pytest.raises(EntregaInvalidaError, match="quantidade"):
            LinhaEntregaInput(1, 0, VAL_2027)

    def test_estoque_insuficiente_propaga_e_rollback(self, app, entrega_setup):
        """Veículo tem 100 cheios; pede 500 → EstoqueInsuficienteError, sem
        nada persistir."""
        with app.app_context():
            ctx = entrega_setup
            pedido = db.session.get(Pedido, ctx["pedido_id"])
            with pytest.raises(EstoqueInsuficienteError):
                PermutaService(db.session, ctx["tenant_id"]).registrar_entrega(
                    pedido=pedido, veiculo_local_id=ctx["veiculo_id"],
                    linhas=[LinhaEntregaInput(ctx["tipo_id"], 500, VAL_2027)],
                )
            db.session.rollback()
            # Saldos intactos
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["veiculo_id"], EstadoGarrafao.cheio, VAL_2027) == 100
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["veiculo_id"], EstadoGarrafao.vazio, VAL_2027) == 0
            assert db.session.scalar(select(db.func.count(Permuta.id))) == 0


class TestIsolamentoTenant:
    def test_pedido_de_b_em_service_de_a_levanta(self, app, two_tenants, entrega_setup):
        """Service de A não pode operar pedido de B (não confirma existência
        nem por permissão — levanta PermissionError indicando bug)."""
        with app.app_context():
            tid_a = two_tenants["a"]["tenant_id"]
            tid_b = two_tenants["b"]["tenant_id"]
            cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_cli", tipo=TipoCliente.varejo)
            tipo_b = TipoGarrafao(tenant_id=tid_b, nome="ZZZ_B_tipo",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            veh_b = LocalEstoque(tenant_id=tid_b, tipo=TipoLocal.veiculo,
                                  nome="Veiculo B")
            db.session.add_all([cli_b, tipo_b, veh_b])
            db.session.commit()

            pedido_b = PedidoService(db.session, tid_b).criar_pedido(
                cliente_id=cli_b.id,
                itens=[ItemPedidoInput(tipo_b.id, 1, VAL_2029, Decimal("10.00"))],
            )
            PedidoService(db.session, tid_b).transicionar(
                pedido_b, StatusPedido.roteirizado,
            )
            db.session.commit()

            svc_a = PermutaService(db.session, tid_a)
            with pytest.raises(PermissionError):
                svc_a.registrar_entrega(
                    pedido=pedido_b, veiculo_local_id=veh_b.id,
                    linhas=[LinhaEntregaInput(tipo_b.id, 1, VAL_2029)],
                )


class TestMatchItemPorValidade:
    def test_item_com_validade_NULL_recebe_qtd_atendida(self, app, two_tenants):
        """Pedido varejo (item validade=NULL) deve receber qtd_atendida
        mesmo quando entregue com validade concreta."""
        with app.app_context():
            tid = two_tenants["a"]["tenant_id"]
            cli = Cliente(tenant_id=tid, nome="Varejo Y", tipo=TipoCliente.varejo)
            tipo = TipoGarrafao(tenant_id=tid, nome="20L V", material=MaterialGarrafao.PC,
                                 capacidade_litros=Decimal("20.00"))
            veh = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão")
            db.session.add_all([cli, tipo, veh])
            db.session.flush()
            PoolService(db.session, tid).registrar_ajuste(
                tipo_garrafao_id=tipo.id, quantidade=10, local_id=veh.id,
                estado=EstadoGarrafao.cheio, validade=VAL_2027, sinal=+1,
                observacao="seed",
            )
            ped = PedidoService(db.session, tid).criar_pedido(
                cliente_id=cli.id, politica_permuta=PoliticaPermuta.flexivel,
                itens=[ItemPedidoInput(tipo.id, 5, None, Decimal("10.00"))],
            )
            PedidoService(db.session, tid).transicionar(ped, StatusPedido.roteirizado)
            db.session.commit()

            PermutaService(db.session, tid).registrar_entrega(
                pedido=ped, veiculo_local_id=veh.id,
                linhas=[LinhaEntregaInput(tipo.id, 5, VAL_2027)],
            )
            db.session.commit()
            assert ped.itens[0].qtd_atendida == 5
