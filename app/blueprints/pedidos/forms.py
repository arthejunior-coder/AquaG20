"""Forms WTF do blueprint pedidos.

Para o cabeçalho do pedido usamos um form FlaskForm clássico. Os ITENS
chegam como N linhas dinâmicas no POST (`itens-N-tipo_garrafao_id`,
`itens-N-quantidade`, etc), adicionadas via HTMX no browser. Não usamos
WTForms FieldList porque a UX dinâmica fica menos engessada lendo o
request.form direto — e o PedidoService já valida tudo a fundo.
"""

from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, Optional

from app.models.pedidos import CanalPedido, FormaPagamento, PoliticaPermuta


def _opcoes(py_enum, com_vazio: bool = False, label_vazio: str = "—"):
    opts = [(e.value, e.value.replace("_", " ").title()) for e in py_enum]
    if com_vazio:
        opts = [("", label_vazio)] + opts
    return opts


class PedidoCabecalhoForm(FlaskForm):
    """Apenas o CABEÇALHO. Cliente vem como SelectField populado pelo route.

    Os itens NÃO estão aqui — chegam direto do request.form e são
    parseados em `parse_itens_input()`.
    """

    cliente_id = SelectField(
        "Cliente",
        coerce=int,
        validators=[DataRequired(message="Escolha um cliente.")],
    )
    politica_permuta = SelectField(
        "Política de permuta",
        choices=_opcoes(PoliticaPermuta),
        default=PoliticaPermuta.casar.value,
        validators=[DataRequired()],
    )
    forma_pagamento = SelectField(
        "Forma de pagamento",
        choices=_opcoes(FormaPagamento, com_vazio=True, label_vazio="—"),
        validators=[Optional()],
    )
    canal = SelectField(
        "Canal",
        choices=_opcoes(CanalPedido, com_vazio=True, label_vazio="—"),
        validators=[Optional()],
    )
    observacao = StringField("Observação", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Criar pedido")

    def to_header_kwargs(self) -> dict:
        return {
            "cliente_id": self.cliente_id.data,
            "politica_permuta": PoliticaPermuta(self.politica_permuta.data),
            "forma_pagamento": (
                FormaPagamento(self.forma_pagamento.data)
                if self.forma_pagamento.data else None
            ),
            "canal": CanalPedido(self.canal.data) if self.canal.data else None,
            "observacao": (self.observacao.data or "").strip() or None,
        }
