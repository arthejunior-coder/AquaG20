"""Wrapper Argon2 para hashing de senhas.

Argon2id é o padrão recomendado pela OWASP. Parâmetros vêm da config
da app (ARGON2_TIME_COST, ARGON2_MEMORY_COST, ARGON2_PARALLELISM) para
permitir ajustes por ambiente sem mudar código.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from flask import current_app


def _hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=current_app.config["ARGON2_TIME_COST"],
        memory_cost=current_app.config["ARGON2_MEMORY_COST"],
        parallelism=current_app.config["ARGON2_PARALLELISM"],
    )


def hash_password(senha: str) -> str:
    return _hasher().hash(senha)


def verify_password(senha_hash: str, senha: str) -> bool:
    try:
        _hasher().verify(senha_hash, senha)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(senha_hash: str) -> bool:
    return _hasher().check_needs_rehash(senha_hash)
