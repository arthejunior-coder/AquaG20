"""Abstração mínima de envio de email.

Backends:
  - "log" (default em dev/test): dump no logger da app, visível no
    terminal `flask run`. Bom pra inspeção manual em dev.
  - "smtp" (prod): envia via SMTP usando `smtplib` stdlib (sem dep
    extra). Funciona com SES, SendGrid, Postmark, Mailgun, Gmail —
    qualquer provedor que exponha SMTP. Config necessária abaixo.

Configuração SMTP (env vars, todas lidas em `BaseConfig`):
    MAIL_BACKEND=smtp
    SMTP_HOST=email-smtp.us-east-1.amazonaws.com
    SMTP_PORT=587                        # 465 com SSL, 587 com STARTTLS
    SMTP_USERNAME=...
    SMTP_PASSWORD=...
    SMTP_USE_TLS=true                    # STARTTLS sobre porta 587
    SMTP_USE_SSL=false                   # SMTPS direto na 465
    SMTP_FROM_ADDR=no-reply@aquag20.com
    SMTP_FROM_NAME="AquaG20"             # opcional
    SMTP_TIMEOUT=30                      # segundos

Não é uma camada genérica de templates de email — é o suficiente
para o reset de senha do MVP. Quando virar mais coisa, refatorar.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from flask import current_app


def send_email(*, to: str, subject: str, body: str) -> None:
    """Envia email. Backend selecionado por `MAIL_BACKEND` (log|smtp).

    Não captura exception silenciosamente — falha de envio deve subir
    pro caller (que decide entre 500/retry/etc). Em produção, o handler
    500 da app loga o stack trace.
    """
    backend = current_app.config.get("MAIL_BACKEND", "log")
    if backend == "log":
        _send_log(to=to, subject=subject, body=body)
    elif backend == "smtp":
        _send_smtp(to=to, subject=subject, body=body)
    else:
        raise NotImplementedError(
            f"MAIL_BACKEND={backend!r} desconhecido. Use 'log' ou 'smtp'."
        )


# ---------------------------------------------------------------------------
# Backend: log (dev)
# ---------------------------------------------------------------------------


def _send_log(*, to: str, subject: str, body: str) -> None:
    """Backend de dev: dump no logger. Visível em `flask run`."""
    current_app.logger.info(
        "[MAIL/log] To: %s\n  Subject: %s\n  Body:\n%s",
        to, subject, _indent(body),
    )


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# Backend: smtp (prod)
# ---------------------------------------------------------------------------


def _send_smtp(*, to: str, subject: str, body: str) -> None:
    """Envia via SMTP. Lê tudo de current_app.config.

    Auto-seleciona SSL direto (porta 465) ou STARTTLS (porta 587) por
    config flag. Login só acontece se SMTP_USERNAME estiver setado —
    permite servidores internos que não exigem auth.
    """
    cfg = current_app.config

    host = cfg.get("SMTP_HOST")
    from_addr = cfg.get("SMTP_FROM_ADDR")
    if not host:
        raise RuntimeError("MAIL_BACKEND=smtp exige SMTP_HOST configurado")
    if not from_addr:
        raise RuntimeError("MAIL_BACKEND=smtp exige SMTP_FROM_ADDR configurado")

    port = int(cfg.get("SMTP_PORT") or 587)
    use_tls = bool(cfg.get("SMTP_USE_TLS", True))
    use_ssl = bool(cfg.get("SMTP_USE_SSL", False))
    timeout = int(cfg.get("SMTP_TIMEOUT") or 30)
    username = cfg.get("SMTP_USERNAME")
    password = cfg.get("SMTP_PASSWORD")
    from_name = cfg.get("SMTP_FROM_NAME") or "AquaG20"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = to
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg.set_content(body)

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=timeout) as smtp:
        # STARTTLS só quando SSL direto NÃO foi usado — duas camadas TLS quebra.
        if use_tls and not use_ssl:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)

    current_app.logger.info(
        "[MAIL/smtp] enviado para %s (subject=%r)", to, subject,
    )
