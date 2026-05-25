from app.repositories.base import BaseRepository
from app.repositories.cadastros_repo import (
    CentroCustoRepository,
    ClienteRepository,
    FornecedorRepository,
)
from app.repositories.financeiro_repo import LancamentoRepository
from app.repositories.pedido_repo import (
    PedidoItemRepository,
    PedidoRepository,
    PermutaRepository,
)
from app.repositories.rota_repo import RotaParadaRepository, RotaRepository
from app.repositories.pool_repo import (
    GarrafaoMovimentoRepository,
    GarrafaoSaldoRepository,
    LocalEstoqueRepository,
    TipoGarrafaoRepository,
)

__all__ = [
    "BaseRepository",
    # cadastros
    "ClienteRepository",
    "FornecedorRepository",
    "CentroCustoRepository",
    # pool
    "TipoGarrafaoRepository",
    "LocalEstoqueRepository",
    "GarrafaoSaldoRepository",
    "GarrafaoMovimentoRepository",
    # pedidos
    "PedidoRepository",
    "PedidoItemRepository",
    "PermutaRepository",
    # logistica
    "RotaRepository",
    "RotaParadaRepository",
    # financeiro
    "LancamentoRepository",
]
