"""Forms dos 5 tipos de movimento manual do pool.

Cada form corresponde a 1 método do `PoolService`. As `choices` dos
SelectField são preenchidas em runtime na view (dependem de dados do
tenant), portanto declaradas vazias aqui.
"""

from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.models.pool import EstadoGarrafao


def _estado_choices(incluir_avariado: bool = True):
    out = [(EstadoGarrafao.cheio.value, "Cheio"), (EstadoGarrafao.vazio.value, "Vazio")]
    if incluir_avariado:
        out.append((EstadoGarrafao.avariado.value, "Avariado"))
    return out


class _MovimentoBase(FlaskForm):
    """Campos comuns aos 5 forms — herdados para evitar repetição."""

    tipo_garrafao_id = SelectField("Tipo de garrafão", coerce=int,
                                    validators=[DataRequired()], choices=[])
    quantidade = IntegerField("Quantidade", validators=[DataRequired(), NumberRange(min=1)])
    validade = DateField("Validade do lote", validators=[DataRequired()],
                          description="Data estampada no fundo dos garrafões afetados.")
    observacao = StringField("Observação", validators=[Optional(), Length(max=255)])


class CompraForm(_MovimentoBase):
    local_destino_id = SelectField("Local destino", coerce=int,
                                    validators=[DataRequired()], choices=[])
    estado = SelectField("Estado de chegada", validators=[DataRequired()],
                          choices=_estado_choices(incluir_avariado=False))
    submit = SubmitField("Registrar compra")


class DescarteForm(_MovimentoBase):
    local_origem_id = SelectField("Local origem", coerce=int,
                                   validators=[DataRequired()], choices=[])
    estado = SelectField("Estado do garrafão", validators=[DataRequired()],
                          choices=_estado_choices())
    submit = SubmitField("Registrar descarte")


class TransferenciaForm(_MovimentoBase):
    local_origem_id = SelectField("Origem", coerce=int,
                                   validators=[DataRequired()], choices=[])
    local_destino_id = SelectField("Destino", coerce=int,
                                    validators=[DataRequired()], choices=[])
    estado = SelectField("Estado", validators=[DataRequired()],
                          choices=_estado_choices())
    submit = SubmitField("Registrar transferência")


class AvariaForm(_MovimentoBase):
    local_id = SelectField("Local", coerce=int,
                            validators=[DataRequired()], choices=[])
    estado_origem = SelectField("Estado antes da avaria", validators=[DataRequired()],
                                 choices=_estado_choices(incluir_avariado=False))
    submit = SubmitField("Registrar avaria")


class EnvaseForm(_MovimentoBase):
    """Industrialização — local DEVE ser do tipo 'industria'."""

    local_industria_id = SelectField(
        "Local indústria",
        coerce=int,
        validators=[DataRequired()],
        choices=[],
        description="Apenas locais cadastrados como 'industria' aparecem aqui.",
    )
    submit = SubmitField("Registrar envase")


class AjusteForm(_MovimentoBase):
    local_id = SelectField("Local", coerce=int,
                            validators=[DataRequired()], choices=[])
    estado = SelectField("Estado", validators=[DataRequired()],
                          choices=_estado_choices())
    sinal = SelectField(
        "Direção do ajuste",
        choices=[("1", "Adicionar (+)"), ("-1", "Remover (−)")],
        validators=[DataRequired()],
        coerce=int,
    )
    # Observação OBRIGATÓRIA em ajuste — justificativa
    observacao = StringField(
        "Justificativa",
        validators=[DataRequired(), Length(min=3, max=255)],
        description="Por que o saldo está sendo corrigido manualmente?",
    )
    submit = SubmitField("Registrar ajuste")
