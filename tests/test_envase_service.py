"""Testes do EnvaseService.

Cobre as garantias específicas do envase:
  - 2 movimentos com MESMA validade (vasilhame não muda)
  - delta_total == 0 (invariante do pool — herdado do PoolService)
  - vazio decresce, cheio cresce, mesmo local indústria
  - rejeita local que não seja tipo='industria'
  - propaga EstoqueInsuficienteError quando faltam vazios
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
from app.services.envase_service import EnvaseService
from app.services.pool_service import (
    Delta,
    EstoqueInsuficienteError,
    PoolService,
)


VAL_2029 = date(2029, 6, 1)


@pytest.fixture
def envase_setup(app, two_tenants):
    """Universo: 1 tipo de garrafão + locais (CD + indústria) + 100 vazios no CD."""
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        tipo = TipoGarrafao(
            tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"), valor_reposicao=Decimal("35.00"),
        )
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito")
        ind = LocalEstoque(tenant_id=tid, tipo=TipoLocal.industria, nome="Indústria X")
        db.session.add_all([tipo, cd, ind])
        db.session.flush()
        # Seed: 100 vazios direto na indústria (ajuste, p/ poder envasar)
        pool = PoolService(db.session, tid)
        pool.registrar_ajuste(
            tipo_garrafao_id=tipo.id, quantidade=100,
            local_id=ind.id, estado=EstadoGarrafao.vazio,
            validade=VAL_2029, sinal=+1, observacao="seed teste",
        )
        db.session.commit()
        return {
            "tenant_id": tid,
            "tipo_id": tipo.id,
            "cd_id": cd.id,
            "ind_id": ind.id,
        }


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


# ---------------------------------------------------------------------------


class TestEnvase:
    def test_envase_decresce_vazio_e_cresce_cheio(self, app, envase_setup):
        with app.app_context():
            ctx = envase_setup
            svc = EnvaseService(db.session, ctx["tenant_id"])
            svc.registrar_envase(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=30,
                local_industria_id=ctx["ind_id"], validade=VAL_2029,
            )
            db.session.commit()
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["ind_id"], EstadoGarrafao.vazio, VAL_2029) == 70
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["ind_id"], EstadoGarrafao.cheio, VAL_2029) == 30

    def test_envase_gera_2_movimentos_mesma_validade(self, app, envase_setup):
        with app.app_context():
            ctx = envase_setup
            svc = EnvaseService(db.session, ctx["tenant_id"])
            svc.registrar_envase(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                local_industria_id=ctx["ind_id"], validade=VAL_2029,
            )
            db.session.commit()

            movs = db.session.scalars(
                select(GarrafaoMovimento).where(GarrafaoMovimento.tipo == TipoMovimento.envase)
            ).all()
            assert len(movs) == 2
            # Ambas com a MESMA validade
            assert {m.validade for m in movs} == {VAL_2029}
            estados = {m.estado for m in movs}
            assert estados == {EstadoGarrafao.vazio, EstadoGarrafao.cheio}

    def test_envase_tamanho_pool_inalterado(self, app, envase_setup):
        """Garante a invariante (2) do pool — envase não muda total."""
        with app.app_context():
            ctx = envase_setup
            tid = ctx["tenant_id"]
            # Total inicial em todos os estados/locais do tenant
            total_antes = db.session.scalar(
                select(db.func.coalesce(db.func.sum(GarrafaoSaldo.quantidade), 0))
                .where(GarrafaoSaldo.tenant_id == tid)
            )
            svc = EnvaseService(db.session, tid)
            svc.registrar_envase(
                tipo_garrafao_id=ctx["tipo_id"], quantidade=50,
                local_industria_id=ctx["ind_id"], validade=VAL_2029,
            )
            db.session.commit()
            total_depois = db.session.scalar(
                select(db.func.coalesce(db.func.sum(GarrafaoSaldo.quantidade), 0))
                .where(GarrafaoSaldo.tenant_id == tid)
            )
            assert total_antes == total_depois

    def test_envase_sem_vazios_levanta(self, app, envase_setup):
        with app.app_context():
            ctx = envase_setup
            svc = EnvaseService(db.session, ctx["tenant_id"])
            with pytest.raises(EstoqueInsuficienteError):
                svc.registrar_envase(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=999,
                    local_industria_id=ctx["ind_id"], validade=VAL_2029,
                )
            db.session.rollback()
            # Saldo intacto
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["ind_id"], EstadoGarrafao.vazio, VAL_2029) == 100
            assert _saldo(db.session, ctx["tenant_id"], ctx["tipo_id"],
                          ctx["ind_id"], EstadoGarrafao.cheio, VAL_2029) == 0


class TestValidacaoLocal:
    def test_envase_em_local_nao_industria_levanta(self, app, envase_setup):
        with app.app_context():
            ctx = envase_setup
            svc = EnvaseService(db.session, ctx["tenant_id"])
            with pytest.raises(ValueError, match="industria"):
                svc.registrar_envase(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                    local_industria_id=ctx["cd_id"],  # tipo=cd, não industria
                    validade=VAL_2029,
                )

    def test_envase_em_local_inexistente_levanta(self, app, envase_setup):
        with app.app_context():
            ctx = envase_setup
            svc = EnvaseService(db.session, ctx["tenant_id"])
            with pytest.raises(ValueError, match="não existe"):
                svc.registrar_envase(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                    local_industria_id=99999, validade=VAL_2029,
                )

    def test_envase_em_local_de_outro_tenant_levanta(self, app, envase_setup, two_tenants):
        """Local pode existir, mas se for de outro tenant, vira 'não existe'
        sob a ótica do EnvaseService de A — não confirma existência."""
        with app.app_context():
            ctx = envase_setup
            # Cria indústria no tenant B
            ind_b = LocalEstoque(
                tenant_id=two_tenants["b"]["tenant_id"],
                tipo=TipoLocal.industria, nome="Indústria B",
            )
            db.session.add(ind_b)
            db.session.commit()

            svc_a = EnvaseService(db.session, ctx["tenant_id"])
            with pytest.raises(ValueError, match="não existe"):
                svc_a.registrar_envase(
                    tipo_garrafao_id=ctx["tipo_id"], quantidade=10,
                    local_industria_id=ind_b.id, validade=VAL_2029,
                )
