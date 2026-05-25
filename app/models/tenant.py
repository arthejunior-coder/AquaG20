import enum
from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, TINYINT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.extensions import db
from app.models.base import TenantMixin, enum_col


class PlanoTenant(str, enum.Enum):
    trial = "trial"
    basico = "basico"
    pro = "pro"
    enterprise = "enterprise"


class PapelUsuario(str, enum.Enum):
    admin = "admin"
    gestor = "gestor"
    atendimento = "atendimento"
    motorista = "motorista"
    financeiro = "financeiro"


class Tenant(db.Model):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    razao_social: Mapped[str] = mapped_column(String(160), nullable=False)
    nome_fantasia: Mapped[str | None] = mapped_column(String(160), nullable=True)
    cnpj: Mapped[str | None] = mapped_column(String(18), nullable=True)
    plano: Mapped[PlanoTenant] = mapped_column(
        enum_col(PlanoTenant), nullable=False, default=PlanoTenant.trial
    )
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="tenant", lazy="raise")

    __table_args__ = (UniqueConstraint("cnpj", name="uq_tenant_cnpj"),)

    def __repr__(self) -> str:
        return f"<Tenant {self.id} {self.razao_social!r}>"


class Usuario(UserMixin, TenantMixin, db.Model):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False)
    senha_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    papel: Mapped[PapelUsuario] = mapped_column(
        enum_col(PapelUsuario), nullable=False, default=PapelUsuario.atendimento
    )
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="usuarios", lazy="raise")

    __table_args__ = (
        UniqueConstraint("email", name="uq_usuario_email"),
        Index("idx_usuario_tenant", "tenant_id"),
    )

    @property
    def is_active(self) -> bool:  # override do UserMixin
        return bool(self.ativo)

    def __repr__(self) -> str:
        return f"<Usuario {self.id} {self.email!r} papel={self.papel.value}>"
