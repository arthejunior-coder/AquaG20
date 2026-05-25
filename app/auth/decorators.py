"""Decorators e helpers de autorização.

`papel_requerido` complementa `@login_required` exigindo um papel específico.
`repo()` instancia um Repository já amarrado ao tenant da sessão — é a porta
de entrada que materializa a regra de ouro do projeto (filtro tenant_id em
toda query).
"""

import functools

from flask import abort
from flask_login import current_user, login_required


def papel_requerido(*papeis: str):
    """Garante que o usuário logado tem um dos papéis listados.

    Implica `@login_required`. Compara contra `current_user.papel.value`.
    Exemplo: `@papel_requerido("admin", "gestor")`.
    """
    papeis_validos = set(papeis)

    def decorator(fn):
        @functools.wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user.papel.value not in papeis_validos:
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def repo(repo_cls):
    """Instancia um Repository já ligado ao tenant_id da sessão.

    Uso na view:
        clientes = repo(ClienteRepository).query().all()

    Será amplamente usado a partir do passo 6, quando o BaseRepository for
    introduzido. Fica disponível aqui desde já para o resto do código.
    """
    if not current_user.is_authenticated:
        abort(401)
    from app.extensions import db

    return repo_cls(db.session, current_user.tenant_id)
