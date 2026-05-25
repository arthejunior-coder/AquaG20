"""Teste end-to-end do fluxo de negócio do MVP.

Cenário: admin loga, cadastra cliente, monta saldo no pool, cria pedido,
roteiriza, entrega (gera permuta + atualiza saldos), faz descarte, e
verifica que os 3 KPIs do dashboard refletem cada passo.

Faz APENAS via HTTP (cliente Flask de teste) — exercita o stack completo
desde URL routing até template renderizado. É a defesa contra regressão
quando algum passo do meio é alterado.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente
from app.models.pedidos import Pedido, StatusPedido
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoSaldo,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.pool_service import PoolService


HOJE = date.today()
VAL_FUTURA = (HOJE.replace(day=1) + timedelta(days=45))


@pytest.fixture
def e2e_universe(app, two_tenants):
    """Universo: tipo+veículo+CD prontos. Cliente é criado VIA HTTP no teste."""
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        tipo = TipoGarrafao(
            tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
            valor_reposicao=Decimal("35.00"),
        )
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito Demo")
        veh = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão Demo")
        db.session.add_all([tipo, cd, veh])
        db.session.flush()
        # Seed pool: 50 cheios validade futura no veículo (pronto para entregar)
        PoolService(db.session, tid).registrar_ajuste(
            tipo_garrafao_id=tipo.id, quantidade=50, local_id=veh.id,
            estado=EstadoGarrafao.cheio, validade=VAL_FUTURA, sinal=+1,
            observacao="seed e2e",
        )
        db.session.commit()
        return {
            "tid": tid, "tipo": tipo.id, "cd": cd.id, "veh": veh.id,
        }


class TestFluxoCompleto:
    def test_login_cadastro_pedido_entrega_descarte_kpi(
        self, app, client, e2e_universe, login_as
    ):
        ctx = e2e_universe

        # ---- 1. LOGIN ----
        login_as(client, "admin@a.com", "senha-A-123")

        # ---- 2. CADASTRA CLIENTE via HTTP ----
        r = client.post("/cadastros/clientes/novo", data={
            "nome": "Cliente E2E",
            "tipo": "atacado",
            "ativo": "y",
            "submit": "Salvar",
        }, follow_redirects=False)
        assert r.status_code == 302, f"esperava redirect, got {r.status_code}"

        with app.app_context():
            cli = db.session.scalar(
                db.select(Cliente).where(Cliente.nome == "Cliente E2E")
            )
            assert cli is not None
            assert cli.tenant_id == ctx["tid"]
            cli_id = cli.id

        # ---- 3. DASHBOARD INICIAL: KPI custo = 0 ----
        r = client.get("/")
        assert r.status_code == 200
        # Card de reposição mostra R$ 0.00 (sem descarte ainda)
        assert b"R$ 0.00" in r.data

        # ---- 4. CRIA PEDIDO via HTTP ----
        r = client.post("/pedidos/novo", data={
            "cliente_id": str(cli_id),
            "politica_permuta": "casar",
            "forma_pagamento": "dinheiro",
            "canal": "balcao",
            "observacao": "pedido e2e",
            "itens-0-tipo_garrafao_id": str(ctx["tipo"]),
            "itens-0-quantidade": "10",
            "itens-0-validade_solicitada": VAL_FUTURA.isoformat(),
            "itens-0-preco_unitario": "15.00",
            "submit": "Criar pedido",
        }, follow_redirects=False)
        assert r.status_code == 302
        location = r.headers["Location"]
        assert "/pedidos/" in location

        with app.app_context():
            ped = db.session.scalar(db.select(Pedido))
            assert ped is not None
            assert ped.tenant_id == ctx["tid"]
            assert ped.status == StatusPedido.aberto
            assert ped.qtd_total == 10
            assert ped.valor_total == Decimal("150.00")
            ped_id = ped.id

        # ---- 5. ROTEIRIZA via HTTP ----
        r = client.post(f"/pedidos/{ped_id}/status", data={"novo": "roteirizado"},
                        follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(Pedido, ped_id).status == StatusPedido.roteirizado

        # ---- 6. REGISTRA ENTREGA via HTTP ----
        r = client.post(f"/pedidos/{ped_id}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "desbalanco_garrafoes": "0",
            "observacao": "entregue e2e",
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "10",
            "linhas-0-validade_entregue": VAL_FUTURA.isoformat(),
            "linhas-0-validade_recebida": "",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            p = db.session.get(Pedido, ped_id)
            assert p.status == StatusPedido.entregue
            # Saldo: 40 cheios + 10 vazios no veículo
            s_cheio = db.session.scalar(db.select(GarrafaoSaldo).where(
                GarrafaoSaldo.local_id == ctx["veh"],
                GarrafaoSaldo.estado == EstadoGarrafao.cheio,
            ))
            assert s_cheio.quantidade == 40
            s_vazio = db.session.scalar(db.select(GarrafaoSaldo).where(
                GarrafaoSaldo.local_id == ctx["veh"],
                GarrafaoSaldo.estado == EstadoGarrafao.vazio,
            ))
            assert s_vazio.quantidade == 10

        # ---- 7. DASHBOARD: taxa de casamento 100% ----
        r = client.get("/")
        assert r.status_code == 200
        assert b"100.0%" in r.data
        assert b"10 / 10" in r.data  # permutas_casada/total

        # ---- 8. DESCARTE via HTTP (KPI custo deve subir) ----
        r = client.post("/pool/movimentos/novo/descarte", data={
            "tipo_garrafao_id": str(ctx["tipo"]),
            "quantidade": "2",
            "local_origem_id": str(ctx["veh"]),
            "estado": "vazio",
            "validade": VAL_FUTURA.isoformat(),
            "observacao": "vencido descartado",
            "submit": "Registrar",
        }, follow_redirects=False)
        assert r.status_code == 302

        # ---- 9. DASHBOARD: KPI custo = 2 * 35 = R$ 70.00 ----
        r = client.get("/")
        assert r.status_code == 200
        assert b"70.00" in r.data

        # ---- 10. AUDITORIA: reconstrução do saldo bate ----
        with app.app_context():
            divergencias = PoolService(db.session, ctx["tid"]).reconstruir_saldos(
                dry_run=True
            )
            assert divergencias == [], f"saldos divergem do livro-razão: {divergencias}"
