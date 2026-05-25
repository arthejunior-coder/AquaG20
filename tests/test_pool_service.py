"""Bateria pesada do PoolService — coração do projeto.

Cobre as 4 invariantes:
  (1) saldo reconstruível a partir dos movimentos
  (2) só compra/descarte alteram tamanho do pool
  (3) quantidade >= 0; abaixo disso, rollback
  (4) (FOR UPDATE não testável trivialmente — testado indiretamente
       pela ausência de race em testes sequenciais)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    GarrafaoSaldo,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
    TipoMovimento,
)
from app.services.pool_service import (
    Delta,
    EstoqueInsuficienteError,
    InvariantePoolViolada,
    PoolService,
)


# ---------------------------------------------------------------------------
# Fixtures locais — universo mínimo de pool por tenant
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_setup(app, two_tenants):
    """Cria 1 tipo de garrafão + 3 locais (CD, veículo, indústria) por tenant.

    Retorna dict com IDs prontos para uso nos testes.
    """
    with app.app_context():
        ctx = {}
        for k in ("a", "b"):
            tid = two_tenants[k]["tenant_id"]
            tipo = TipoGarrafao(
                tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
                capacidade_litros=Decimal("20.00"), valor_reposicao=Decimal("35.00"),
            )
            cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito")
            veic = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão 1")
            ind = LocalEstoque(tenant_id=tid, tipo=TipoLocal.industria, nome="Indústria Águas Boas")
            db.session.add_all([tipo, cd, veic, ind])
            db.session.flush()
            ctx[k] = {
                "tenant_id": tid,
                "tipo_id": tipo.id,
                "cd_id": cd.id,
                "veic_id": veic.id,
                "ind_id": ind.id,
            }
        db.session.commit()
        return ctx


@pytest.fixture
def svc(app, pool_setup):
    """PoolService já amarrado ao tenant A."""
    with app.app_context():
        yield PoolService(db.session, tenant_id=pool_setup["a"]["tenant_id"])


def _saldo(session, tenant_id, tipo_id, local_id, estado, validade):
    """Helper de leitura direta no banco."""
    stmt = select(GarrafaoSaldo).where(
        GarrafaoSaldo.tenant_id == tenant_id,
        GarrafaoSaldo.tipo_garrafao_id == tipo_id,
        GarrafaoSaldo.local_id == local_id,
        GarrafaoSaldo.estado == estado,
        GarrafaoSaldo.validade == validade,
    )
    s = session.scalar(stmt)
    return s.quantidade if s else 0


VAL_2027 = date(2027, 6, 1)
VAL_2028 = date(2028, 6, 1)
VAL_2029 = date(2029, 6, 1)


# ===========================================================================
# Bloco 1 — operações básicas
# ===========================================================================


class TestCompra:
    def test_compra_cria_saldo_vazio_no_destino(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=100,
                local_destino_id=ctx["cd_id"], validade=VAL_2029,
            )
            db.session.commit()
            qtd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                         ctx["cd_id"], EstadoGarrafao.vazio, VAL_2029)
            assert qtd == 100

    def test_compras_sucessivas_acumulam(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=60,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2029)
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=40,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2029)
            db.session.commit()
            qtd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                         ctx["cd_id"], EstadoGarrafao.vazio, VAL_2029)
            assert qtd == 100

    def test_compra_gera_movimento_no_livro_razao(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=50,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2029,
                                  observacao="NF 12345")
            db.session.commit()
            movs = db.session.scalars(select(GarrafaoMovimento)).all()
            assert len(movs) == 1
            m = movs[0]
            assert m.tipo == TipoMovimento.compra
            assert m.local_origem_id is None
            assert m.local_destino_id == ctx["cd_id"]
            assert m.quantidade == 50
            assert m.observacao == "NF 12345"


class TestDescarte:
    def test_descarte_subtrai_saldo(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=50,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2027)
            svc.registrar_descarte(tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                                    local_origem_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                                    validade=VAL_2027)
            db.session.commit()
            qtd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                         ctx["cd_id"], EstadoGarrafao.vazio, VAL_2027)
            assert qtd == 40

    def test_descarte_sem_saldo_levanta_e_faz_rollback(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            with pytest.raises(EstoqueInsuficienteError):
                svc.registrar_descarte(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=5,
                    local_origem_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                    validade=VAL_2029,
                )
            db.session.rollback()
            # Nenhum movimento nem saldo persistiram
            movs = db.session.scalars(select(GarrafaoMovimento)).all()
            assert len(movs) == 0

    def test_descarte_acima_do_saldo_levanta(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=20,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2027)
            db.session.commit()
            with pytest.raises(EstoqueInsuficienteError):
                svc.registrar_descarte(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=21,
                    local_origem_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                    validade=VAL_2027,
                )
            db.session.rollback()
            qtd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                         ctx["cd_id"], EstadoGarrafao.vazio, VAL_2027)
            assert qtd == 20


class TestTransferencia:
    def test_transferencia_clean(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=100,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2028)
            svc.registrar_transferencia(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=30,
                local_origem_id=ctx["cd_id"], local_destino_id=ctx["veic_id"],
                estado=EstadoGarrafao.vazio, validade=VAL_2028,
            )
            db.session.commit()
            qtd_cd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                            ctx["cd_id"], EstadoGarrafao.vazio, VAL_2028)
            qtd_veic = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                              ctx["veic_id"], EstadoGarrafao.vazio, VAL_2028)
            assert qtd_cd == 70
            assert qtd_veic == 30

    def test_transferencia_gera_movimento_unico_com_origem_e_destino(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2028)
            svc.registrar_transferencia(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=5,
                local_origem_id=ctx["cd_id"], local_destino_id=ctx["veic_id"],
                estado=EstadoGarrafao.vazio, validade=VAL_2028,
            )
            db.session.commit()
            movs = db.session.scalars(
                select(GarrafaoMovimento).where(GarrafaoMovimento.tipo == TipoMovimento.transferencia)
            ).all()
            assert len(movs) == 1
            assert movs[0].local_origem_id == ctx["cd_id"]
            assert movs[0].local_destino_id == ctx["veic_id"]

    def test_transferencia_para_mesmo_local_levanta(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            with pytest.raises(ValueError):
                svc.registrar_transferencia(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=5,
                    local_origem_id=ctx["cd_id"], local_destino_id=ctx["cd_id"],
                    estado=EstadoGarrafao.vazio, validade=VAL_2028,
                )


class TestAvaria:
    def test_avaria_move_cheio_para_avariado(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            # Setup: 50 cheios no veículo
            svc.aplicar_deltas(
                tipo=TipoMovimento.ajuste, tipo_garrafao_id=ctx["tipo_id"],
                deltas=[Delta(local_id=ctx["veic_id"], estado=EstadoGarrafao.cheio,
                              validade=VAL_2027, quantidade=50, sinal=+1)],
                observacao="setup teste",
            )
            svc.registrar_avaria(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=3,
                local_id=ctx["veic_id"], estado_origem=EstadoGarrafao.cheio,
                validade=VAL_2027,
            )
            db.session.commit()
            qtd_cheio = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                                ctx["veic_id"], EstadoGarrafao.cheio, VAL_2027)
            qtd_avar = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                               ctx["veic_id"], EstadoGarrafao.avariado, VAL_2027)
            assert qtd_cheio == 47
            assert qtd_avar == 3

    def test_avaria_gera_2_movimentos(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.aplicar_deltas(
                tipo=TipoMovimento.ajuste, tipo_garrafao_id=ctx["tipo_id"],
                deltas=[Delta(local_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                              validade=VAL_2027, quantidade=10, sinal=+1)],
                observacao="setup",
            )
            svc.registrar_avaria(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=2,
                local_id=ctx["cd_id"], estado_origem=EstadoGarrafao.vazio,
                validade=VAL_2027,
            )
            db.session.commit()
            movs_avaria = db.session.scalars(
                select(GarrafaoMovimento).where(GarrafaoMovimento.tipo == TipoMovimento.avaria)
            ).all()
            assert len(movs_avaria) == 2


class TestAjuste:
    def test_ajuste_positivo(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_ajuste(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=15,
                local_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                validade=VAL_2028, sinal=+1, observacao="inventário fisico bate +15",
            )
            db.session.commit()
            qtd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                         ctx["cd_id"], EstadoGarrafao.vazio, VAL_2028)
            assert qtd == 15

    def test_ajuste_sem_observacao_levanta(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            with pytest.raises(ValueError, match="observacao"):
                svc.registrar_ajuste(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=5,
                    local_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                    validade=VAL_2028, sinal=+1, observacao="",
                )


# ===========================================================================
# Bloco 2 — invariantes
# ===========================================================================


class TestInvariantePool:
    def test_transferencia_com_deltas_assimetricos_levanta(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            with pytest.raises(InvariantePoolViolada):
                svc.aplicar_deltas(
                    tipo=TipoMovimento.transferencia,
                    tipo_garrafao_id=ctx["tipo_id"],
                    deltas=[
                        Delta(local_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                              validade=VAL_2028, quantidade=10, sinal=-1),
                        Delta(local_id=ctx["veic_id"], estado=EstadoGarrafao.vazio,
                              validade=VAL_2028, quantidade=5, sinal=+1),  # !=10
                    ],
                )

    def test_compra_com_delta_negativo_levanta(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            with pytest.raises(InvariantePoolViolada):
                svc.aplicar_deltas(
                    tipo=TipoMovimento.compra,
                    tipo_garrafao_id=ctx["tipo_id"],
                    deltas=[Delta(local_id=ctx["cd_id"], estado=EstadoGarrafao.vazio,
                                  validade=VAL_2028, quantidade=10, sinal=-1)],
                )

    def test_delta_invalido_levanta(self):
        # sinal != ±1
        with pytest.raises(ValueError):
            Delta(local_id=1, estado=EstadoGarrafao.vazio, validade=VAL_2028,
                  quantidade=10, sinal=0)
        # quantidade <= 0
        with pytest.raises(ValueError):
            Delta(local_id=1, estado=EstadoGarrafao.vazio, validade=VAL_2028,
                  quantidade=0, sinal=+1)
        # validade None
        with pytest.raises(ValueError):
            Delta(local_id=1, estado=EstadoGarrafao.vazio, validade=None,
                  quantidade=10, sinal=+1)


# ===========================================================================
# Bloco 3 — isolamento tenant
# ===========================================================================


class TestIsolamentoTenant:
    def test_pool_service_so_ve_proprio_tenant(self, app, pool_setup):
        with app.app_context():
            ctx_a = pool_setup["a"]
            ctx_b = pool_setup["b"]
            # A compra 100; B compra 200
            svc_a = PoolService(db.session, tenant_id=ctx_a["tenant_id"])
            svc_b = PoolService(db.session, tenant_id=ctx_b["tenant_id"])
            svc_a.registrar_compra(tipo_garrafao_id=ctx_a["tipo_id"], quantidade=100,
                                    local_destino_id=ctx_a["cd_id"], validade=VAL_2029)
            svc_b.registrar_compra(tipo_garrafao_id=ctx_b["tipo_id"], quantidade=200,
                                    local_destino_id=ctx_b["cd_id"], validade=VAL_2029)
            db.session.commit()

            # Reconstrução por tenant não vê o outro
            div_a = svc_a.reconstruir_saldos()
            div_b = svc_b.reconstruir_saldos()
            assert div_a == []
            assert div_b == []

            qa = _saldo(db.session, ctx_a["tenant_id"], ctx_a["tipo_id"],
                        ctx_a["cd_id"], EstadoGarrafao.vazio, VAL_2029)
            qb = _saldo(db.session, ctx_b["tenant_id"], ctx_b["tipo_id"],
                        ctx_b["cd_id"], EstadoGarrafao.vazio, VAL_2029)
            assert qa == 100
            assert qb == 200


# ===========================================================================
# Bloco 4 — reconstrução / auditoria
# ===========================================================================


class TestReconstrucao:
    def test_reconstrucao_zero_divergencia_apos_varios_movs(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            # ~10 movimentos variados
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=100,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2027)
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=200,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2028)
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=300,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2029)
            svc.registrar_transferencia(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=50,
                local_origem_id=ctx["cd_id"], local_destino_id=ctx["veic_id"],
                estado=EstadoGarrafao.vazio, validade=VAL_2027,
            )
            svc.registrar_avaria(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                local_id=ctx["veic_id"], estado_origem=EstadoGarrafao.vazio,
                validade=VAL_2027,
            )
            svc.registrar_descarte(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                local_origem_id=ctx["veic_id"], estado=EstadoGarrafao.avariado,
                validade=VAL_2027,
            )
            db.session.commit()

            divergencias = svc.reconstruir_saldos(dry_run=True)
            assert divergencias == [], f"Divergencias: {divergencias}"

    def test_reconstrucao_detecta_corrupcao_de_saldo(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=100,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2027)
            db.session.commit()

            # Adulteração manual no saldo (simula bug/intervenção)
            saldo = db.session.scalar(
                select(GarrafaoSaldo).where(GarrafaoSaldo.tenant_id == ctx["tenant_id"])
            )
            saldo.quantidade = 999
            db.session.commit()

            divergencias = svc.reconstruir_saldos(dry_run=True)
            assert len(divergencias) == 1
            d = divergencias[0]
            assert d.esperado == 100
            assert d.real == 999

    def test_reconstrucao_apply_corrige_divergencia(self, app, svc, pool_setup):
        with app.app_context():
            ctx = pool_setup["a"]
            svc.registrar_compra(tipo_garrafao_id=ctx["tipo_id"], quantidade=100,
                                  local_destino_id=ctx["cd_id"], validade=VAL_2027)
            db.session.commit()

            saldo = db.session.scalar(
                select(GarrafaoSaldo).where(GarrafaoSaldo.tenant_id == ctx["tenant_id"])
            )
            saldo.quantidade = 999
            db.session.commit()

            svc.reconstruir_saldos(dry_run=False)
            db.session.commit()

            # Após apply, divergências sumiram
            assert svc.reconstruir_saldos() == []
            qtd = _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                         ctx["cd_id"], EstadoGarrafao.vazio, VAL_2027)
            assert qtd == 100
