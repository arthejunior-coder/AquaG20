"""Repositórios de pedidos e permutas.

Permuta também ganha repo aqui porque ela é consultada/criada nos KPIs
e na entrega — quem orquestra a CRIAÇÃO é o PermutaService (passo 16),
mas leitura por blueprint passa por esse repo.
"""

from app.models.pedidos import Pedido, PedidoItem, Permuta
from app.repositories.base import BaseRepository


class PedidoRepository(BaseRepository):
    model = Pedido


class PedidoItemRepository(BaseRepository):
    model = PedidoItem


class PermutaRepository(BaseRepository):
    model = Permuta
