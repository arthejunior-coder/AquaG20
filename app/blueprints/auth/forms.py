from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length


class LoginForm(FlaskForm):
    email = EmailField(
        "Email",
        validators=[DataRequired(), Email(), Length(max=160)],
    )
    senha = PasswordField(
        "Senha",
        validators=[DataRequired(), Length(min=1, max=255)],
    )
    lembrar = BooleanField("Lembrar de mim")
    submit = SubmitField("Entrar")


class EsqueciSenhaForm(FlaskForm):
    email = EmailField(
        "Email",
        validators=[DataRequired(), Email(), Length(max=160)],
    )
    submit = SubmitField("Enviar link de redefinição")


class RedefinirSenhaForm(FlaskForm):
    """Política de senha mínima: 8+ caracteres. Sem regras complexas
    (caracteres especiais, etc) — NIST 800-63B desencoraja regras
    rebuscadas; comprimento é o que importa. Usuário decide o resto.
    """

    senha = PasswordField(
        "Nova senha",
        validators=[
            DataRequired(),
            Length(min=8, max=255, message="Mínimo 8 caracteres."),
        ],
    )
    confirmacao = PasswordField(
        "Confirme a nova senha",
        validators=[
            DataRequired(),
            EqualTo("senha", message="As senhas não conferem."),
        ],
    )
    submit = SubmitField("Redefinir senha")
