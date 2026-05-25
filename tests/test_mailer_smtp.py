"""Testes do backend SMTP do mailer — monkeypatch smtplib.

Sem rede: substituímos `smtplib.SMTP` / `smtplib.SMTP_SSL` por classes
fake que capturam as chamadas e nos deixam inspecionar a mensagem.
"""

from __future__ import annotations

import pytest

from app.auth.mailer import send_email


class _FakeSMTP:
    """Captura chamadas em vez de conectar de verdade."""

    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_called_with = None
        self.sent_messages = []
        self.closed = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_called_with = (username, password)

    def send_message(self, msg):
        self.sent_messages.append(msg)


class _FakeSMTPSSL(_FakeSMTP):
    """Subclasse mantém appends em _FakeSMTP.instances também (herança)
    + sua própria lista pra filtragem fácil nos tests."""
    instances: list = []

    def __init__(self, host, port, timeout=None):
        super().__init__(host, port, timeout=timeout)
        _FakeSMTPSSL.instances.append(self)


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.instances.clear()
    _FakeSMTPSSL.instances.clear()
    yield


@pytest.fixture
def smtp_env(app, monkeypatch):
    """Configura backend smtp + monkeypatch smtplib.SMTP/SMTP_SSL."""
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)

    # Salva config original pra restaurar (fixture session-scoped)
    original = {
        k: app.config.get(k) for k in [
            "MAIL_BACKEND", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
            "SMTP_PASSWORD", "SMTP_USE_TLS", "SMTP_USE_SSL", "SMTP_FROM_ADDR",
            "SMTP_FROM_NAME", "SMTP_TIMEOUT",
        ]
    }
    app.config.update(
        MAIL_BACKEND="smtp",
        SMTP_HOST="smtp.example.com",
        SMTP_PORT=587,
        SMTP_USERNAME=None,
        SMTP_PASSWORD=None,
        SMTP_USE_TLS=True,
        SMTP_USE_SSL=False,
        SMTP_FROM_ADDR="no-reply@aquag20.com",
        SMTP_FROM_NAME="AquaG20",
        SMTP_TIMEOUT=30,
    )
    yield
    app.config.update({k: v for k, v in original.items() if v is not None})


# ---------------------------------------------------------------------------


class TestSendSmtpBasico:
    def test_envia_via_smtp_padrao_starttls(self, app, smtp_env):
        with app.app_context():
            send_email(to="cliente@example.com",
                       subject="Teste", body="corpo simples")
        assert len(_FakeSMTP.instances) == 1
        smtp = _FakeSMTP.instances[0]
        assert smtp.host == "smtp.example.com"
        assert smtp.port == 587
        assert smtp.timeout == 30
        assert smtp.starttls_called is True
        assert smtp.login_called_with is None  # sem credenciais
        assert smtp.closed is True
        assert len(smtp.sent_messages) == 1
        msg = smtp.sent_messages[0]
        assert msg["Subject"] == "Teste"
        assert msg["To"] == "cliente@example.com"
        assert "AquaG20" in msg["From"]
        assert "no-reply@aquag20.com" in msg["From"]
        assert msg.get_content().strip() == "corpo simples"

    def test_login_quando_credenciais_setadas(self, app, smtp_env):
        app.config["SMTP_USERNAME"] = "user"
        app.config["SMTP_PASSWORD"] = "pass"
        with app.app_context():
            send_email(to="x@y.com", subject="s", body="b")
        smtp = _FakeSMTP.instances[0]
        assert smtp.login_called_with == ("user", "pass")

    def test_from_sem_nome_usa_so_endereco(self, app, smtp_env):
        app.config["SMTP_FROM_NAME"] = ""
        with app.app_context():
            send_email(to="x@y.com", subject="s", body="b")
        msg = _FakeSMTP.instances[0].sent_messages[0]
        # Com nome vazio, default "AquaG20" é aplicado no mailer
        assert "AquaG20" in msg["From"] or msg["From"] == "no-reply@aquag20.com"


class TestSendSmtpSSL:
    def test_usa_smtp_ssl_quando_use_ssl(self, app, smtp_env):
        app.config["SMTP_USE_SSL"] = True
        app.config["SMTP_PORT"] = 465
        with app.app_context():
            send_email(to="x@y.com", subject="s", body="b")
        # Deve ter instanciado a classe SSL (e nada da base "pura").
        ssl_instances = _FakeSMTPSSL.instances
        plain_instances = [i for i in _FakeSMTP.instances
                            if not isinstance(i, _FakeSMTPSSL)]
        assert len(ssl_instances) == 1
        assert len(plain_instances) == 0
        ssl_smtp = ssl_instances[0]
        # NÃO faz starttls em cima de SSL (duas camadas quebra)
        assert ssl_smtp.starttls_called is False
        assert ssl_smtp.port == 465

    def test_ssl_e_tls_ambos_ignora_starttls(self, app, smtp_env):
        """Mesmo com USE_TLS=True, se USE_SSL=True não faz STARTTLS."""
        app.config["SMTP_USE_SSL"] = True
        app.config["SMTP_USE_TLS"] = True
        with app.app_context():
            send_email(to="x@y.com", subject="s", body="b")
        assert _FakeSMTPSSL.instances[0].starttls_called is False


class TestSendSmtpErros:
    def test_sem_host_levanta(self, app, smtp_env):
        app.config["SMTP_HOST"] = None
        with app.app_context():
            with pytest.raises(RuntimeError, match="SMTP_HOST"):
                send_email(to="x@y.com", subject="s", body="b")

    def test_sem_from_addr_levanta(self, app, smtp_env):
        app.config["SMTP_FROM_ADDR"] = None
        with app.app_context():
            with pytest.raises(RuntimeError, match="SMTP_FROM_ADDR"):
                send_email(to="x@y.com", subject="s", body="b")

    def test_send_message_falha_propaga(self, app, smtp_env, monkeypatch):
        """Se smtplib lança no send_message, o caller recebe a exception."""
        def _explode(self, msg):
            raise smtplib.SMTPException("relay denied")

        import smtplib
        monkeypatch.setattr(_FakeSMTP, "send_message", _explode)
        with app.app_context():
            with pytest.raises(smtplib.SMTPException, match="relay denied"):
                send_email(to="x@y.com", subject="s", body="b")


class TestBackendDesconhecido:
    def test_levanta_explicito(self, app):
        original = app.config.get("MAIL_BACKEND")
        app.config["MAIL_BACKEND"] = "sendgrid-api"
        try:
            with app.app_context():
                with pytest.raises(NotImplementedError, match="sendgrid-api"):
                    send_email(to="x@y.com", subject="s", body="b")
        finally:
            app.config["MAIL_BACKEND"] = original
