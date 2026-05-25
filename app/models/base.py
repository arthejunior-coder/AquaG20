import enum

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class TenantMixin:
    """Acrescenta tenant_id a todo model de negócio.

    O filtro automático por tenant_id é responsabilidade do BaseRepository —
    este mixin garante apenas a coluna + índice, batendo com o schema oficial.
    Nunca aplicar em Tenant.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[int]:
        # O índice é declarado em __table_args__ de cada model para casar com
        # o nome usado no schema (idx_<table>_tenant), que varia por tabela.
        return mapped_column(
            BIGINT(unsigned=True),
            ForeignKey("tenants.id"),
            nullable=False,
        )


def enum_col(py_enum: type[enum.Enum], **kwargs) -> Enum:
    """Helper para mapear `enum.Enum` Python → MySQL ENUM nativo.

    Sem `values_callable`, o SQLAlchemy persiste o NOME do enum
    (`PapelUsuario.admin`) em vez do VALOR (`"admin"`), incompatível com
    o ENUM já existente no schema. Este helper centraliza essa armadilha.
    """
    return Enum(
        py_enum,
        native_enum=True,
        values_callable=lambda cls: [e.value for e in cls],
        **kwargs,
    )
