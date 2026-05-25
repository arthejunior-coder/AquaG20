from datetime import datetime
from urllib.parse import urlparse

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from app.auth.mailer import send_email
from app.auth.password import hash_password, verify_password
from app.auth.tokens import gerar_token_reset, verificar_token_reset
from app.blueprints.auth import bp
from app.blueprints.auth.forms import (
    EsqueciSenhaForm,
    LoginForm,
    RedefinirSenhaForm,
)
from app.extensions import db, limiter
from app.models.tenant import Usuario


def _is_safe_redirect(target: str | None) -> bool:
    """Bloqueia open redirects — só aceita caminhos relativos do mesmo host."""
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.netloc and not parsed.scheme


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        # Login não conhece tenant antes do lookup. Email é UNIQUE global.
        stmt = select(Usuario).where(Usuario.email == email)  # NO-TENANT-FILTER
        user = db.session.scalar(stmt)

        if user and user.ativo and verify_password(user.senha_hash, form.senha.data):
            login_user(user, remember=form.lembrar.data)
            user.ultimo_login = datetime.now()
            db.session.commit()

            next_url = request.args.get("next")
            if _is_safe_redirect(next_url):
                return redirect(next_url)
            return redirect(url_for("dashboard.index"))

        flash("Email ou senha inválidos.", "danger")

    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Você saiu da sessão.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Reset de senha
# ---------------------------------------------------------------------------


# Mensagem genérica: NÃO revelar se o email existe (defesa contra
# enumeração). Usada tanto no caso "email não cadastrado" quanto "email
# encontrado e link enviado".
_MSG_GENERICA_ESQUECI = (
    "Se o email estiver cadastrado, enviaremos um link para redefinir "
    "a senha em alguns instantes."
)


@bp.route("/esqueci-senha", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def esqueci_senha():
    """Solicita link de reset. Mostra sempre a mesma mensagem — ataca
    enumeração de emails. Em dev, o link aparece no log do `flask run`."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = EsqueciSenhaForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        # Email é UNIQUE global no schema, então não há vazamento.
        user = db.session.scalar(
            select(Usuario).where(Usuario.email == email)  # NO-TENANT-FILTER
        )
        if user and user.ativo:
            token = gerar_token_reset(user.id)
            link = url_for("auth.redefinir_senha", token=token, _external=True)
            send_email(
                to=user.email,
                subject="AquaG20 — redefinição de senha",
                body=(
                    f"Olá {user.nome},\n\n"
                    f"Recebemos um pedido para redefinir sua senha.\n"
                    f"O link abaixo é válido por 1 hora:\n\n"
                    f"{link}\n\n"
                    f"Se não foi você, ignore este email.\n"
                ),
            )
        else:
            # Email não encontrado OU usuário inativo: loga internamente
            # mas resposta ao cliente é a MESMA mensagem.
            current_app.logger.info(
                "esqueci_senha: nenhum link enviado para %r (não cadastrado ou inativo)",
                email,
            )
        flash(_MSG_GENERICA_ESQUECI, "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/esqueci_senha.html", form=form)


@bp.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token):
    """Consome o token e redefine a senha. Token inválido/expirado →
    flash + redirect para o login. Sem distinção entre as causas."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    user_id = verificar_token_reset(token)
    if user_id is None:
        flash("Link inválido ou expirado. Solicite um novo.", "danger")
        return redirect(url_for("auth.esqueci_senha"))

    user = db.session.get(Usuario, user_id)
    if user is None or not user.ativo:
        flash("Link inválido ou expirado. Solicite um novo.", "danger")
        return redirect(url_for("auth.esqueci_senha"))

    form = RedefinirSenhaForm()
    if form.validate_on_submit():
        user.senha_hash = hash_password(form.senha.data)
        db.session.commit()
        current_app.logger.info(
            "redefinir_senha: senha redefinida para usuario id=%d", user.id
        )
        flash("Senha redefinida. Faça login com a nova senha.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/redefinir_senha.html", form=form, token=token)
