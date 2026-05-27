"""Fixtures compartilhadas dos testes pytest.

Estratégia de banco: usa MySQL aquag20_test (não SQLite) porque o schema
do projeto depende de ENUMs nativos do MySQL e UNIQUEs com NULL que se
comportam diferente em SQLite. O DB deve ter sido criado uma vez via
scripts/setup_mysql.sql.

Cada teste roda numa tabela limpa (TRUNCATE com FK_CHECKS=0 antes).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app import create_app
from app.auth.password import hash_password
from app.extensions import db
from app.models.tenant import PapelUsuario, Tenant, Usuario


# Data fixa pra todos os testes — fixtures usam datas hardcoded tipo 2027-2029
# que precisam fazer sentido relativo a "hoje". Sem isso, mover a data real
# do sistema invalida testes ao longo do tempo.
# Escolhida 2026-05-01 porque acomoda VALs comuns dos testes:
# - 2026 (mês atual ou próximos) → janela aceita
# - 2027/2028 → dentro de 36 meses
# - 2029-05 → exatamente o limite
TEST_HOJE = date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _freeze_today_in_validity():
    """Congela `_HOJE_OVERRIDE` em app.validity pra TEST_HOJE.

    Aplicada em TODOS os tests automaticamente — sem isso, validar_validade_pedida
    usa date.today() que vai drift conforme passa o tempo e quebra testes que
    usam datas hardcoded como 2027/2028/2029.
    """
    import app.validity as v
    original = v._HOJE_OVERRIDE
    v._HOJE_OVERRIDE = TEST_HOJE
    yield
    v._HOJE_OVERRIDE = original


# Ordem de TRUNCATE — folhas primeiro, raízes (tenants/usuarios) por último.
# Em todo caso usamos FOREIGN_KEY_CHECKS=0, então a ordem importa só por
# defesa em profundidade.
_TABLES_TO_CLEAR = [
    "permutas",
    "rota_paradas",
    "rotas",
    "pedido_itens",
    "pedidos",
    "garrafao_movimentos",
    "garrafao_saldos",
    "lancamentos",
    "centros_custo",
    "tipos_garrafao",
    "locais_estoque",
    "veiculos",
    "entregadores",
    "fornecedores",
    "clientes",
    "usuarios",
    "tenants",
]


@pytest.fixture(scope="session")
def app():
    """App única para a sessão de testes, usando TestConfig (aquag20_test)."""
    app = create_app("test")
    return app


@pytest.fixture(autouse=True)
def _db_clean(app):
    """Limpa todas as tabelas antes de cada teste. autouse=True garante
    isolamento sem precisar incluir manualmente na assinatura."""
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for tbl in _TABLES_TO_CLEAR:
                conn.execute(text(f"TRUNCATE TABLE {tbl}"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    yield


@pytest.fixture
def client(app):
    """Test client do Flask. CSRF está desativado em TestConfig."""
    return app.test_client()


@pytest.fixture
def two_tenants(app):
    """Cria dois tenants (A e B) com um admin cada.

    Os identificadores do tenant B carregam o marcador 'ZZZ_B_only_' para
    facilitar testes de vazamento — qualquer resposta HTTP de uma rota
    autenticada como admin de A que contenha essa string indica vazamento.

    Retorna dicionário com IDs e credenciais (não os objetos ORM, para
    evitar DetachedInstanceError fora do contexto da app).
    """
    with app.app_context():
        tenant_a = Tenant(razao_social="Distribuidora A LTDA")
        db.session.add(tenant_a)
        db.session.flush()
        admin_a = Usuario(
            tenant_id=tenant_a.id,
            nome="Admin A",
            email="admin@a.com",
            senha_hash=hash_password("senha-A-123"),
            papel=PapelUsuario.admin,
        )
        db.session.add(admin_a)

        tenant_b = Tenant(razao_social="ZZZ_B_only_razao_social")
        db.session.add(tenant_b)
        db.session.flush()
        admin_b = Usuario(
            tenant_id=tenant_b.id,
            nome="ZZZ_B_only_admin_nome",
            email="admin@b.com",
            senha_hash=hash_password("senha-B-123"),
            papel=PapelUsuario.admin,
        )
        db.session.add(admin_b)

        db.session.commit()

        return {
            "a": {
                "tenant_id": tenant_a.id,
                "admin_id": admin_a.id,
                "email": "admin@a.com",
                "senha": "senha-A-123",
            },
            "b": {
                "tenant_id": tenant_b.id,
                "admin_id": admin_b.id,
                "email": "admin@b.com",
                "senha": "senha-B-123",
            },
        }


@pytest.fixture
def login_as(app):
    """Helper para logar via test client. Retorna função.

    Uso:
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/")

    Reseta o storage do rate-limiter ANTES do POST de login pra não
    ser barrado por contaminação acumulada (limit /auth/login: 10/min).
    """
    from app.extensions import limiter

    def _login(client, email: str, senha: str):
        with app.app_context():
            try:
                limiter.reset()
            except Exception:
                pass
            try:
                if limiter._storage is not None:
                    limiter._storage.reset()
            except Exception:
                pass

        # CSRF desativado em TestConfig — não precisa de token
        r = client.post(
            "/auth/login",
            data={"email": email, "senha": senha, "submit": "Entrar"},
            follow_redirects=False,
        )
        assert r.status_code == 302, (
            f"Login falhou para {email}: status={r.status_code} "
            f"body={r.data[:200]!r}"
        )
        return r

    return _login
