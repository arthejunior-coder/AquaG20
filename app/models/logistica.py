"""Models de rotas/paradas — mapeados desde já, SEM blueprint no MVP.

A operação de roteirização (planejar rotas, atribuir veículo+entregador,
ordenar paradas, navegar GPS) é Fase 2. Mapear as tabelas agora permite:

  1. Permuta.parada_id pode apontar para rota_paradas mesmo que NULL no MVP.
  2. flask db migrate retorna "No changes detected" — o schema importado
     bate 100% com os models.
  3. Quando a Fase 2 começar, só precisa criar o blueprint + service;
     as tabelas e relacionamentos já existem corretos.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index
from sqlalchemy.dialects.mysql import BIGINT, DECIMAL, INTEGER
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.extensions import db
from app.models.base import TenantMixin, enum_col


class StatusRota(str, enum.Enum):
    planejada = "planejada"
    em_andamento = "em_andamento"
    concluida = "concluida"
    cancelada = "cancelada"


class StatusParada(str, enum.Enum):
    pendente = "pendente"
    entregue = "entregue"
    falhou = "falhou"


class Rota(TenantMixin, db.Model):
    """Agrupamento diário de pedidos atribuído a 1 veículo+entregador."""

    __tablename__ = "rotas"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    data_rota: Mapped[date] = mapped_column(Date, nullable=False)
    veiculo_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("veiculos.id"), nullable=True
    )
    entregador_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("entregadores.id"), nullable=True
    )
    status: Mapped[StatusRota] = mapped_column(
        enum_col(StatusRota), nullable=False,
        default=StatusRota.planejada, server_default="planejada",
    )
    distancia_km: Mapped[Decimal | None] = mapped_column(DECIMAL(8, 2), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (Index("idx_rota_tenant_data", "tenant_id", "data_rota"),)

    def __repr__(self) -> str:
        return f"<Rota {self.id} {self.data_rota} {self.status.value}>"


class RotaParada(TenantMixin, db.Model):
    """Parada da rota: ordem + status + quantidades efetivas.

    `qtd_entregue` é número de cheios saídos; `qtd_recolhido` é número
    de vazios voltando. Diferença vira saldo_garrafoes no cliente
    (desbalanço — ver Cliente.saldo_garrafoes).
    """

    __tablename__ = "rota_paradas"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    rota_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("rotas.id"), nullable=False
    )
    pedido_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("pedidos.id"), nullable=False
    )
    ordem: Mapped[int] = mapped_column(INTEGER, nullable=False, default=0, server_default="0")
    status: Mapped[StatusParada] = mapped_column(
        enum_col(StatusParada), nullable=False,
        default=StatusParada.pendente, server_default="pendente",
    )
    entregue_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    qtd_entregue: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    qtd_recolhido: Mapped[int | None] = mapped_column(INTEGER, nullable=True)

    __table_args__ = (Index("idx_parada_rota", "rota_id", "ordem"),)

    def __repr__(self) -> str:
        return f"<RotaParada rota={self.rota_id} pedido={self.pedido_id} ordem={self.ordem}>"
