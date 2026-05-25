"""Testes do FEFOService — First-Expire, First-Out."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.pool import (
    EstadoGarrafao,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.fefo_service import FEFOService
from app.services.pool_service import PoolService


VAL_2027 = date(2027, 6, 1)
VAL_2028 = date(2028, 6, 1)
VAL_2029 = date(2029, 6, 1)


@pytest.fixture
def fefo_setup(app, two_tenants):
    """1 tipo + 1 local, com 3 lotes de cheios em validades diferentes:
        2027: 50 cheios   ← mais próximo do vencimento
        2028: 100 cheios
        2029: 200 cheios  ← mais recente
    """
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        tipo = TipoGarrafao(
            tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
        )
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito")
        db.session.add_all([tipo, cd])
        db.session.flush()

        pool = PoolService(db.session, tid)
        for validade, qtd in [(VAL_2027, 50), (VAL_2028, 100), (VAL_2029, 200)]:
            pool.registrar_ajuste(
                tipo_garrafao_id=tipo.id, quantidade=qtd,
                local_id=cd.id, estado=EstadoGarrafao.cheio,
                validade=validade, sinal=+1, observacao="seed teste",
            )
        db.session.commit()
        return {"tenant_id": tid, "tipo_id": tipo.id, "cd_id": cd.id}


class TestFEFO:
    def test_pequena_quantidade_usa_apenas_lote_mais_antigo(self, app, fefo_setup):
        with app.app_context():
            ctx = fefo_setup
            svc = FEFOService(db.session, ctx["tenant_id"])
            sug = svc.recomendar_lotes(
                tipo_garrafao_id=ctx["tipo_id"], local_id=ctx["cd_id"],
                quantidade=30,
            )
            assert len(sug.lotes) == 1
            assert sug.lotes[0].validade == VAL_2027
            assert sug.lotes[0].quantidade == 30
            assert sug.atende_totalmente

    def test_atravessa_lotes_em_ordem_de_validade(self, app, fefo_setup):
        """Pede 120: deve tirar TODOS os 50 de 2027 + 70 de 2028 (próximo mais antigo)."""
        with app.app_context():
            ctx = fefo_setup
            svc = FEFOService(db.session, ctx["tenant_id"])
            sug = svc.recomendar_lotes(
                tipo_garrafao_id=ctx["tipo_id"], local_id=ctx["cd_id"],
                quantidade=120,
            )
            assert len(sug.lotes) == 2
            assert sug.lotes[0].validade == VAL_2027
            assert sug.lotes[0].quantidade == 50
            assert sug.lotes[1].validade == VAL_2028
            assert sug.lotes[1].quantidade == 70
            assert sug.total_atendido == 120
            assert sug.atende_totalmente

    def test_atende_total_consume_3_lotes(self, app, fefo_setup):
        """Pede 350 (= 50+100+200). Deve esvaziar tudo, exatamente."""
        with app.app_context():
            ctx = fefo_setup
            svc = FEFOService(db.session, ctx["tenant_id"])
            sug = svc.recomendar_lotes(
                tipo_garrafao_id=ctx["tipo_id"], local_id=ctx["cd_id"],
                quantidade=350,
            )
            assert [l.quantidade for l in sug.lotes] == [50, 100, 200]
            assert sug.atende_totalmente
            assert sug.quantidade_faltando == 0

    def test_atende_parcial_quando_falta_saldo(self, app, fefo_setup):
        """Pede 1000 mas só há 350. Retorna parcial, não levanta."""
        with app.app_context():
            ctx = fefo_setup
            svc = FEFOService(db.session, ctx["tenant_id"])
            sug = svc.recomendar_lotes(
                tipo_garrafao_id=ctx["tipo_id"], local_id=ctx["cd_id"],
                quantidade=1000,
            )
            assert sug.total_atendido == 350
            assert sug.quantidade_faltando == 650
            assert not sug.atende_totalmente

    def test_quantidade_invalida_levanta(self, app, fefo_setup):
        with app.app_context():
            ctx = fefo_setup
            svc = FEFOService(db.session, ctx["tenant_id"])
            with pytest.raises(ValueError):
                svc.recomendar_lotes(
                    tipo_garrafao_id=ctx["tipo_id"], local_id=ctx["cd_id"],
                    quantidade=0,
                )

    def test_estado_diferente_de_cheio(self, app, fefo_setup):
        """Por default busca cheios; pedir vazios sem ter retorna vazio."""
        with app.app_context():
            ctx = fefo_setup
            svc = FEFOService(db.session, ctx["tenant_id"])
            sug = svc.recomendar_lotes(
                tipo_garrafao_id=ctx["tipo_id"], local_id=ctx["cd_id"],
                quantidade=10, estado=EstadoGarrafao.vazio,
            )
            assert sug.lotes == []
            assert sug.total_atendido == 0
            assert sug.quantidade_faltando == 10


class TestIsolamentoTenant:
    def test_fefo_de_a_nao_ve_saldos_de_b(self, app, two_tenants):
        """B cria saldo em 2027. FEFOService de A não enxerga."""
        with app.app_context():
            # Setup B: tipo + local + 100 cheios em 2027
            tid_b = two_tenants["b"]["tenant_id"]
            tipo_b = TipoGarrafao(tenant_id=tid_b, nome="20L PC B",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            cd_b = LocalEstoque(tenant_id=tid_b, tipo=TipoLocal.cd, nome="CD B")
            db.session.add_all([tipo_b, cd_b])
            db.session.flush()
            PoolService(db.session, tid_b).registrar_ajuste(
                tipo_garrafao_id=tipo_b.id, quantidade=100,
                local_id=cd_b.id, estado=EstadoGarrafao.cheio,
                validade=VAL_2027, sinal=+1, observacao="B",
            )
            db.session.commit()

            # A consulta o tipo_id/local_id de B → não vê nada
            tid_a = two_tenants["a"]["tenant_id"]
            svc_a = FEFOService(db.session, tid_a)
            sug = svc_a.recomendar_lotes(
                tipo_garrafao_id=tipo_b.id,  # ID de B
                local_id=cd_b.id,            # ID de B
                quantidade=10,
            )
            assert sug.lotes == []


class TestEndpointJSON:
    def test_endpoint_retorna_sugestao(self, client, fefo_setup, login_as):
        ctx = fefo_setup
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(
            f"/pool/fefo?tipo_garrafao_id={ctx['tipo_id']}"
            f"&local_id={ctx['cd_id']}&quantidade=120"
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["total_atendido"] == 120
        assert data["atende_totalmente"] is True
        assert len(data["lotes"]) == 2
        # FEFO: lote mais antigo primeiro
        assert data["lotes"][0]["validade"] == "2027-06-01"
        assert data["lotes"][0]["quantidade"] == 50

    def test_endpoint_parametros_invalidos(self, client, fefo_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pool/fefo?tipo_garrafao_id=abc&local_id=1&quantidade=10")
        assert r.status_code == 400
        assert "erro" in r.get_json()

    def test_endpoint_exige_autenticacao(self, client, fefo_setup):
        r = client.get("/pool/fefo?tipo_garrafao_id=1&local_id=1&quantidade=10",
                       follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]
