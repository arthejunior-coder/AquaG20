"""Forms do blueprint admin/usuarios."""

from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional

from app.models.tenant import PapelUsuario


def _papel_choices():
    """admin é incluído também — afinal, o admin pode promover outro."""
    return [(p.value, p.value.title()) for p in PapelUsuario]


class NovoUsuarioForm(FlaskForm):
    """Form de criação. Senha inicial pode ser:
      - definida pelo admin agora (digitada nos dois campos), ou
      - enviada por email (envia link de definição da primeira senha;
        usa o mesmo fluxo de 'reset' do auth — o usuário fica inativo
        até definir, ou ativo+sem senha utilizável — escolhemos a 2ª
        com hash randômico, pra simplificar).

    Para MVP: o admin DEFINE a senha aqui mesmo e passa por outro canal.
    Reset por email é uma opção via botão 'enviar link' no edit.
    """

    nome = StringField("Nome", validators=[DataRequired(), Length(max=120)])
    email = EmailField(
        "Email", validators=[DataRequired(), Email(), Length(max=160)]
    )
    papel = SelectField(
        "Papel", choices=_papel_choices(), validators=[DataRequired()],
    )
    senha = PasswordField(
        "Senha inicial",
        validators=[
            DataRequired(),
            Length(min=8, max=255, message="Mínimo 8 caracteres."),
        ],
        description="Comunique ao usuário por canal seguro. Ele pode redefinir depois.",
    )
    confirmacao = PasswordField(
        "Confirme a senha",
        validators=[
            DataRequired(),
            EqualTo("senha", message="As senhas não conferem."),
        ],
    )
    ativo = BooleanField("Ativo", default=True)
    submit = SubmitField("Criar usuário")


class EditarUsuarioForm(FlaskForm):
    """Edição — não permite trocar email (UNIQUE global; muda muito coisa).
    Senha NÃO é editada aqui — admin envia link de reset ou usuário faz
    'esqueci a senha'."""

    nome = StringField("Nome", validators=[DataRequired(), Length(max=120)])
    papel = SelectField(
        "Papel", choices=_papel_choices(), validators=[DataRequired()],
    )
    ativo = BooleanField("Ativo")
    submit = SubmitField("Salvar")
