"""Forms WTF do blueprint financeiro."""

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import DateField, DecimalField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.models.financeiro import (
    FormaLancamento,
    NaturezaLancamento,
)


def _opcoes(py_enum, com_vazio: bool = False):
    opts = [(e.value, e.value.replace("_", " ").title()) for e in py_enum]
    if com_vazio:
        opts = [("", "—")] + opts
    return opts


class LancamentoForm(FlaskForm):
    """Form de criação/edição de lançamento.

    Os SelectFields de cliente/fornecedor/centro/pedido são populados no
    route (vêm como tuplas (id, label) já filtradas pelo tenant).
    """

    natureza = SelectField(
        "Natureza",
        choices=_opcoes(NaturezaLancamento),
        validators=[DataRequired()],
    )
    descricao = StringField(
        "Descrição", validators=[DataRequired(), Length(max=200)]
    )
    valor = DecimalField(
        "Valor (R$)",
        places=2,
        validators=[DataRequired(), NumberRange(min=Decimal("0.01"))],
    )
    vencimento = DateField("Vencimento", validators=[DataRequired()])
    centro_custo_id = SelectField(
        "Centro de custo",
        coerce=lambda v: int(v) if v else None,
        validators=[Optional()],
    )
    cliente_id = SelectField(
        "Cliente (receber)",
        coerce=lambda v: int(v) if v else None,
        validators=[Optional()],
    )
    fornecedor_id = SelectField(
        "Fornecedor (pagar)",
        coerce=lambda v: int(v) if v else None,
        validators=[Optional()],
    )
    pedido_id = StringField(
        "Pedido vinculado (opcional)",
        validators=[Optional(), Length(max=20)],
    )
    forma = SelectField(
        "Forma",
        choices=_opcoes(FormaLancamento, com_vazio=True),
        validators=[Optional()],
    )
    submit = SubmitField("Salvar")

    def parsed_pedido_id(self) -> int | None:
        if not self.pedido_id.data:
            return None
        try:
            return int(self.pedido_id.data)
        except ValueError:
            return None


class PagarForm(FlaskForm):
    """Form para marcar lançamento como pago. valor_pago default = lancamento.valor."""

    pago_em = DateField("Data do pagamento", validators=[DataRequired()])
    valor_pago = DecimalField(
        "Valor pago (R$)",
        places=2,
        validators=[DataRequired(), NumberRange(min=Decimal("0.01"))],
    )
    forma = SelectField(
        "Forma",
        choices=_opcoes(FormaLancamento, com_vazio=True),
        validators=[Optional()],
    )
    submit = SubmitField("Marcar pago")
