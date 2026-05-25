"""Testes da tela de entrega: GET /pedidos/<id>/entregar, POST + permutas."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.pedidos import Pedido, PedidoItem, Permuta, PoliticaPermuta, StatusPedido
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoSaldo,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.pool_service import PoolService


VAL_2027 = date(2027, 6, 1)
VAL_2028 = date(2028, 6, 1)
VAL_2029 = date(2029, 6, 1)


@pytest.fixture
def entrega_ctx(app, two_tenants):
    """Cliente + tipo + veículo + 100 cheios 2027 no veículo + pedido roteirizado."""
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        cli = Cliente(tenant_id=tid, nome="Atacadista Z", tipo=TipoCliente.atacado)
        tipo = TipoGarrafao(tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
                             capacidade_litros=Decimal("20.00"))
        veh = LocalEstoque(tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão Alpha")
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito Central")
        db.session.add_all([cli, tipo, veh, cd])
        db.session.flush()
        PoolService(db.session, tid).registrar_ajuste(
            tipo_garrafao_id=tipo.id, quantidade=100, local_id=veh.id,
            estado=EstadoGarrafao.cheio, validade=VAL_2027, sinal=+1,
            observacao="seed",
        )
        ped = PedidoService(db.session, tid).criar_pedido(
            cliente_id=cli.id, politica_permuta=PoliticaPermuta.casar,
            itens=[ItemPedidoInput(tipo.id, 30, VAL_2027, Decimal("15.00"))],
        )
        PedidoService(db.session, tid).transicionar(ped, StatusPedido.roteirizado)
        db.session.commit()
        return {
            "tid": tid, "cli": cli.id, "tipo": tipo.id,
            "veh": veh.id, "cd": cd.id, "pedido_id": ped.id,
        }


class TestGETEntregar:
    def test_get_renderiza_form_com_linhas_pre(self, client, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(f"/pedidos/{entrega_ctx['pedido_id']}/entregar")
        assert r.status_code == 200
        assert b"Registrar entrega" in r.data
        assert b"Caminh" in r.data  # Caminhão Alpha
        assert b"linhas-0-quantidade" in r.data
        # Default qtd = qtd_solicitada (30)
        assert b'value="30"' in r.data

    def test_get_em_pedido_aberto_redireciona_com_flash(self, client, app, entrega_ctx, login_as):
        """Não dá pra entregar pedido em 'aberto' — botão nem aparece."""
        with app.app_context():
            ped = db.session.get(Pedido, entrega_ctx["pedido_id"])
            ped.status = StatusPedido.aberto
            db.session.commit()
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(f"/pedidos/{entrega_ctx['pedido_id']}/entregar", follow_redirects=False)
        assert r.status_code == 302
        assert "/pedidos/" in r.headers["Location"]

    def test_404_pedido_inexistente(self, client, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/99999/entregar")
        assert r.status_code == 404

    def test_anon_redireciona_login(self, client, entrega_ctx):
        r = client.get(f"/pedidos/{entrega_ctx['pedido_id']}/entregar", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


class TestPOSTEntregar:
    def test_entrega_casada_atualiza_tudo(self, client, app, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        r = client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "desbalanco_garrafoes": "0",
            "observacao": "entrega normal",
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "30",
            "linhas-0-validade_entregue": "2027-06-01",
            "linhas-0-validade_recebida": "",  # default = entregue
        }, follow_redirects=False)
        assert r.status_code == 302

        with app.app_context():
            ped = db.session.get(Pedido, ctx["pedido_id"])
            assert ped.status == StatusPedido.entregue
            # Saldo: 70 cheios + 30 vazios no veículo, validade 2027
            s_cheio = db.session.scalar(db.select(GarrafaoSaldo).where(
                GarrafaoSaldo.local_id == ctx["veh"],
                GarrafaoSaldo.estado == EstadoGarrafao.cheio,
            ))
            assert s_cheio.quantidade == 70
            s_vazio = db.session.scalar(db.select(GarrafaoSaldo).where(
                GarrafaoSaldo.local_id == ctx["veh"],
                GarrafaoSaldo.estado == EstadoGarrafao.vazio,
            ))
            assert s_vazio.quantidade == 30
            # 1 permuta casada
            perm = db.session.scalar(db.select(Permuta))
            assert perm is not None
            assert perm.quantidade == 30
            assert bool(perm.casado) is True
            assert bool(perm.concessao) is False
            # qtd_atendida atualizada
            item = db.session.scalar(db.select(PedidoItem))
            assert item.qtd_atendida == 30

    def test_entrega_descasada_em_politica_casar_grava_concessao(
        self, client, app, entrega_ctx, login_as
    ):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        r = client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "desbalanco_garrafoes": "0",
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "30",
            "linhas-0-validade_entregue": "2027-06-01",
            "linhas-0-validade_recebida": "2028-06-01",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            perm = db.session.scalar(db.select(Permuta))
            assert bool(perm.casado) is False
            assert bool(perm.concessao) is True

    def test_entrega_com_desbalanco_atualiza_cliente(
        self, client, app, entrega_ctx, login_as
    ):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "desbalanco_garrafoes": "3",
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "30",
            "linhas-0-validade_entregue": "2027-06-01",
        }, follow_redirects=False)
        with app.app_context():
            cli = db.session.get(Cliente, ctx["cli"])
            assert cli.saldo_garrafoes == 3

    def test_sem_veiculo_re_renderiza_com_erro(self, client, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        r = client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": "",
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "10",
            "linhas-0-validade_entregue": "2027-06-01",
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b"escolha um ve" in r.data

    def test_sem_quantidade_re_renderiza(self, client, app, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        r = client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "0",   # ignorada
            "linhas-0-validade_entregue": "2027-06-01",
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b"pelo menos 1 linha" in r.data
        with app.app_context():
            ped = db.session.get(Pedido, ctx["pedido_id"])
            assert ped.status == StatusPedido.roteirizado  # nada mudou

    def test_estoque_insuficiente_faz_rollback(self, client, app, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        r = client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "999",  # mais que 100 disponíveis
            "linhas-0-validade_entregue": "2027-06-01",
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            ped = db.session.get(Pedido, ctx["pedido_id"])
            assert ped.status == StatusPedido.roteirizado
            assert db.session.scalar(db.select(db.func.count(Permuta.id))) == 0
            # Saldo intacto
            s = db.session.scalar(db.select(GarrafaoSaldo).where(
                GarrafaoSaldo.local_id == ctx["veh"],
                GarrafaoSaldo.estado == EstadoGarrafao.cheio,
            ))
            assert s.quantidade == 100


class TestListaPermutas:
    def test_lista_vazia(self, client, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/permutas")
        assert r.status_code == 200
        assert b"Nenhuma permuta" in r.data

    def test_lista_mostra_permuta_apos_entrega(self, client, app, entrega_ctx, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = entrega_ctx
        client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "30",
            "linhas-0-validade_entregue": "2027-06-01",
        })
        r = client.get("/pedidos/permutas")
        assert r.status_code == 200
        assert b"Atacadista Z" in r.data
        assert b"20L PC" in r.data


class TestIsolamentoTenant:
    def test_b_nao_ve_permutas_de_a(self, client, app, entrega_ctx, two_tenants, login_as):
        """B registra permuta; A loga e na lista de A não aparece."""
        ctx = entrega_ctx
        # A registra entrega
        login_as(client, "admin@a.com", "senha-A-123")
        client.post(f"/pedidos/{ctx['pedido_id']}/entregar", data={
            "veiculo_local_id": str(ctx["veh"]),
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "30",
            "linhas-0-validade_entregue": "2027-06-01",
        })
        # Logout
        client.post("/auth/logout")

        # B loga (sem dados)
        login_as(client, "admin@b.com", "senha-B-123")
        r = client.get("/pedidos/permutas")
        assert r.status_code == 200
        assert b"Nenhuma permuta" in r.data

    def test_a_nao_pode_entregar_pedido_de_b(self, client, app, two_tenants, login_as):
        # Setup B
        with app.app_context():
            tid_b = two_tenants["b"]["tenant_id"]
            cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_cli", tipo=TipoCliente.varejo)
            tipo_b = TipoGarrafao(tenant_id=tid_b, nome="ZZZ_B_tipo",
                                   material=MaterialGarrafao.PC,
                                   capacidade_litros=Decimal("20.00"))
            veh_b = LocalEstoque(tenant_id=tid_b, tipo=TipoLocal.veiculo, nome="Veh B")
            db.session.add_all([cli_b, tipo_b, veh_b])
            db.session.flush()
            ped_b = PedidoService(db.session, tid_b).criar_pedido(
                cliente_id=cli_b.id,
                itens=[ItemPedidoInput(tipo_b.id, 1, VAL_2029, Decimal("10.00"))],
            )
            PedidoService(db.session, tid_b).transicionar(ped_b, StatusPedido.roteirizado)
            db.session.commit()
            pid_b = ped_b.id

        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(f"/pedidos/{pid_b}/entregar")
        assert r.status_code == 404
