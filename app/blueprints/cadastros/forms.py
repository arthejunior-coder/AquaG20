"""Forms WTF dos cadastros base.

Convenção: o `tipo` de Cliente/CentroCusto é manipulado como string no
form e convertido para o `enum.Enum` no service/route — mantém o form
agnóstico do model. Validação de CPF/CNPJ é só por tamanho neste passo;
fica para hardening (passo 19) a regra completa.
"""

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DecimalField,
    EmailField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
)
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional, Regexp

from app.models.cadastros import TipoCentroCusto, TipoCliente
from app.models.pool import MaterialGarrafao, TipoLocal


def _opcoes(py_enum):
    """Lista de (valor, label) p/ SelectField, com label capitalizado."""
    return [(e.value, e.value.replace("_", " ").title()) for e in py_enum]


class ClienteForm(FlaskForm):
    # Identificação
    nome = StringField("Nome", validators=[DataRequired(), Length(max=160)])
    tipo = SelectField(
        "Tipo",
        choices=_opcoes(TipoCliente),
        validators=[DataRequired()],
    )
    nome_fantasia = StringField("Nome fantasia", validators=[Optional(), Length(max=160)])
    documento = StringField("CPF/CNPJ", validators=[Optional(), Length(max=18)])

    # Contato
    telefone = StringField("Telefone", validators=[Optional(), Length(max=20)])
    email = EmailField("Email", validators=[Optional(), Email(), Length(max=160)])

    # Endereço
    endereco = StringField("Endereço", validators=[Optional(), Length(max=200)])
    bairro = StringField("Bairro", validators=[Optional(), Length(max=100)])
    cidade = StringField("Cidade", validators=[Optional(), Length(max=100)])
    uf = StringField(
        "UF",
        validators=[Optional(), Length(min=2, max=2), Regexp(r"^[A-Za-z]{2}$", message="Use 2 letras")],
    )
    cep = StringField("CEP", validators=[Optional(), Length(max=9)])

    # Operação
    saldo_garrafoes = IntegerField(
        "Desbalanço de garrafões",
        validators=[Optional(), NumberRange(min=-9999, max=9999)],
        default=0,
        description="Pode ser negativo. Em operação normal tende a 0.",
    )
    ativo = BooleanField("Ativo", default=True)

    submit = SubmitField("Salvar")

    def to_model_kwargs(self) -> dict:
        """Devolve dict pronto para `repo.add(**kwargs)` ou `populate_obj`."""
        data = {f.name: f.data for f in self if f.name not in ("submit", "csrf_token")}
        # Normalizações
        data["tipo"] = TipoCliente(data["tipo"])
        if data.get("uf"):
            data["uf"] = data["uf"].upper()
        if data.get("email"):
            data["email"] = data["email"].lower()
        # Strings vazias → None (banco aceita NULL nos campos opcionais)
        for k in ("nome_fantasia", "documento", "telefone", "email",
                  "endereco", "bairro", "cidade", "uf", "cep"):
            if data.get(k) == "":
                data[k] = None
        if data.get("saldo_garrafoes") is None:
            data["saldo_garrafoes"] = 0
        return data


class FornecedorForm(FlaskForm):
    nome = StringField("Nome", validators=[DataRequired(), Length(max=160)])
    documento = StringField("CNPJ", validators=[Optional(), Length(max=18)])
    telefone = StringField("Telefone", validators=[Optional(), Length(max=20)])
    endereco = StringField("Endereço", validators=[Optional(), Length(max=200)])
    submit = SubmitField("Salvar")

    def to_model_kwargs(self) -> dict:
        data = {f.name: f.data for f in self if f.name not in ("submit", "csrf_token")}
        for k in ("documento", "telefone", "endereco"):
            if data.get(k) == "":
                data[k] = None
        return data


class CentroCustoForm(FlaskForm):
    nome = StringField("Nome", validators=[DataRequired(), Length(max=120)])
    tipo = SelectField(
        "Categoria",
        choices=_opcoes(TipoCentroCusto),
        validators=[DataRequired()],
    )
    ativo = BooleanField("Ativo", default=True)
    submit = SubmitField("Salvar")

    def to_model_kwargs(self) -> dict:
        data = {f.name: f.data for f in self if f.name not in ("submit", "csrf_token")}
        data["tipo"] = TipoCentroCusto(data["tipo"])
        return data


class TipoGarrafaoForm(FlaskForm):
    nome = StringField("Nome do tipo", validators=[DataRequired(), Length(max=80)])
    material = SelectField(
        "Material",
        choices=[(m.value, m.value) for m in MaterialGarrafao],
        validators=[DataRequired()],
    )
    capacidade_litros = DecimalField(
        "Capacidade (litros)",
        places=2,
        validators=[DataRequired(), NumberRange(min=Decimal("0.01"), max=Decimal("999.99"))],
        default=Decimal("20.00"),
    )
    valor_reposicao = DecimalField(
        "Custo de reposição (R$)",
        places=2,
        validators=[Optional(), NumberRange(min=Decimal("0"), max=Decimal("99999999.99"))],
        description="Custo de comprar 1 vasilhame novo. Crítico para o KPI de reposição.",
    )
    ativo = BooleanField("Ativo", default=True)
    submit = SubmitField("Salvar")

    def to_model_kwargs(self) -> dict:
        data = {f.name: f.data for f in self if f.name not in ("submit", "csrf_token")}
        data["material"] = MaterialGarrafao(data["material"])
        return data


class LocalEstoqueForm(FlaskForm):
    nome = StringField("Nome", validators=[DataRequired(), Length(max=120)])
    tipo = SelectField(
        "Tipo de local",
        choices=[(t.value, t.value.title()) for t in TipoLocal],
        validators=[DataRequired()],
    )
    veiculo_id = SelectField(
        "Veículo associado",
        coerce=lambda v: int(v) if v else None,
        validators=[Optional()],
        description="Obrigatório quando tipo='veiculo'.",
    )
    submit = SubmitField("Salvar")

    def to_model_kwargs(self) -> dict:
        data = {f.name: f.data for f in self if f.name not in ("submit", "csrf_token")}
        data["tipo"] = TipoLocal(data["tipo"])
        if data.get("veiculo_id") in ("", 0):
            data["veiculo_id"] = None
        return data
