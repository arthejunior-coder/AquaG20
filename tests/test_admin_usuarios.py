"""Testes do blueprint /admin/usuarios."""

from __future__ import annotations

import pytest

from app.auth.password import hash_password, verify_password
from app.extensions import db
from app.models.tenant import PapelUsuario, Usuario


@pytest.fixture
def admin_setup(app, two_tenants):
    """Cria usuário 'atendimento' em A + outro 'gestor' em B (com marcador)."""
    with app.app_context():
        tid_a = two_tenants["a"]["tenant_id"]
        tid_b = two_tenants["b"]["tenant_id"]
        att_a = Usuario(
            tenant_id=tid_a, nome="Att A", email="att@a.com",
            senha_hash=hash_password("senha-att-123"),
            papel=PapelUsuario.atendimento,
        )
        ges_b = Usuario(
            tenant_id=tid_b, nome="ZZZ_B_only_gestor",
            email="gestor@b.com",
            senha_hash=hash_password("senha-ges-123"),
            papel=PapelUsuario.gestor,
        )
        db.session.add_all([att_a, ges_b])
        db.session.commit()
        return {
            "tid_a": tid_a, "tid_b": tid_b,
            "admin_a_id": two_tenants["a"]["admin_id"],
            "admin_b_id": two_tenants["b"]["admin_id"],
            "att_a_id": att_a.id,
            "ges_b_id": ges_b.id,
        }


# ---------------------------------------------------------------------------


class TestAuth:
    def test_anon_redireciona_login(self, client, admin_setup):
        r = client.get("/admin/usuarios", follow_redirects=False)
        assert r.status_code == 302

    def test_papel_atendimento_403(self, client, admin_setup, login_as):
        login_as(client, "att@a.com", "senha-att-123")
        r = client.get("/admin/usuarios")
        assert r.status_code == 403

    def test_papel_admin_acessa(self, client, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/admin/usuarios")
        assert r.status_code == 200
        assert b"Admin A" in r.data
        assert b"Att A" in r.data


# ---------------------------------------------------------------------------


class TestIsolamentoTenant:
    def test_lista_so_mostra_usuarios_do_tenant(self, client, admin_setup, login_as):
        """Admin de A vê A; nunca o gestor de B (marcado com ZZZ_B_only)."""
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/admin/usuarios")
        assert r.status_code == 200
        assert b"ZZZ_B_only" not in r.data
        assert b"gestor@b.com" not in r.data

    def test_404_em_usuario_de_outro_tenant(self, client, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get(f"/admin/usuarios/{admin_setup['ges_b_id']}")
        assert r.status_code == 404

    def test_toggle_usuario_de_outro_tenant_404(self, client, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(f"/admin/usuarios/{admin_setup['ges_b_id']}/toggle")
        assert r.status_code == 404


# ---------------------------------------------------------------------------


class TestCriar:
    def test_cria_usuario_padrao(self, client, app, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post("/admin/usuarios/novo", data={
            "nome": "Novo Atendente",
            "email": "novo@a.com",
            "papel": "atendimento",
            "senha": "senhaForte123",
            "confirmacao": "senhaForte123",
            "ativo": "y",
            "submit": "Criar usuário",
        }, follow_redirects=False)
        assert r.status_code == 302

        with app.app_context():
            u = db.session.scalar(
                db.select(Usuario).where(Usuario.email == "novo@a.com")
            )
            assert u is not None
            assert u.tenant_id == admin_setup["tid_a"]
            assert u.papel == PapelUsuario.atendimento
            assert verify_password(u.senha_hash, "senhaForte123") is True

    def test_email_duplicado_global_rejeita(self, client, app, admin_setup, login_as):
        """Email é UNIQUE global — não dá pra criar mesmo se for em outro tenant."""
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post("/admin/usuarios/novo", data={
            "nome": "Conflito",
            "email": "gestor@b.com",   # email do gestor de B
            "papel": "atendimento",
            "senha": "qualquer123",
            "confirmacao": "qualquer123",
            "ativo": "y",
            "submit": "Criar usuário",
        })
        assert r.status_code == 200
        assert b"j" in r.data and b"existe" in r.data
        with app.app_context():
            # Não criou usuário extra
            count = db.session.scalar(
                db.select(db.func.count(Usuario.id))
                .where(Usuario.email == "gestor@b.com")
            )
            assert count == 1

    def test_senha_curta_rejeita(self, client, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post("/admin/usuarios/novo", data={
            "nome": "X", "email": "x@a.com", "papel": "atendimento",
            "senha": "abc", "confirmacao": "abc", "ativo": "y",
            "submit": "Criar usuário",
        })
        assert r.status_code == 200
        assert b"8 caracteres" in r.data

    def test_confirmacao_diferente_rejeita(self, client, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post("/admin/usuarios/novo", data={
            "nome": "X", "email": "x@a.com", "papel": "atendimento",
            "senha": "senhaForte123", "confirmacao": "outraSenha456",
            "ativo": "y", "submit": "Criar usuário",
        })
        assert r.status_code == 200
        assert b"conferem" in r.data


# ---------------------------------------------------------------------------


class TestEditar:
    def test_edita_papel_e_nome(self, client, app, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        uid = admin_setup["att_a_id"]
        r = client.post(f"/admin/usuarios/{uid}", data={
            "nome": "Att A (renomeado)",
            "papel": "gestor",
            "ativo": "y",
            "submit": "Salvar",
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            u = db.session.get(Usuario, uid)
            assert u.nome == "Att A (renomeado)"
            assert u.papel == PapelUsuario.gestor

    def test_admin_nao_pode_rebaixar_a_si_mesmo(
        self, client, app, admin_setup, login_as
    ):
        login_as(client, "admin@a.com", "senha-A-123")
        admin_id = admin_setup["admin_a_id"]
        r = client.post(f"/admin/usuarios/{admin_id}", data={
            "nome": "Admin A",
            "papel": "atendimento",  # rebaixar
            "ativo": "y",
            "submit": "Salvar",
        }, follow_redirects=False)
        # Re-renderiza o form (não persiste)
        assert r.status_code == 200
        with app.app_context():
            u = db.session.get(Usuario, admin_id)
            assert u.papel == PapelUsuario.admin


# ---------------------------------------------------------------------------


class TestToggle:
    def test_toggle_desativa_e_reativa(self, client, app, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        uid = admin_setup["att_a_id"]
        client.post(f"/admin/usuarios/{uid}/toggle")
        with app.app_context():
            # ativo é TINYINT(1) — retorna 0/1, não bool puro
            assert bool(db.session.get(Usuario, uid).ativo) is False
        client.post(f"/admin/usuarios/{uid}/toggle")
        with app.app_context():
            assert bool(db.session.get(Usuario, uid).ativo) is True

    def test_admin_nao_pode_desativar_a_si_mesmo(
        self, client, app, admin_setup, login_as
    ):
        login_as(client, "admin@a.com", "senha-A-123")
        admin_id = admin_setup["admin_a_id"]
        r = client.post(f"/admin/usuarios/{admin_id}/toggle",
                        follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            u = db.session.get(Usuario, admin_id)
            assert bool(u.ativo) is True  # NÃO mudou


# ---------------------------------------------------------------------------


class TestEnviarReset:
    def test_dispara_link(self, client, app, admin_setup, login_as):
        import logging

        login_as(client, "admin@a.com", "senha-A-123")
        captured = []

        class _Cap(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        h = _Cap()
        h.setLevel(logging.INFO)
        app.logger.addHandler(h)
        original_level = app.logger.level
        app.logger.setLevel(logging.INFO)
        try:
            r = client.post(f"/admin/usuarios/{admin_setup['att_a_id']}/enviar-reset",
                            follow_redirects=False)
            assert r.status_code == 302
        finally:
            app.logger.removeHandler(h)
            app.logger.setLevel(original_level)

        joined = "\n".join(captured)
        assert "redefinir-senha" in joined
        assert "att@a.com" in joined

    def test_inativo_recusa(self, client, app, admin_setup, login_as):
        """Não envia reset para usuário inativo."""
        with app.app_context():
            u = db.session.get(Usuario, admin_setup["att_a_id"])
            u.ativo = False
            db.session.commit()
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(f"/admin/usuarios/{admin_setup['att_a_id']}/enviar-reset",
                        follow_redirects=True)
        assert r.status_code == 200
        assert b"inativo" in r.data.lower()

    def test_de_outro_tenant_404(self, client, admin_setup, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.post(
            f"/admin/usuarios/{admin_setup['ges_b_id']}/enviar-reset"
        )
        assert r.status_code == 404
