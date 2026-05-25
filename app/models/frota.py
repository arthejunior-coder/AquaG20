"""Models da frota: Veiculo, Entregador.

São mapeados desde já porque as tabelas existem no schema e têm FKs
referenciadas por outros modelos (locais_estoque.veiculo_id, rotas.*).
**Não têm blueprint próprio no MVP** — a operação de rotas é Fase 2.
"""

import enum

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.mysql import BIGINT, INTEGER, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantMixin, enum_col


class TipoVeiculo(str, enum.Enum):
    caminhao = "caminhao"
    pickup = "pickup"
    moto = "moto"


class Veiculo(TenantMixin, db.Model):
    __tablename__ = "veiculos"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tipo: Mapped[TipoVeiculo] = mapped_column(enum_col(TipoVeiculo), nullable=False)
    placa: Mapped[str | None] = mapped_column(String(8), nullable=True)
    descricao: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Capacidade em número de garrafões — base para validar carga
    capacidade_garrafoes: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, server_default="0"
    )
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")

    __table_args__ = (Index("idx_veiculo_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return f"<Veiculo {self.id} {self.tipo.value} {self.placa!r}>"


class Entregador(TenantMixin, db.Model):
    """Motorista/entregador. `usuario_id` é opcional — nem todo entregador
    tem login (alguns só recebem a rota impressa)."""

    __tablename__ = "entregadores"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    usuario_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("usuarios.id"), nullable=True
    )
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    telefone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cnh: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")

    __table_args__ = (Index("idx_entregador_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return f"<Entregador {self.id} {self.nome!r}>"
