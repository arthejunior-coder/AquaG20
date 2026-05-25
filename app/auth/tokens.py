"""Geração e verificação de tokens assinados (reset de senha, convite).

Usa `itsdangerous.URLSafeTimedSerializer` — tokens carregam um payload
serializável (JSON-safe) + expiração; assinatura via SECRET_KEY da app.

Por que esse design (e não armazenar token no banco):
  - Sem migration nova. Stateless.
  - SECRET_KEY rotacionada invalida todos os tokens vivos automaticamente.
  - Expiração validada no `loads(max_age=...)`.
  - Trade-off: não conseguimos *revogar* um token específico sem trocar
    a SECRET_KEY. Para reset de senha, o risco é baixo (válido só 1h).
"""

from __future__ import annotations

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


# Salts diferentes para cada propósito — evita confundir token de reset
# com token de convite ou de confirmação de email se forem adicionados.
_SALT_RESET_SENHA = "aquag20-reset-senha-v1"

# Validade default: 1 hora. Reset é uma ação de uma vez só, curta.
RESET_SENHA_MAX_AGE = 60 * 60  # segundos


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def gerar_token_reset(usuario_id: int) -> str:
    """Token assinado para o user_id, salt 'reset-senha'."""
    return _serializer().dumps(usuario_id, salt=_SALT_RESET_SENHA)


def verificar_token_reset(token: str, *, max_age: int | None = None) -> int | None:
    """Retorna usuario_id se token válido (assinatura + idade <= max_age);
    None caso contrário. Não distingue 'expirado' vs 'inválido' — caller
    decide se mostra mensagem genérica."""
    max_age = max_age if max_age is not None else RESET_SENHA_MAX_AGE
    try:
        return int(_serializer().loads(token, salt=_SALT_RESET_SENHA, max_age=max_age))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
