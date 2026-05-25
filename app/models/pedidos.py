"""Models de pedidos e permutas.

Três entidades:
  - Pedido        : cabeçalho (cliente, totais, política de permuta)
  - PedidoItem    : 1 linha POR FAIXA DE VALIDADE (granularidade central)
  - Permuta       : evento da troca cheio↔vazio na entrega (KPI casamento)

⚠ Por que itens por faixa de validade?
A permuta CASADA depende de saber, na hora da entrega, qual validade
o cliente recebeu — para o vazio de volta ter validade compatível.
Pedido atacado tem várias linhas (10×2027, 15×2028, 35×2029); pedido
varejo costuma ter UMA linha com `validade_solicitada=NULL` (sem exigência).

A diferença entre `qtd_solicitada` (o que o cliente pediu) e
`qtd_atendida` (o que de fato saiu) é preenchida na separação/entrega.
Concessões viram `Permuta.concessao=True` na entrega — não mexem aqui.

⚠ MVP sem rotas: `Permuta.parada_id` é NULL até o blueprint de rotas
existir (Fase 2). Queries de KPI devem usar LEFT JOIN em rota_paradas.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import BIGINT, DECIMAL, INTEGER, TINYINT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.extensions import db
from app.models.base import TenantMixin, enum_col


# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class StatusPedido(str, enum.Enum):
    aberto = "aberto"
    roteirizado = "roteirizado"
    em_entrega = "em_entrega"
    entregue = "entregue"
    cancelado = "cancelado"


class PoliticaPermuta(str, enum.Enum):
    """Política aplicada na hora da entrega:
        casar    — tenta manter validade. Descasamento vira concessão (atacado).
        flexivel — aceita qualquer validade no retorno (varejo/consumidor final).
    """

    casar = "casar"
    flexivel = "flexivel"


class FormaPagamento(str, enum.Enum):
    dinheiro = "dinheiro"
    pix = "pix"
    cartao = "cartao"
    prazo = "prazo"


class CanalPedido(str, enum.Enum):
    telefone = "telefone"
    whatsapp = "whatsapp"
    app = "app"
    balcao = "balcao"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Pedido(TenantMixin, db.Model):
    """Cabeçalho do pedido: cliente + totais + política de permuta.

    `qtd_total` e `valor_total` são DENORMALIZADOS (soma dos itens). O
    PedidoService recalcula em qualquer mutação de itens — nunca confiar
    em update manual desses campos pela view.
    """

    __tablename__ = "pedidos"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    cliente_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("clientes.id"), nullable=False
    )
    status: Mapped[StatusPedido] = mapped_column(
        enum_col(StatusPedido), nullable=False,
        default=StatusPedido.aberto, server_default="aberto",
    )
    politica_permuta: Mapped[PoliticaPermuta] = mapped_column(
        enum_col(PoliticaPermuta), nullable=False,
        default=PoliticaPermuta.casar, server_default="casar",
    )
    qtd_total: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, server_default="0"
    )
    valor_total: Mapped[Decimal] = mapped_column(
        DECIMAL(12, 2), nullable=False, default=Decimal("0.00"), server_default="0.00"
    )
    forma_pagamento: Mapped[FormaPagamento | None] = mapped_column(
        enum_col(FormaPagamento), nullable=True
    )
    canal: Mapped[CanalPedido | None] = mapped_column(enum_col(CanalPedido), nullable=True)
    observacao: Mapped[str | None] = mapped_column(String(255), nullable=True)
    criado_por: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    itens: Mapped[list["PedidoItem"]] = relationship(
        "PedidoItem", back_populates="pedido", cascade="all, delete-orphan",
        order_by="PedidoItem.id",
    )

    __table_args__ = (
        Index("idx_pedido_tenant_status", "tenant_id", "status"),
        Index("idx_pedido_cliente", "cliente_id"),
    )

    def __repr__(self) -> str:
        return f"<Pedido {self.id} cliente={self.cliente_id} {self.status.value} qtd={self.qtd_total}>"


class PedidoItem(TenantMixin, db.Model):
    """Linha do pedido POR FAIXA DE VALIDADE.

    `validade_solicitada=NULL` significa "sem exigência" (varejo). A
    distinção entre `qtd_solicitada` e `qtd_atendida` é proposital: ela
    aparece quando há concessão (entrega com validade diferente) ou
    quando o estoque não atende totalmente o pedido.
    """

    __tablename__ = "pedido_itens"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    pedido_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("pedidos.id"), nullable=False
    )
    tipo_garrafao_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tipos_garrafao.id"), nullable=False
    )
    validade_solicitada: Mapped[date | None] = mapped_column(Date, nullable=True)
    qtd_solicitada: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, server_default="0"
    )
    qtd_atendida: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, server_default="0"
    )
    preco_unitario: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2), nullable=True)

    pedido: Mapped["Pedido"] = relationship("Pedido", back_populates="itens")

    __table_args__ = (Index("idx_item_pedido", "pedido_id"),)

    def __repr__(self) -> str:
        return (
            f"<PedidoItem pedido={self.pedido_id} tipo={self.tipo_garrafao_id} "
            f"val={self.validade_solicitada} sol={self.qtd_solicitada} at={self.qtd_atendida}>"
        )


class Permuta(TenantMixin, db.Model):
    """Evento de troca cheio↔vazio realizada na entrega.

    ⭐ Onde o descasamento é medido. Para CADA tipo de garrafão entregue
    numa parada, surge uma (ou mais) linhas de permuta. `casado` indica
    se a validade do vazio recebido bate com a do cheio entregue.

    `concessao=True` quando a política do pedido era `casar`, mas a
    operação aceitou validade diferente para não perder a venda. Em
    `flexivel`, descasamento NÃO é concessão — é o normal.

    `parada_id` é NULL no MVP (sem rotas). Queries usam LEFT JOIN.
    """

    __tablename__ = "permutas"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    parada_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("rota_paradas.id"), nullable=True
    )
    pedido_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("pedidos.id"), nullable=True
    )
    cliente_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("clientes.id"), nullable=False
    )
    tipo_garrafao_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tipos_garrafao.id"), nullable=False
    )
    quantidade: Mapped[int] = mapped_column(INTEGER, nullable=False)
    validade_entregue: Mapped[date | None] = mapped_column(Date, nullable=True)
    validade_recebida: Mapped[date | None] = mapped_column(Date, nullable=True)
    # casado/concessao: TINYINT(1) no schema; usamos bool no Python.
    casado: Mapped[bool] = mapped_column(
        TINYINT(1), nullable=False, default=0, server_default="0"
    )
    concessao: Mapped[bool] = mapped_column(
        TINYINT(1), nullable=False, default=0, server_default="0"
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_permuta_tenant", "tenant_id", "criado_em"),
        Index("idx_permuta_casado", "tenant_id", "casado"),
    )

    def __repr__(self) -> str:
        return (
            f"<Permuta {self.id} cliente={self.cliente_id} tipo={self.tipo_garrafao_id} "
            f"qtd={self.quantidade} casado={bool(self.casado)} concessao={bool(self.concessao)}>"
        )
