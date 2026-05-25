"""Testes do blueprint /pedidos.

Foco:
  - Listagem com isolamento por tenant (não vaza pedido de B).
  - Criação via POST com itens-N-* (formato HTMX dinâmico).
  - Detalhe + transições + cancelamento.
  - GET /pedidos/itens/nova-linha (parcial HTMX para crescer formulário).
  - Auth + papel (atendimento não cancela; anônimo é redirecionado).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.cadastros import Cliente, TipoCliente
from app.models.pedidos import Pedido, PedidoItem, PoliticaPermuta, StatusPedido
from app.models.pool import MaterialGarrafao, TipoGarrafao


VAL_2027 = date(2027, 6, 1)
VAL_2028 = date(2028, 6, 1)
VAL_2029 = date(2029, 6, 1)


@pytest.fixture
def blueprint_setup(app, two_tenants):
    """Cliente + 2 tipos no tenant A; cliente + 1 tipo no tenant B (com marcador)."""
    with app.app_context():
        tid_a = two_tenants["a"]["tenant_id"]
        tid_b = two_tenants["b"]["tenant_id"]

        cli_a = Cliente(tenant_id=tid_a, nome="Atacadista A", tipo=TipoCliente.atacado)
        t20_a = TipoGarrafao(tenant_id=tid_a, nome="20L PC A",
                              material=MaterialGarrafao.PC,
                              capacidade_litros=Decimal("20.00"))
        t10_a = TipoGarrafao(tenant_id=tid_a, nome="10L PET A",
                              material=MaterialGarrafao.PET,
                              capacidade_litros=Decimal("10.00"))

        cli_b = Cliente(tenant_id=tid_b, nome="ZZZ_B_only_cli", tipo=TipoCliente.varejo)
        t_b = TipoGarrafao(tenant_id=tid_b, nome="ZZZ_B_only_tipo",
                            material=MaterialGarrafao.PC,
                            capacidade_litros=Decimal("20.00"))
        db.session.add_all([cli_a, t20_a, t10_a, cli_b, t_b])
        db.session.commit()
        return {
            "tid_a": tid_a, "tid_b": tid_b,
            "cli_a": cli_a.id, "t20_a": t20_a.id, "t10_a": t10_a.id,
            "cli_b": cli_b.id, "t_b": t_b.id,
        }


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------


class TestAuth:
    def test_lista_exige_login(self, client, blueprint_setup):
        r = client.get("/pedidos/", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_novo_exige_login(self, client, blueprint_setup):
        r = client.get("/pedidos/novo", follow_redirects=False)
        assert r.status_code == 302

    def test_detalhe_inexistente_404(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/99999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------


class TestListagem:
    def test_lista_vazia_renderiza(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/")
        assert r.status_code == 200
        assert b"Nenhum pedido" in r.data

    def test_filtro_status_invalido_ignora(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/?status=foo")
        assert r.status_code == 200

    def test_hx_request_retorna_parcial(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/", headers={"HX-Request": "true"})
        assert r.status_code == 200
        # Parcial não inclui <html>/<head>
        assert b"<html" not in r.data


# ---------------------------------------------------------------------------
# Criação via POST com itens-N-*
# ---------------------------------------------------------------------------


class TestCriacao:
    def test_get_form_renderiza(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/novo")
        assert r.status_code == 200
        assert b"Novo pedido" in r.data
        # Cliente do tenant A deve aparecer; o de B não
        assert b"Atacadista A" in r.data
        assert b"ZZZ_B_only_cli" not in r.data

    def test_cria_pedido_atacado_3_linhas(self, client, blueprint_setup, login_as, app):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = blueprint_setup
        r = client.post("/pedidos/novo", data={
            "cliente_id": str(ctx["cli_a"]),
            "politica_permuta": "casar",
            "forma_pagamento": "prazo",
            "canal": "whatsapp",
            "observacao": "pedido atacado teste",
            "itens-0-tipo_garrafao_id": str(ctx["t20_a"]),
            "itens-0-quantidade": "10",
            "itens-0-validade_solicitada": "2027-06-01",
            "itens-0-preco_unitario": "15.00",
            "itens-1-tipo_garrafao_id": str(ctx["t20_a"]),
            "itens-1-quantidade": "15",
            "itens-1-validade_solicitada": "2028-06-01",
            "itens-1-preco_unitario": "15.00",
            "itens-2-tipo_garrafao_id": str(ctx["t20_a"]),
            "itens-2-quantidade": "35",
            "itens-2-validade_solicitada": "2029-06-01",
            "itens-2-preco_unitario": "15.00",
            "submit": "Criar pedido",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/pedidos/" in r.headers["Location"]

        # Confere persistência
        with app.app_context():
            pedidos = db.session.scalars(db.select(Pedido)).all()
            assert len(pedidos) == 1
            p = pedidos[0]
            assert p.tenant_id == ctx["tid_a"]
            assert p.cliente_id == ctx["cli_a"]
            assert p.qtd_total == 60
            assert p.valor_total == Decimal("900.00")
            itens = db.session.scalars(
                db.select(PedidoItem).where(PedidoItem.pedido_id == p.id)
                .order_by(PedidoItem.validade_solicitada)
            ).all()
            assert [i.validade_solicitada for i in itens] == [VAL_2027, VAL_2028, VAL_2029]
            assert [i.qtd_solicitada for i in itens] == [10, 15, 35]

    def test_cria_pedido_varejo_validade_vazia(self, client, blueprint_setup, login_as, app):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = blueprint_setup
        r = client.post("/pedidos/novo", data={
            "cliente_id": str(ctx["cli_a"]),
            "politica_permuta": "flexivel",
            "forma_pagamento": "",
            "canal": "",
            "observacao": "",
            "itens-0-tipo_garrafao_id": str(ctx["t10_a"]),
            "itens-0-quantidade": "2",
            "itens-0-validade_solicitada": "",
            "itens-0-preco_unitario": "18.00",
            "submit": "Criar pedido",
        }, follow_redirects=False)
        assert r.status_code == 302

        with app.app_context():
            p = db.session.scalar(db.select(Pedido))
            assert p.politica_permuta == PoliticaPermuta.flexivel
            assert p.forma_pagamento is None
            assert p.canal is None
            assert p.qtd_total == 2
            assert p.valor_total == Decimal("36.00")
            item = db.session.scalar(db.select(PedidoItem))
            assert item.validade_solicitada is None

    def test_cria_pedido_ignora_linha_vazia(self, client, blueprint_setup, login_as, app):
        """Linha onde o usuário não preencheu nada deve ser silenciosamente ignorada."""
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = blueprint_setup
        r = client.post("/pedidos/novo", data={
            "cliente_id": str(ctx["cli_a"]),
            "politica_permuta": "casar",
            "itens-0-tipo_garrafao_id": str(ctx["t20_a"]),
            "itens-0-quantidade": "5",
            "itens-0-validade_solicitada": "2029-06-01",
            "itens-0-preco_unitario": "10.00",
            # linha 1 totalmente vazia
            "itens-1-tipo_garrafao_id": "",
            "itens-1-quantidade": "",
            "itens-1-validade_solicitada": "",
            "itens-1-preco_unitario": "",
            "submit": "Criar pedido",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            p = db.session.scalar(db.select(Pedido))
            assert p.qtd_total == 5
            assert len(p.itens) == 1

    def test_cria_pedido_sem_itens_falha_e_re_renderiza(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = blueprint_setup
        r = client.post("/pedidos/novo", data={
            "cliente_id": str(ctx["cli_a"]),
            "politica_permuta": "casar",
            "submit": "Criar pedido",
        })
        assert r.status_code == 200
        assert b"pelo menos 1 item" in r.data

    def test_cria_pedido_data_invalida_falha(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        ctx = blueprint_setup
        r = client.post("/pedidos/novo", data={
            "cliente_id": str(ctx["cli_a"]),
            "politica_permuta": "casar",
            "itens-0-tipo_garrafao_id": str(ctx["t20_a"]),
            "itens-0-quantidade": "5",
            "itens-0-validade_solicitada": "31/12/2027",  # formato errado
            "itens-0-preco_unitario": "10.00",
            "submit": "Criar pedido",
        })
        assert r.status_code == 200
        assert b"data inv" in r.data or b"inv" in r.data


# ---------------------------------------------------------------------------
# Partial HTMX da nova linha
# ---------------------------------------------------------------------------


class TestNovaLinhaHTMX:
    def test_retorna_tr_com_idx(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/itens/nova-linha?idx=3")
        assert r.status_code == 200
        assert b'data-row-idx="3"' in r.data
        assert b'itens-3-tipo_garrafao_id' in r.data
        assert b'itens-3-quantidade' in r.data

    def test_idx_invalido_vira_zero(self, client, blueprint_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/itens/nova-linha?idx=abc")
        assert r.status_code == 200
        assert b'data-row-idx="0"' in r.data


# ---------------------------------------------------------------------------
# Detalhe + transições
# ---------------------------------------------------------------------------


def _criar_pedido_aberto(app, blueprint_setup) -> int:
    """Helper: cria 1 pedido aberto e retorna o id (fora do contexto HTTP)."""
    from app.services.pedido_service import ItemPedidoInput, PedidoService

    with app.app_context():
        ctx = blueprint_setup
        svc = PedidoService(db.session, ctx["tid_a"])
        p = svc.criar_pedido(
            cliente_id=ctx["cli_a"],
            itens=[ItemPedidoInput(ctx["t20_a"], 5, VAL_2029, Decimal("10.00"))],
        )
        db.session.commit()
        return p.id


class TestDetalheETransicoes:
    def test_detalhe_mostra_itens(self, client, blueprint_setup, login_as, app):
        pid = _criar_pedido_aberto(app, blueprint_setup)
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(f"/pedidos/{pid}")
        assert r.status_code == 200
        assert b"20L PC A" in r.data
        assert b"Atacadista A" in r.data
        assert b"Aberto" in r.data
        # botão de transição visível
        assert b"Roteirizar" in r.data

    def test_transicao_aberto_para_roteirizado(self, client, blueprint_setup, login_as, app):
        pid = _criar_pedido_aberto(app, blueprint_setup)
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(f"/pedidos/{pid}/status", data={"novo": "roteirizado"},
                        follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            p = db.session.get(Pedido, pid)
            assert p.status == StatusPedido.roteirizado

    def test_transicao_invalida_volta_flash(self, client, blueprint_setup, login_as, app):
        pid = _criar_pedido_aberto(app, blueprint_setup)
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(f"/pedidos/{pid}/status", data={"novo": "em_entrega"},
                        follow_redirects=True)
        assert r.status_code == 200
        # status deve continuar aberto
        with app.app_context():
            p = db.session.get(Pedido, pid)
            assert p.status == StatusPedido.aberto

    def test_cancelar(self, client, blueprint_setup, login_as, app):
        pid = _criar_pedido_aberto(app, blueprint_setup)
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(f"/pedidos/{pid}/cancelar", follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            p = db.session.get(Pedido, pid)
            assert p.status == StatusPedido.cancelado


# ---------------------------------------------------------------------------
# Isolamento por tenant
# ---------------------------------------------------------------------------


class TestIsolamentoTenant:
    def test_lista_de_a_nao_mostra_pedidos_de_b(
        self, client, blueprint_setup, login_as, app
    ):
        # B cria 1 pedido
        from app.services.pedido_service import ItemPedidoInput, PedidoService
        with app.app_context():
            ctx = blueprint_setup
            svc_b = PedidoService(db.session, ctx["tid_b"])
            svc_b.criar_pedido(
                cliente_id=ctx["cli_b"],
                itens=[ItemPedidoInput(ctx["t_b"], 1, VAL_2029, Decimal("10.00"))],
            )
            db.session.commit()

        # A loga e lista
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/")
        assert r.status_code == 200
        assert b"ZZZ_B_only" not in r.data

    def test_detalhe_de_pedido_de_b_retorna_404_para_a(
        self, client, blueprint_setup, login_as, app
    ):
        from app.services.pedido_service import ItemPedidoInput, PedidoService
        with app.app_context():
            ctx = blueprint_setup
            ped_b = PedidoService(db.session, ctx["tid_b"]).criar_pedido(
                cliente_id=ctx["cli_b"],
                itens=[ItemPedidoInput(ctx["t_b"], 1, VAL_2029, Decimal("10.00"))],
            )
            db.session.commit()
            pid_b = ped_b.id

        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(f"/pedidos/{pid_b}")
        assert r.status_code == 404  # não confirmar existência ao atacante

    def test_a_nao_pode_transicionar_pedido_de_b(
        self, client, blueprint_setup, login_as, app
    ):
        from app.services.pedido_service import ItemPedidoInput, PedidoService
        with app.app_context():
            ctx = blueprint_setup
            ped_b = PedidoService(db.session, ctx["tid_b"]).criar_pedido(
                cliente_id=ctx["cli_b"],
                itens=[ItemPedidoInput(ctx["t_b"], 1, VAL_2029, Decimal("10.00"))],
            )
            db.session.commit()
            pid_b = ped_b.id

        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(f"/pedidos/{pid_b}/status", data={"novo": "roteirizado"})
        assert r.status_code == 404
        with app.app_context():
            p = db.session.get(Pedido, pid_b)
            assert p.status == StatusPedido.aberto

    def test_cliente_de_b_nao_aparece_no_select_de_a(
        self, client, blueprint_setup, login_as
    ):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/pedidos/novo")
        assert r.status_code == 200
        assert b"ZZZ_B_only" not in r.data
