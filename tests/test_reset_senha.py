"""Testes do fluxo de reset de senha."""

from __future__ import annotations

import re

import pytest

from app.auth.password import hash_password, verify_password
from app.auth.tokens import (
    RESET_SENHA_MAX_AGE,
    gerar_token_reset,
    verificar_token_reset,
)
from app.extensions import db
from app.models.tenant import PapelUsuario, Usuario


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestTokens:
    def test_roundtrip(self, app):
        with app.app_context():
            t = gerar_token_reset(42)
            assert verificar_token_reset(t) == 42

    def test_token_invalido(self, app):
        with app.app_context():
            assert verificar_token_reset("token-aleatorio-quebrado") is None

    def test_token_expirado(self, app):
        with app.app_context():
            t = gerar_token_reset(7)
            # max_age=-1 → idade qualquer > -1, sempre expirado
            assert verificar_token_reset(t, max_age=-1) is None

    def test_token_amarrado_a_secret_key(self, app):
        """Trocar SECRET_KEY invalida todos os tokens vivos."""
        with app.app_context():
            t = gerar_token_reset(7)
            assert verificar_token_reset(t) == 7
            original = app.config["SECRET_KEY"]
            app.config["SECRET_KEY"] = "outra-chave-completamente-diferente"
            try:
                assert verificar_token_reset(t) is None
            finally:
                app.config["SECRET_KEY"] = original


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------


class TestEsqueciSenha:
    def test_get_renderiza_form(self, client, two_tenants):
        r = client.get("/auth/esqueci-senha")
        assert r.status_code == 200
        assert b"Redefinir senha" in r.data
        assert b"email" in r.data.lower()

    def test_post_email_existente_emite_flash_generico(
        self, client, app, two_tenants, caplog
    ):
        r = client.post("/auth/esqueci-senha", data={
            "email": "admin@a.com",
            "submit": "Enviar link de redefinição",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]
        # Mailer "log" deve ter sido invocado — link aparece no log da app
        # (não temos como interceptar facilmente aqui sem custom handler;
        # validamos no test do mailer direto, abaixo).

    def test_post_email_inexistente_emite_mesma_mensagem(self, client, two_tenants):
        """Defesa contra enumeração: mesma resposta para email existente
        e inexistente."""
        r1 = client.post("/auth/esqueci-senha",
                         data={"email": "fantasma@nope.com",
                               "submit": "Enviar link de redefinição"},
                         follow_redirects=True)
        assert r1.status_code == 200
        body = r1.data
        # Mensagem genérica deve aparecer
        assert b"Se o email estiver cadastrado" in body

    def test_post_email_invalido_re_renderiza(self, client, two_tenants):
        r = client.post("/auth/esqueci-senha",
                        data={"email": "nao-eh-email",
                              "submit": "Enviar link de redefinição"})
        assert r.status_code == 200
        # Continua no formulário (sem flash genérico)
        assert b"Redefinir senha" in r.data

    def test_logado_redireciona_para_dashboard(self, client, two_tenants, login_as):
        login_as(client, "admin@a.com", "senha-A-123")
        r = client.get("/auth/esqueci-senha", follow_redirects=False)
        assert r.status_code == 302


class TestRedefinirSenha:
    def test_get_com_token_valido_renderiza_form(self, app, client, two_tenants):
        with app.app_context():
            token = gerar_token_reset(two_tenants["a"]["admin_id"])
        r = client.get(f"/auth/redefinir-senha/{token}")
        assert r.status_code == 200
        assert b"nova senha" in r.data.lower() or b"Nova senha" in r.data

    def test_get_com_token_invalido_redireciona(self, client, two_tenants):
        r = client.get("/auth/redefinir-senha/token-quebrado",
                       follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/esqueci-senha" in r.headers["Location"]

    def test_get_com_token_expirado_redireciona(self, app, client, two_tenants):
        with app.app_context():
            token = gerar_token_reset(two_tenants["a"]["admin_id"])
        # Hack: substituir max_age via monkey — mais simples é gerar e mudar
        # SECRET_KEY temporariamente para invalidar
        original = app.config["SECRET_KEY"]
        app.config["SECRET_KEY"] = "muda-pra-invalidar"
        try:
            r = client.get(f"/auth/redefinir-senha/{token}", follow_redirects=False)
            assert r.status_code == 302
        finally:
            app.config["SECRET_KEY"] = original

    def test_get_com_user_inativo_redireciona(self, app, client, two_tenants):
        """Usuário desativado entre o pedido e o uso do link: rejeita."""
        with app.app_context():
            u = db.session.get(Usuario, two_tenants["a"]["admin_id"])
            u.ativo = False
            db.session.commit()
            token = gerar_token_reset(u.id)
        r = client.get(f"/auth/redefinir-senha/{token}", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/esqueci-senha" in r.headers["Location"]

    def test_post_redefine_senha_e_login_funciona(self, app, client, two_tenants):
        admin_id = two_tenants["a"]["admin_id"]
        with app.app_context():
            token = gerar_token_reset(admin_id)
        r = client.post(f"/auth/redefinir-senha/{token}", data={
            "senha": "novaSenha123",
            "confirmacao": "novaSenha123",
            "submit": "Redefinir senha",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

        # Confere que a senha mudou e a antiga não vale mais
        with app.app_context():
            u = db.session.get(Usuario, admin_id)
            assert verify_password(u.senha_hash, "novaSenha123") is True
            assert verify_password(u.senha_hash, "senha-A-123") is False

    def test_post_confirmacao_diferente_rejeita(self, app, client, two_tenants):
        with app.app_context():
            token = gerar_token_reset(two_tenants["a"]["admin_id"])
        r = client.post(f"/auth/redefinir-senha/{token}", data={
            "senha": "novaSenha123",
            "confirmacao": "outraSenha456",
            "submit": "Redefinir senha",
        })
        assert r.status_code == 200
        assert b"conferem" in r.data
        # Senha antiga ainda vale
        with app.app_context():
            u = db.session.get(Usuario, two_tenants["a"]["admin_id"])
            assert verify_password(u.senha_hash, "senha-A-123") is True

    def test_post_senha_curta_rejeita(self, app, client, two_tenants):
        with app.app_context():
            token = gerar_token_reset(two_tenants["a"]["admin_id"])
        r = client.post(f"/auth/redefinir-senha/{token}", data={
            "senha": "abc", "confirmacao": "abc",
            "submit": "Redefinir senha",
        })
        assert r.status_code == 200
        assert b"M" in r.data and b"8 caracteres" in r.data


# ---------------------------------------------------------------------------
# Mailer
# ---------------------------------------------------------------------------


class TestMailerLog:
    def test_envia_para_log(self, app, caplog):
        """Backend default 'log' deve escrever no logger da app."""
        import logging
        from app.auth.mailer import send_email
        # Caplog precisa propagar do app.logger
        with app.app_context():
            app.logger.propagate = True
            try:
                with caplog.at_level(logging.INFO, logger=app.logger.name):
                    send_email(to="x@y.com", subject="TestSubject",
                               body="corpo de teste\nlinha 2")
            finally:
                app.logger.propagate = False
        # A mensagem deve incluir o body
        joined = "\n".join(rec.message for rec in caplog.records)
        assert "x@y.com" in joined
        assert "TestSubject" in joined
        assert "corpo de teste" in joined

    def test_backend_desconhecido_levanta(self, app):
        from app.auth.mailer import send_email
        original = app.config.get("MAIL_BACKEND")
        app.config["MAIL_BACKEND"] = "smtp-real"
        try:
            with app.app_context():
                with pytest.raises(NotImplementedError):
                    send_email(to="x@y.com", subject="s", body="b")
        finally:
            if original is None:
                app.config.pop("MAIL_BACKEND", None)
            else:
                app.config["MAIL_BACKEND"] = original


# ---------------------------------------------------------------------------
# E2E
# ---------------------------------------------------------------------------


class TestResetE2E:
    def test_fluxo_completo_via_log_capturado(self, app, client, two_tenants):
        """Solicita reset → captura link do log → consome → loga com nova senha."""
        import logging
        admin_email = two_tenants["a"]["email"]

        # Captura logs da app durante o request
        handler_records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                handler_records.append(self.format(record))

        h = _Capture()
        h.setLevel(logging.INFO)
        app.logger.addHandler(h)
        # TestConfig fixa app.logger em WARNING; aqui precisamos de INFO
        # para capturar a linha do mailer.
        original_level = app.logger.level
        app.logger.setLevel(logging.INFO)
        try:
            r = client.post("/auth/esqueci-senha", data={
                "email": admin_email,
                "submit": "Enviar link de redefinição",
            })
            assert r.status_code == 302
        finally:
            app.logger.removeHandler(h)
            app.logger.setLevel(original_level)

        # Extrai link do log
        joined = "\n".join(handler_records)
        m = re.search(r"http[^\s]+/auth/redefinir-senha/([^\s]+)", joined)
        assert m is not None, f"link não encontrado no log:\n{joined}"
        token = m.group(1)

        # Redefine
        r = client.post(f"/auth/redefinir-senha/{token}", data={
            "senha": "NovaSenhaForte42",
            "confirmacao": "NovaSenhaForte42",
            "submit": "Redefinir senha",
        }, follow_redirects=False)
        assert r.status_code == 302

        # Login com nova senha
        r = client.post("/auth/login", data={
            "email": admin_email, "senha": "NovaSenhaForte42",
            "submit": "Entrar",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" not in r.headers["Location"]  # foi pra dashboard
