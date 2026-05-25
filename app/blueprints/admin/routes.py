"""Blueprint /admin — gestão de usuários DENTRO do tenant do admin logado.

Restrito a papel='admin'. Endpoints:
  GET  /admin/                         redireciona para usuarios
  GET  /admin/usuarios                 lista
  GET  /admin/usuarios/novo            form criar
  POST /admin/usuarios/novo            cria
  GET  /admin/usuarios/<id>            form editar
  POST /admin/usuarios/<id>            atualiza
  POST /admin/usuarios/<id>/toggle     ativa/desativa (soft delete)
  POST /admin/usuarios/<id>/enviar-reset  dispara link de reset por email

Garantias críticas:
  - Lista/edição/toggle SEMPRE filtra por tenant — admin de A NUNCA vê
    nem mexe em usuário de B (mesmo conhecendo o id).
  - Toggle não permite desativar o PRÓPRIO admin logado (ele se trancaria
    fora do tenant).
  - Email novo é validado UNIQUE GLOBAL — schema não permite duplicata
    entre tenants (limitação aceita do schema).
"""

from __future__ import annotations

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import select

from app.auth.decorators import papel_requerido, repo
from app.auth.mailer import send_email
from app.auth.password import hash_password
from app.auth.tokens import gerar_token_reset
from app.blueprints.admin import bp
from app.blueprints.admin.forms import EditarUsuarioForm, NovoUsuarioForm
from app.extensions import db
from app.models.tenant import PapelUsuario, Usuario
from app.repositories.base import BaseRepository


class UsuarioRepository(BaseRepository):
    model = Usuario


@bp.route("/")
@papel_requerido("admin")
def index():
    return redirect(url_for("admin.lista_usuarios"))


@bp.route("/usuarios")
@papel_requerido("admin")
def lista_usuarios():
    r = repo(UsuarioRepository)
    usuarios = r.all(r.select().order_by(Usuario.ativo.desc(), Usuario.nome))
    return render_template("admin/usuarios_lista.html", usuarios=usuarios)


@bp.route("/usuarios/novo", methods=["GET", "POST"])
@papel_requerido("admin")
def novo_usuario():
    form = NovoUsuarioForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        # UNIQUE global no schema — checa antes de tentar persistir
        # NO-TENANT-FILTER: email é único entre todos os tenants
        existing = db.session.scalar(
            select(Usuario).where(Usuario.email == email)  # NO-TENANT-FILTER
        )
        if existing:
            flash(
                f"Já existe um usuário com o email {email}. Lembre que "
                "emails são únicos em toda a plataforma.",
                "danger",
            )
        else:
            r = repo(UsuarioRepository)
            r.add(
                nome=form.nome.data.strip(),
                email=email,
                senha_hash=hash_password(form.senha.data),
                papel=PapelUsuario(form.papel.data),
                ativo=form.ativo.data,
            )
            db.session.commit()
            flash(f"Usuário {email} criado.", "success")
            return redirect(url_for("admin.lista_usuarios"))
    return render_template("admin/usuarios_form.html", form=form, usuario=None)


@bp.route("/usuarios/<int:id>", methods=["GET", "POST"])
@papel_requerido("admin")
def editar_usuario(id):
    r = repo(UsuarioRepository)
    user = r.get(id)
    if user is None:
        abort(404)

    form = EditarUsuarioForm()
    if request.method == "POST":
        if form.validate_on_submit():
            # Proteção: admin não pode rebaixar a si mesmo via este form
            # (poderia se trancar fora). Pra trocar papel próprio precisa de
            # outro admin.
            novo_papel = PapelUsuario(form.papel.data)
            if user.id == current_user.id and novo_papel != PapelUsuario.admin:
                flash(
                    "Você não pode rebaixar seu próprio papel — peça a outro admin.",
                    "danger",
                )
            else:
                user.nome = form.nome.data.strip()
                user.papel = novo_papel
                user.ativo = form.ativo.data
                db.session.commit()
                flash("Usuário atualizado.", "success")
                return redirect(url_for("admin.lista_usuarios"))
    else:
        form.nome.data = user.nome
        form.papel.data = user.papel.value
        form.ativo.data = bool(user.ativo)

    return render_template("admin/usuarios_form.html", form=form, usuario=user)


@bp.route("/usuarios/<int:id>/toggle", methods=["POST"])
@papel_requerido("admin")
def toggle_usuario(id):
    r = repo(UsuarioRepository)
    user = r.get(id)
    if user is None:
        abort(404)

    # Defesa: admin não trava a si mesmo
    if user.id == current_user.id and user.ativo:
        flash(
            "Você não pode desativar a si mesmo. Crie outro admin antes.",
            "danger",
        )
        return redirect(url_for("admin.lista_usuarios"))

    user.ativo = not user.ativo
    db.session.commit()
    flash(
        f"Usuário {'reativado' if user.ativo else 'desativado'}.",
        "info",
    )
    return redirect(url_for("admin.lista_usuarios"))


@bp.route("/usuarios/<int:id>/enviar-reset", methods=["POST"])
@papel_requerido("admin")
def enviar_reset(id):
    """Dispara link de reset de senha para o usuário (mesma mecânica de
    'esqueci a senha'). Útil quando o usuário esqueceu mas não consegue
    pedir sozinho."""
    r = repo(UsuarioRepository)
    user = r.get(id)
    if user is None:
        abort(404)
    if not user.ativo:
        flash(
            "Usuário inativo — reative antes de enviar link de redefinição.",
            "warning",
        )
        return redirect(url_for("admin.lista_usuarios"))

    token = gerar_token_reset(user.id)
    link = url_for("auth.redefinir_senha", token=token, _external=True)
    send_email(
        to=user.email,
        subject="AquaG20 — defina sua senha",
        body=(
            f"Olá {user.nome},\n\n"
            f"O administrador do seu tenant disparou um link para você "
            f"definir uma nova senha.\nO link abaixo é válido por 1 hora:\n\n"
            f"{link}\n\n"
            f"Se você não esperava este email, ignore.\n"
        ),
    )
    current_app.logger.info(
        "admin.enviar_reset: link enviado a usuario id=%d por admin id=%d",
        user.id, current_user.id,
    )
    flash(f"Link de redefinição enviado para {user.email}.", "success")
    return redirect(url_for("admin.lista_usuarios"))
