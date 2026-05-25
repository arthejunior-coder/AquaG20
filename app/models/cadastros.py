"""Models de cadastros base: Cliente, Fornecedor, CentroCusto.

Os 3 são entidades independentes, sem relação direta entre si neste módulo
(centros_custo só aparece em lancamentos do financeiro). Estão juntos por
serem o conteúdo do blueprint `cadastros` (passo 8).
"""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CHAR, DateTime, Index, Numeric, String
from sqlalchemy.dialects.mysql import BIGINT, INTEGER, TINYINT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.extensions import db
from app.models.base import TenantMixin, enum_col


class TipoCliente(str, enum.Enum):
    atacado = "atacado"
    varejo = "varejo"
    final = "final"


class TipoCentroCusto(str, enum.Enum):
    operacional = "operacional"
    administrativo = "administrativo"
    comercial = "comercial"
    frota = "frota"


class Cliente(TenantMixin, db.Model):
    __tablename__ = "clientes"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tipo: Mapped[TipoCliente] = mapped_column(
        enum_col(TipoCliente), nullable=False, default=TipoCliente.final, server_default="final"
    )
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    nome_fantasia: Mapped[str | None] = mapped_column(String(160), nullable=True)
    documento: Mapped[str | None] = mapped_column(String(18), nullable=True)  # CPF/CNPJ

    # ATENÇÃO: telefone com até 20 chars (lembrar do overflow do ERP BV — manter 20).
    telefone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # Endereço + geocoding (lat/long DECIMAL(10,7) — precisão ~1cm)
    endereco: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bairro: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cidade: Mapped[str | None] = mapped_column(String(100), nullable=True)
    uf: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    cep: Mapped[str | None] = mapped_column(String(9), nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)

    # DESBALANÇO de permuta — não é "posse rastreada" do garrafão.
    # Cresce só quando a troca cheio↔vazio não fecha 1-por-1 (ex.: cliente
    # novo levou 2 cheios e não tinha vazios). Em operação normal tende a 0.
    saldo_garrafoes: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, server_default="0"
    )
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_cliente_tenant", "tenant_id"),
        Index("idx_cliente_cidade", "tenant_id", "cidade", "bairro"),
    )

    def __repr__(self) -> str:
        return f"<Cliente {self.id} {self.nome!r} tipo={self.tipo.value}>"


class Fornecedor(TenantMixin, db.Model):
    """Indústria(s) onde se compra a água envasada.

    No fluxo padrão, o garrafão é do distribuidor (não se compra vasilhame
    do fornecedor) — o que se paga é água + serviço de envase. Os campos
    de lat/long são para o roteamento até a indústria.
    """

    __tablename__ = "fornecedores"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    documento: Mapped[str | None] = mapped_column(String(18), nullable=True)
    telefone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    endereco: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (Index("idx_fornecedor_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return f"<Fornecedor {self.id} {self.nome!r}>"


class CentroCusto(TenantMixin, db.Model):
    """Classificação dos lançamentos financeiros (a pagar / a receber).

    A enumeração de tipos vem do schema; o usuário pode criar quantos
    centros quiser dentro de cada categoria (ex.: "Combustível" e
    "Manutenção" ambos com tipo='frota').
    """

    __tablename__ = "centros_custo"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    tipo: Mapped[TipoCentroCusto] = mapped_column(
        enum_col(TipoCentroCusto),
        nullable=False,
        default=TipoCentroCusto.operacional,
        server_default="operacional",
    )
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")

    __table_args__ = (Index("idx_cc_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return f"<CentroCusto {self.id} {self.nome!r} tipo={self.tipo.value}>"
