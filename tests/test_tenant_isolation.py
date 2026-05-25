"""Testes esqueletais de isolamento por tenant.

Este arquivo cresce a cada passo do roadmap. Quando uma nova rota de
LISTAGEM autenticada nasce, adicione-a em `ROTAS_LISTAGEM_GET`. Quando
uma nova entidade ganha cadastro, adicione-a em
`test_repository_isolation_*`. O objetivo é que a CI quebre se alguma
nova rota esquecer o filtro tenant_id.

Hoje, com só `/` (dashboard placeholder), os testes parametrizados são
quase tautológicos — mas a estrutura está pronta.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.password import hash_password
from app.extensions import db
from app.models.tenant import PapelUsuario, Usuario
from app.repositories.base import BaseRepository


# ---------------------------------------------------------------------------
# REPO de exemplo — Usuario é o único model de negócio com TenantMixin que
# existe nesta fase. A partir do passo 7 cada cadastro ganha seu próprio
# *Repository e os testes abaixo se expandem.
# ---------------------------------------------------------------------------


class UsuarioRepository(BaseRepository):
    model = Usuario


# ---------------------------------------------------------------------------
# Bloco A — fixtures funcionam
# ---------------------------------------------------------------------------


class TestFixtures:
    def test_two_tenants_have_distinct_ids(self, two_tenants):
        assert two_tenants["a"]["tenant_id"] != two_tenants["b"]["tenant_id"]
        assert two_tenants["a"]["email"] == "admin@a.com"
        assert two_tenants["b"]["email"] == "admin@b.com"

    def test_login_as_admin_a_succeeds(self, client, two_tenants, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")
        assert r.status_code == 200
        assert b"admin@a.com" in r.data

    def test_login_as_admin_b_succeeds(self, client, two_tenants, login_as):
        login_as(client, "admin@b.com", "senha-B-123")
        r = client.get("/")
        assert r.status_code == 200
        assert b"admin@b.com" in r.data

    def test_login_falha_com_senha_errada(self, client, two_tenants):
        r = client.post(
            "/auth/login",
            data={"email": "admin@a.com", "senha": "errada", "submit": "Entrar"},
            follow_redirects=False,
        )
        # Sem redirect — fica em /auth/login com 200 + flash
        assert r.status_code == 200
        assert "inválidos".encode() in r.data


# ---------------------------------------------------------------------------
# Bloco B — BaseRepository isola por tenant_id
# ---------------------------------------------------------------------------


class TestBaseRepository:
    def test_query_only_returns_own_tenant_records(self, app, two_tenants):
        with app.app_context():
            repo_a = UsuarioRepository(db.session, two_tenants["a"]["tenant_id"])
            users_a = repo_a.all()
            assert len(users_a) == 1
            assert users_a[0].email == "admin@a.com"

            repo_b = UsuarioRepository(db.session, two_tenants["b"]["tenant_id"])
            users_b = repo_b.all()
            assert len(users_b) == 1
            assert users_b[0].email == "admin@b.com"

    def test_count_respeita_tenant(self, app, two_tenants):
        with app.app_context():
            repo_a = UsuarioRepository(db.session, two_tenants["a"]["tenant_id"])
            assert repo_a.count() == 1

    def test_get_other_tenant_returns_none(self, app, two_tenants):
        """Buscar pelo ID de uma entidade de outro tenant retorna None.
        Importante: nunca devolver 403 — não confirmar existência."""
        with app.app_context():
            admin_b_id = two_tenants["b"]["admin_id"]
            repo_a = UsuarioRepository(db.session, two_tenants["a"]["tenant_id"])
            assert repo_a.get(admin_b_id) is None

    def test_add_forces_tenant_id_overriding_kwargs(self, app, two_tenants):
        """Mesmo se o caller passar tenant_id de outro tenant em kwargs,
        o repositório sobrescreve com o tenant amarrado."""
        with app.app_context():
            repo_a = UsuarioRepository(db.session, two_tenants["a"]["tenant_id"])
            obj = repo_a.add(
                nome="Tentativa Vazamento",
                email="hacker@test.com",
                senha_hash=hash_password("123"),
                papel=PapelUsuario.atendimento,
                tenant_id=two_tenants["b"]["tenant_id"],  # deve ser ignorado
            )
            db.session.flush()
            assert obj.tenant_id == two_tenants["a"]["tenant_id"]
            db.session.rollback()

    def test_delete_other_tenant_raises(self, app, two_tenants):
        with app.app_context():
            admin_b_id = two_tenants["b"]["admin_id"]
            admin_b = db.session.get(Usuario, admin_b_id)
            repo_a = UsuarioRepository(db.session, two_tenants["a"]["tenant_id"])
            with pytest.raises(PermissionError):
                repo_a.delete(admin_b)

    def test_subclass_without_model_raises(self, app, two_tenants):
        class SemModel(BaseRepository):
            pass

        with app.app_context():
            with pytest.raises(NotImplementedError):
                SemModel(db.session, two_tenants["a"]["tenant_id"])


# ---------------------------------------------------------------------------
# Bloco C — rotas HTTP não vazam dados de outros tenants
#
# Lista PARAMETRIZADA: adicione cada nova rota de listagem GET aqui.
# A asserção é simples: nenhuma resposta autenticada como admin de A pode
# conter o marcador 'ZZZ_B_only_' (que está em dados do tenant B).
# ---------------------------------------------------------------------------

ROTAS_LISTAGEM_GET: list[str] = [
    "/",
    "/cadastros/clientes",
    "/cadastros/fornecedores",
    "/cadastros/centros-custo",
    "/cadastros/tipos-garrafao",
    "/cadastros/locais",
    "/pool/saldos",
    "/pool/movimentos",
    # Adicionar a partir dos próximos passos:
    # "/pedidos",                    # passo 14
    # "/financeiro/lancamentos",     # passo 16
]


@pytest.mark.parametrize("rota", ROTAS_LISTAGEM_GET)
def test_rota_nao_vaza_marcador_de_outro_tenant(client, two_tenants, login_as, rota):
    login_as(client, "admin@a.com", "senha-A-123")
    r = client.get(rota)
    assert r.status_code == 200, f"GET {rota} retornou {r.status_code}"
    assert b"ZZZ_B_only_" not in r.data, (
        f"Vazamento detectado em {rota}: a resposta contém marcador do tenant B"
    )


@pytest.mark.parametrize("rota", ROTAS_LISTAGEM_GET)
def test_rota_exige_autenticacao(client, rota):
    """Sem login, qualquer rota autenticada deve redirecionar para /auth/login."""
    r = client.get(rota, follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]
