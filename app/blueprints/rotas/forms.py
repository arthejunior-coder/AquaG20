"""Forms WTF do blueprint rotas."""

from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, SubmitField
from wtforms.validators import DataRequired, Optional


def _none_or_int(v):
    if v in ("", None, 0, "0"):
        return None
    return int(v)


class RotaForm(FlaskForm):
    """Cabeçalho da rota: data + veículo + entregador (ambos opcionais)."""

    data_rota = DateField("Data da rota", validators=[DataRequired()])
    veiculo_id = SelectField(
        "Veículo",
        coerce=_none_or_int,
        validators=[Optional()],
    )
    entregador_id = SelectField(
        "Entregador",
        coerce=_none_or_int,
        validators=[Optional()],
    )
    submit = SubmitField("Salvar")
