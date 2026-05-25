"""Re-exporta todos os models para que SQLAlchemy/Alembic os descubram
ao importar `app.models`."""

from app.models.base import TenantMixin, enum_col
from app.models.cadastros import (
    CentroCusto,
    Cliente,
    Fornecedor,
    TipoCentroCusto,
    TipoCliente,
)
from app.models.financeiro import (
    FormaLancamento,
    Lancamento,
    NaturezaLancamento,
    StatusLancamento,
)
from app.models.frota import Entregador, TipoVeiculo, Veiculo
from app.models.logistica import Rota, RotaParada, StatusParada, StatusRota
from app.models.pedidos import (
    CanalPedido,
    FormaPagamento,
    Pedido,
    PedidoItem,
    Permuta,
    PoliticaPermuta,
    StatusPedido,
)
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    GarrafaoSaldo,
    LocalEstoque,
    MaterialGarrafao,
    TipoGarrafao,
    TipoLocal,
    TipoMovimento,
)
from app.models.tenant import PapelUsuario, PlanoTenant, Tenant, Usuario

__all__ = [
    # base
    "TenantMixin",
    "enum_col",
    # tenant
    "Tenant",
    "Usuario",
    "PlanoTenant",
    "PapelUsuario",
    # cadastros
    "Cliente",
    "Fornecedor",
    "CentroCusto",
    "TipoCliente",
    "TipoCentroCusto",
    # frota (mapeados sem blueprint no MVP)
    "Veiculo",
    "Entregador",
    "TipoVeiculo",
    # pool
    "TipoGarrafao",
    "LocalEstoque",
    "GarrafaoSaldo",
    "GarrafaoMovimento",
    "MaterialGarrafao",
    "TipoLocal",
    "EstadoGarrafao",
    "TipoMovimento",
    # pedidos
    "Pedido",
    "PedidoItem",
    "Permuta",
    "StatusPedido",
    "PoliticaPermuta",
    "FormaPagamento",
    "CanalPedido",
    # logistica (mapeados sem blueprint no MVP)
    "Rota",
    "RotaParada",
    "StatusRota",
    "StatusParada",
    # financeiro
    "Lancamento",
    "NaturezaLancamento",
    "StatusLancamento",
    "FormaLancamento",
]
