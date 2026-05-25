"""Testes do IndicadoresService (3 KPIs)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.pedidos import (
    PoliticaPermuta,
    StatusPedido,
)
from app.models.pool import (
    EstadoGarrafao,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.indicadores_service import IndicadoresService
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.permuta_service import LinhaEntregaInput, PermutaService
from app.services.pool_service import PoolService


# Datas fixas para envelhecimento (independentes do dia em que o teste roda)
HOJE = date.today()
MES_PASSADO = (HOJE.replace(day=1) - timedelta(days=1))   # último dia mês anterior
MES_FUTURO_1 = (HOJE.replace(day=1) + timedelta(days=45))
MES_FUTURO_3 = (HOJE.replace(day=1) + timedelta(days=120))


@pytest.fixture
def ind_setup(app, two_tenants):
    """Universo: 1 tipo (com valor_reposicao=R$35), 1 CD, 1 veículo, cliente."""
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
        return {
            "tid": tid, "tipo": tipo.id, "cd": cd.id, "veh": veh.id, "cli": cli.id,
        }


# ---------------------------------------------------------------------------


class TestEnvelhecimento:
    def test_agrega_cheios_por_mes(self, app, ind_setup):
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            # 50 cheios validade mês passado (VENCIDO), 100 mês futuro
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=50, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_PASSADO, sinal=+1,
                observacao="seed vencido",
            )
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=100, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed futuro",
            )
            db.session.commit()

            faixas = IndicadoresService(db.session, ctx["tid"]).envelhecimento()
            assert len(faixas) == 2
            faixas_por_mes = {(f.ano, f.mes): f for f in faixas}
            f_passado = faixas_por_mes[(MES_PASSADO.year, MES_PASSADO.month)]
            assert f_passado.quantidade == 50
            assert f_passado.vencido is True

            f_futuro = faixas_por_mes[(MES_FUTURO_1.year, MES_FUTURO_1.month)]
            assert f_futuro.quantidade == 100
            assert f_futuro.vencido is False

    def test_ignora_vazios_e_avariados(self, app, ind_setup):
        """Envelhecimento mira CHEIOS — é deles que vem o risco de venda."""
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=200, local_id=ctx["cd"],
                estado=EstadoGarrafao.vazio, validade=MES_FUTURO_1, sinal=+1,
                observacao="vazios",
            )
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=10, local_id=ctx["cd"],
                estado=EstadoGarrafao.avariado, validade=MES_FUTURO_1, sinal=+1,
                observacao="avariados",
            )
            db.session.commit()
            faixas = IndicadoresService(db.session, ctx["tid"]).envelhecimento()
            assert faixas == []

    def test_ignora_quantidade_zero(self, app, ind_setup):
        """Saldo zerado (porque foi todo entregue) não aparece."""
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=10, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=10, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=-1,
                observacao="zera",
            )
            db.session.commit()
            faixas = IndicadoresService(db.session, ctx["tid"]).envelhecimento()
            assert faixas == []

    def test_corta_alem_da_janela_futura(self, app, ind_setup):
        """Cheio com validade > hoje + 6m (default) não aparece."""
        with app.app_context():
            ctx = ind_setup
            longe = HOJE + timedelta(days=400)
            PoolService(db.session, ctx["tid"]).registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=99, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=longe, sinal=+1,
                observacao="longe",
            )
            db.session.commit()
            faixas = IndicadoresService(db.session, ctx["tid"]).envelhecimento()
            assert faixas == []


# ---------------------------------------------------------------------------


class TestTaxaCasamento:
    def _gerar_permuta(self, ctx, validade_entregue, validade_recebida=None,
                      qtd=10, politica=PoliticaPermuta.casar):
        """Helper: seed cheios + cria pedido roteirizado + registra entrega."""
        pool = PoolService(db.session, ctx["tid"])
        pool.registrar_ajuste(
            tipo_garrafao_id=ctx["tipo"], quantidade=qtd, local_id=ctx["veh"],
            estado=EstadoGarrafao.cheio, validade=validade_entregue, sinal=+1,
            observacao="seed",
        )
        ped = PedidoService(db.session, ctx["tid"]).criar_pedido(
            cliente_id=ctx["cli"], politica_permuta=politica,
            itens=[ItemPedidoInput(ctx["tipo"], qtd, validade_entregue,
                                   Decimal("10.00"))],
        )
        PedidoService(db.session, ctx["tid"]).transicionar(ped, StatusPedido.roteirizado)
        PermutaService(db.session, ctx["tid"]).registrar_entrega(
            pedido=ped, veiculo_local_id=ctx["veh"],
            linhas=[LinhaEntregaInput(
                tipo_garrafao_id=ctx["tipo"], quantidade=qtd,
                validade_entregue=validade_entregue,
                validade_recebida=validade_recebida,
            )],
        )

    def test_tudo_casado_taxa_100(self, app, ind_setup):
        with app.app_context():
            ctx = ind_setup
            self._gerar_permuta(ctx, MES_FUTURO_1, qtd=20)  # casado default
            self._gerar_permuta(ctx, MES_FUTURO_3, qtd=10)
            db.session.commit()
            t = IndicadoresService(db.session, ctx["tid"]).taxa_casamento()
            assert t.permutas_quantidade_total == 30
            assert t.permutas_quantidade_casada == 30
            assert t.percentual == Decimal("100")

    def test_metade_casado_taxa_50(self, app, ind_setup):
        with app.app_context():
            ctx = ind_setup
            self._gerar_permuta(ctx, MES_FUTURO_1, qtd=10)                    # casado
            self._gerar_permuta(ctx, MES_FUTURO_3,
                                validade_recebida=MES_FUTURO_1, qtd=10)       # descasado
            db.session.commit()
            t = IndicadoresService(db.session, ctx["tid"]).taxa_casamento()
            assert t.permutas_quantidade_total == 20
            assert t.permutas_quantidade_casada == 10
            assert t.percentual == Decimal("50")

    def test_sem_permutas_taxa_zero_sem_erro(self, app, ind_setup):
        """Sem permutas o serviço retorna 0/0 → taxa=0 (não divisão por zero)."""
        with app.app_context():
            ctx = ind_setup
            t = IndicadoresService(db.session, ctx["tid"]).taxa_casamento()
            assert t.permutas_quantidade_total == 0
            assert t.permutas_quantidade_casada == 0
            assert t.taxa == Decimal("0")
            assert t.percentual == Decimal("0")


# ---------------------------------------------------------------------------


class TestCustoReposicao:
    def test_descarte_soma_valor_de_reposicao(self, app, ind_setup):
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=10, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            pool.registrar_descarte(
                tipo_garrafao_id=ctx["tipo"], quantidade=3,
                local_origem_id=ctx["cd"], estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, observacao="dia 1",
            )
            db.session.commit()
            c = IndicadoresService(db.session, ctx["tid"]).custo_reposicao()
            assert c.descarte_unidades == 3
            assert c.descarte_valor == Decimal("105.00")  # 3 * 35
            assert c.avaria_unidades == 0
            assert c.total_valor == Decimal("105.00")

    def test_avaria_nao_e_duplicada(self, app, ind_setup):
        """Avaria gera 2 movimentos com mesma quantidade — só 1 entra no
        custo (linha estado='avariado')."""
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=5, local_id=ctx["cd"],
                estado=EstadoGarrafao.vazio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            pool.registrar_avaria(
                tipo_garrafao_id=ctx["tipo"], quantidade=2, local_id=ctx["cd"],
                estado_origem=EstadoGarrafao.vazio, validade=MES_FUTURO_1,
                observacao="estourou",
            )
            db.session.commit()
            c = IndicadoresService(db.session, ctx["tid"]).custo_reposicao()
            assert c.avaria_unidades == 2     # NÃO 4
            assert c.avaria_valor == Decimal("70.00")  # 2 * 35

    def test_descarte_e_avaria_somam(self, app, ind_setup):
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=20, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            pool.registrar_descarte(
                tipo_garrafao_id=ctx["tipo"], quantidade=3,
                local_origem_id=ctx["cd"], estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, observacao="dia 1",
            )
            pool.registrar_avaria(
                tipo_garrafao_id=ctx["tipo"], quantidade=2, local_id=ctx["cd"],
                estado_origem=EstadoGarrafao.cheio, validade=MES_FUTURO_1,
                observacao="quebrou",
            )
            db.session.commit()
            c = IndicadoresService(db.session, ctx["tid"]).custo_reposicao()
            assert c.descarte_unidades == 3
            assert c.avaria_unidades == 2
            assert c.total_unidades == 5
            assert c.total_valor == Decimal("175.00")  # 5 * 35

    def test_tipo_sem_valor_reposicao_conta_zero(self, app, ind_setup, two_tenants):
        """Tipo cadastrado sem valor_reposicao → unidades contam, valor não."""
        with app.app_context():
            ctx = ind_setup
            tid = ctx["tid"]
            tipo_sem_valor = TipoGarrafao(
                tenant_id=tid, nome="20L sem valor",
                material=MaterialGarrafao.PC,
                capacidade_litros=Decimal("20.00"),
                valor_reposicao=None,
            )
            db.session.add(tipo_sem_valor)
            db.session.flush()

            pool = PoolService(db.session, tid)
            pool.registrar_ajuste(
                tipo_garrafao_id=tipo_sem_valor.id, quantidade=10,
                local_id=ctx["cd"], estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, sinal=+1, observacao="seed",
            )
            pool.registrar_descarte(
                tipo_garrafao_id=tipo_sem_valor.id, quantidade=4,
                local_origem_id=ctx["cd"], estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, observacao="d",
            )
            db.session.commit()
            c = IndicadoresService(db.session, tid).custo_reposicao()
            assert c.descarte_unidades == 4
            assert c.descarte_valor == Decimal("0")

    def test_envase_e_transferencia_nao_contam(self, app, ind_setup):
        """Envase/transferência não são "perda" — não entram no custo."""
        with app.app_context():
            ctx = ind_setup
            ind = LocalEstoque(tenant_id=ctx["tid"], tipo=TipoLocal.industria,
                                nome="Indústria")
            db.session.add(ind)
            db.session.flush()
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=20, local_id=ind.id,
                estado=EstadoGarrafao.vazio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            from app.services.envase_service import EnvaseService
            EnvaseService(db.session, ctx["tid"]).registrar_envase(
                tipo_garrafao_id=ctx["tipo"], quantidade=10,
                local_industria_id=ind.id, validade=MES_FUTURO_1,
                observacao="env",
            )
            db.session.commit()
            c = IndicadoresService(db.session, ctx["tid"]).custo_reposicao()
            assert c.total_unidades == 0
            assert c.total_valor == Decimal("0")


# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_combina_3_kpis(self, app, ind_setup):
        with app.app_context():
            ctx = ind_setup
            pool = PoolService(db.session, ctx["tid"])
            pool.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo"], quantidade=10, local_id=ctx["cd"],
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed",
            )
            db.session.commit()
            snap = IndicadoresService(db.session, ctx["tid"]).snapshot()
            assert set(snap.keys()) == {"envelhecimento", "casamento", "custo_reposicao"}
            assert len(snap["envelhecimento"]) == 1


class TestIsolamentoTenant:
    def test_kpis_de_a_nao_veem_dados_de_b(self, app, two_tenants):
        """B mexe no pool dele; KPIs de A devem ser zerados."""
        with app.app_context():
            tid_a = two_tenants["a"]["tenant_id"]
            tid_b = two_tenants["b"]["tenant_id"]
            tipo_b = TipoGarrafao(
                tenant_id=tid_b, nome="20L B", material=MaterialGarrafao.PC,
                capacidade_litros=Decimal("20.00"),
                valor_reposicao=Decimal("99.99"),
            )
            cd_b = LocalEstoque(tenant_id=tid_b, tipo=TipoLocal.cd, nome="CD B")
            db.session.add_all([tipo_b, cd_b])
            db.session.flush()
            PoolService(db.session, tid_b).registrar_ajuste(
                tipo_garrafao_id=tipo_b.id, quantidade=999, local_id=cd_b.id,
                estado=EstadoGarrafao.cheio, validade=MES_FUTURO_1, sinal=+1,
                observacao="seed B",
            )
            PoolService(db.session, tid_b).registrar_descarte(
                tipo_garrafao_id=tipo_b.id, quantidade=10,
                local_origem_id=cd_b.id, estado=EstadoGarrafao.cheio,
                validade=MES_FUTURO_1, observacao="B",
            )
            db.session.commit()

            svc_a = IndicadoresService(db.session, tid_a)
            assert svc_a.envelhecimento() == []
            assert svc_a.taxa_casamento().permutas_quantidade_total == 0
            assert svc_a.custo_reposicao().total_unidades == 0
