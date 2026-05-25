"""Model do financeiro: Lancamento — a pagar e a receber numa só tabela.

Decisão de design do schema (não muda): natureza='receber'|'pagar' diferencia
no select. Simplifica o fluxo de caixa para uma única consulta agrupada por
período + natureza.

Campos de "execução":
  - `vencimento` (NOT NULL): data prevista
  - `pago_em` (NULL → pendente): data em que foi liquidado
  - `valor_pago` (NULL → não pago): valor efetivamente pago, pode ser
     menor (status='parcial') ou igual (status='pago')
  - `status`: pendente | pago | parcial | cancelado

Note que `cliente_id`/`fornecedor_id`/`pedido_id` são todos NULL-able —
um lançamento pode ser totalmente avulso (ex.: pagamento de aluguel via
centro_custo='administrativo'). O FinanceiroService valida coerência:
natureza='receber' não combina com fornecedor_id, e vice-versa.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import BIGINT, DECIMAL
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.extensions import db
from app.models.base import TenantMixin, enum_col


class NaturezaLancamento(str, enum.Enum):
    receber = "receber"
    pagar = "pagar"


class StatusLancamento(str, enum.Enum):
    pendente = "pendente"
    pago = "pago"
    parcial = "parcial"
    cancelado = "cancelado"


class FormaLancamento(str, enum.Enum):
    """Distinto de pedidos.FormaPagamento — schema usa enum próprio aqui."""

    dinheiro = "dinheiro"
    pix = "pix"
    cartao = "cartao"
    boleto = "boleto"
    transferencia = "transferencia"


class Lancamento(TenantMixin, db.Model):
    __tablename__ = "lancamentos"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    natureza: Mapped[NaturezaLancamento] = mapped_column(
        enum_col(NaturezaLancamento), nullable=False
    )
    centro_custo_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("centros_custo.id"), nullable=True
    )
    cliente_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("clientes.id"), nullable=True
    )
    fornecedor_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("fornecedores.id"), nullable=True
    )
    # NOTA: schema oficial NÃO tem FK em pedido_id (apesar das outras existirem).
    # Vínculo é opcional e informativo — não vale invalidar lançamentos antigos
    # se o pedido for excluído.
    pedido_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    descricao: Mapped[str] = mapped_column(String(200), nullable=False)
    valor: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), nullable=False)
    vencimento: Mapped[date] = mapped_column(Date, nullable=False)
    pago_em: Mapped[date | None] = mapped_column(Date, nullable=True)
    valor_pago: Mapped[Decimal | None] = mapped_column(DECIMAL(12, 2), nullable=True)
    status: Mapped[StatusLancamento] = mapped_column(
        enum_col(StatusLancamento), nullable=False,
        default=StatusLancamento.pendente, server_default="pendente",
    )
    forma: Mapped[FormaLancamento | None] = mapped_column(
        enum_col(FormaLancamento), nullable=True
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_lanc_tenant_venc", "tenant_id", "vencimento"),
        Index("idx_lanc_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Lancamento {self.id} {self.natureza.value} R${self.valor} "
            f"venc={self.vencimento} {self.status.value}>"
        )
