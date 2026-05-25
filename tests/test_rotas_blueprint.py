"""Testes do blueprint /rotas + integração com entrega via parada_id."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.frota import Entregador, TipoVeiculo, Veiculo
from app.models.logistica import Rota, RotaParada, StatusParada, StatusRota
from app.models.pedidos import Pedido, Permuta, PoliticaPermuta, StatusPedido
from app.models.pool import (
    EstadoGarrafao,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
)
from app.services.pedido_service import ItemPedidoInput, PedidoService
from app.services.pool_service import PoolService
from app.services.rota_service import RotaService


HOJE = date.today()
VAL_FUTURA = (HOJE.replace(day=1) + (HOJE - HOJE).__class__(days=45))


@pytest.fixture
def bp_setup(app, two_tenants):
    """Universo COMPLETO para fluxo rota → entrega.

    A: cliente + tipo + cd + veículo (LocalEstoque) + Veículo de cadastro +
       entregador + 50 cheios no LocalEstoque-veículo + pedido aberto.
    B: cliente + pedido (marcador para isolamento).
    """
    with app.app_context():
        tid = two_tenants["a"]["tenant_id"]
        cli = Cliente(tenant_id=tid, nome="Cliente Rotas", tipo=TipoCliente.varejo)
        tipo = TipoGarrafao(
            tenant_id=tid, nome="20L PC", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
        )
        veiculo_local = LocalEstoque(
            tenant_id=tid, tipo=TipoLocal.veiculo, nome="Caminhão Alpha",
        )
        cd = LocalEstoque(tenant_id=tid, tipo=TipoLocal.cd, nome="Depósito")
        veiculo_cad = Veiculo(
            tenant_id=tid, tipo=TipoVeiculo.caminhao, placa="AAA0001",
            capacidade_garrafoes=200,
        )
        ent = Entregador(tenant_id=tid, nome="Maria Motorista")
        db.session.add_all([cli, tipo, veiculo_local, cd, veiculo_cad, ent])
        db.session.flush()
        PoolService(db.session, tid).registrar_ajuste(
            tipo_garrafao_id=tipo.id, quantidade=50, local_id=veiculo_local.id,
            estado=EstadoGarrafao.cheio, validade=VAL_FUTURA, sinal=+1,
            observacao="seed",
        )
        ped = PedidoService(db.session, tid).criar_pedido(
            cliente_id=cli.id,
            politica_permuta=PoliticaPermuta.casar,
            itens=[ItemPedidoInput(tipo.id, 10, VAL_FUTURA, Decimal("15.00"))],
        )

        # Tenant B: pedido com marcador
        tid_b = two_tenants["b"]["tenant_id"]
        cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_cli", tipo=TipoCliente.varejo)
        tipo_b = TipoGarrafao(
            tenant_id=tid_b, nome="ZZZ_B_tipo", material=MaterialGarrafao.PC,
            capacidade_litros=Decimal("20.00"),
        )
        db.session.add_all([cli_b, tipo_b])
        db.session.flush()
        ped_b = PedidoService(db.session, tid_b).criar_pedido(
            cliente_id=cli_b.id,
            itens=[ItemPedidoInput(tipo_b.id, 1, VAL_FUTURA, Decimal("10.00"))],
        )

        db.session.commit()
        return {
            "tid": tid, "tid_b": tid_b,
            "cli": cli.id, "tipo": tipo.id,
            "veh_local": veiculo_local.id, "cd": cd.id,
            "veh_cad": veiculo_cad.id, "ent": ent.id,
            "ped": ped.id, "ped_b": ped_b.id,
        }


# ---------------------------------------------------------------------------


class TestAuth:
    def test_anon_redireciona(self, client, bp_setup):
        r = client.get("/rotas/", follow_redirects=False)
        assert r.status_code == 302


class TestListagem:
    def test_lista_vazia(self, client, bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/rotas/")
        assert r.status_code == 200
        assert b"Nenhuma rota" in r.data

    def test_lista_isolada_por_tenant(self, client, app, bp_setup, login_as):
        """B cria uma rota; A não vê."""
        with app.app_context():
            RotaService(db.session, bp_setup["tid_b"]).criar_rota(data_rota=HOJE)
            db.session.commit()
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/rotas/")
        assert r.status_code == 200
        assert b"Nenhuma rota" in r.data


class TestCriacao:
    def test_cria_via_form(self, client, app, bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        r = client.post("/rotas/nova", data={
            "data_rota": HOJE.isoformat(),
            "veiculo_id": str(ctx["veh_cad"]),
            "entregador_id": str(ctx["ent"]),
            "submit": "Salvar",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            rota = db.session.scalar(db.select(Rota))
            assert rota is not None
            assert rota.tenant_id == ctx["tid"]
            assert rota.veiculo_id == ctx["veh_cad"]
            assert rota.entregador_id == ctx["ent"]


class TestAdicionarParada:
    def test_adiciona_e_promove_pedido(self, client, app, bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            rota = RotaService(db.session, ctx["tid"]).criar_rota(data_rota=HOJE)
            db.session.commit()
            rid = rota.id

        r = client.post(f"/rotas/{rid}/paradas", data={
            "pedido_id": str(ctx["ped"]),
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            paradas = db.session.scalars(
                db.select(RotaParada).where(RotaParada.rota_id == rid)
            ).all()
            assert len(paradas) == 1
            assert paradas[0].pedido_id == ctx["ped"]
            ped = db.session.get(Pedido, ctx["ped"])
            assert ped.status == StatusPedido.roteirizado

    def test_pedido_de_outro_tenant_404(self, client, app, bp_setup, login_as):
        """Não dá nem pra tentar adicionar pedido de B na rota de A."""
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            rota = RotaService(db.session, ctx["tid"]).criar_rota(data_rota=HOJE)
            db.session.commit()
            rid = rota.id
        r = client.post(f"/rotas/{rid}/paradas", data={
            "pedido_id": str(ctx["ped_b"]),
        }, follow_redirects=True)
        # 200 com flash de erro
        assert r.status_code == 200
        assert b"n" in r.data.lower()  # genérica "não existe"
        with app.app_context():
            count = db.session.scalar(db.select(db.func.count(RotaParada.id)))
            assert count == 0


class TestTransicoes:
    def test_iniciar_via_http(self, client, app, bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            svc = RotaService(db.session, ctx["tid"])
            rota = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(rota, pedido_id=ctx["ped"])
            db.session.commit()
            rid = rota.id
        r = client.post(f"/rotas/{rid}/iniciar", follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            rota = db.session.get(Rota, rid)
            assert rota.status == StatusRota.em_andamento
            ped = db.session.get(Pedido, ctx["ped"])
            assert ped.status == StatusPedido.em_entrega

    def test_concluir_via_http(self, client, app, bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            svc = RotaService(db.session, ctx["tid"])
            rota = svc.criar_rota(data_rota=HOJE)
            svc.adicionar_parada(rota, pedido_id=ctx["ped"])
            svc.iniciar(rota)
            db.session.commit()
            rid = rota.id
        r = client.post(f"/rotas/{rid}/concluir", follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(Rota, rid).status == StatusRota.concluida

    def test_cancelar_via_http(self, client, app, bp_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            rota = RotaService(db.session, ctx["tid"]).criar_rota(data_rota=HOJE)
            db.session.commit()
            rid = rota.id
        r = client.post(f"/rotas/{rid}/cancelar", follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(Rota, rid).status == StatusRota.cancelada


# ---------------------------------------------------------------------------


class TestEntregaComParada:
    """Integração: entrega via /pedidos/<id>/entregar?parada_id=N marca a
    parada como entregue + Permuta.parada_id setado."""

    def test_entrega_marca_parada_entregue_e_set_permuta_parada_id(
        self, client, app, bp_setup, login_as
    ):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            svc = RotaService(db.session, ctx["tid"])
            rota = svc.criar_rota(
                data_rota=HOJE, veiculo_id=ctx["veh_cad"], entregador_id=ctx["ent"],
            )
            parada = svc.adicionar_parada(rota, pedido_id=ctx["ped"])
            svc.iniciar(rota)
            db.session.commit()
            pid = parada.id
            rid = rota.id

        r = client.post(
            f"/pedidos/{ctx['ped']}/entregar?parada_id={pid}",
            data={
                "veiculo_local_id": str(ctx["veh_local"]),
                "desbalanco_garrafoes": "0",
                "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
                "linhas-0-quantidade": "10",
                "linhas-0-validade_entregue": VAL_FUTURA.isoformat(),
                "linhas-0-validade_recebida": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Redirect deve ir pra rota (não pra pedido) quando parada é informada
        assert f"/rotas/{rid}" in r.headers["Location"]

        with app.app_context():
            parada = db.session.get(RotaParada, pid)
            assert parada.status == StatusParada.entregue
            assert parada.entregue_em is not None
            assert parada.qtd_entregue == 10
            assert parada.qtd_recolhido == 10

            perm = db.session.scalar(db.select(Permuta))
            assert perm is not None
            assert perm.parada_id == pid

            ped = db.session.get(Pedido, ctx["ped"])
            assert ped.status == StatusPedido.entregue

    def test_entrega_sem_parada_continua_funcionando(
        self, client, app, bp_setup, login_as
    ):
        """Entrega tradicional (sem rota) ainda funciona — Permuta.parada_id=None."""
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            PedidoService(db.session, ctx["tid"]).transicionar(
                db.session.get(Pedido, ctx["ped"]),
                StatusPedido.roteirizado,
            )
            db.session.commit()

        r = client.post(f"/pedidos/{ctx['ped']}/entregar", data={
            "veiculo_local_id": str(ctx["veh_local"]),
            "desbalanco_garrafoes": "0",
            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
            "linhas-0-quantidade": "10",
            "linhas-0-validade_entregue": VAL_FUTURA.isoformat(),
            "linhas-0-validade_recebida": "",
        }, follow_redirects=False)
        assert r.status_code == 302
        # Sem parada_id → redirect pra pedido (não pra rota)
        assert f"/pedidos/{ctx['ped']}" in r.headers["Location"]
        with app.app_context():
            perm = db.session.scalar(db.select(Permuta))
            assert perm.parada_id is None

    def test_parada_id_invalido_ignora_silenciosamente(
        self, client, app, bp_setup, login_as
    ):
        """parada_id de outro tenant ou inexistente → entrega prossegue sem rota."""
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = bp_setup
        with app.app_context():
            PedidoService(db.session, ctx["tid"]).transicionar(
                db.session.get(Pedido, ctx["ped"]),
                StatusPedido.roteirizado,
            )
            db.session.commit()
        r = client.post(f"/pedidos/{ctx['ped']}/entregar?parada_id=99999",
                        data={
                            "veiculo_local_id": str(ctx["veh_local"]),
                            "linhas-0-tipo_garrafao_id": str(ctx["tipo"]),
                            "linhas-0-quantidade": "10",
                            "linhas-0-validade_entregue": VAL_FUTURA.isoformat(),
                        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            perm = db.session.scalar(db.select(Permuta))
            assert perm is not None
            assert perm.parada_id is None
