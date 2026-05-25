"""Repositórios dos cadastros base.

Cada classe é trivial agora (`model = X`); ganha métodos próprios quando
um filtro/regra recorrer no blueprint (ex.: `find_atacado_por_cidade()`).
Se algum repo ficar grande, mover para arquivo próprio.
"""

from app.models.cadastros import CentroCusto, Cliente, Fornecedor
from app.repositories.base import BaseRepository


class ClienteRepository(BaseRepository):
    model = Cliente


class FornecedorRepository(BaseRepository):
    model = Fornecedor


class CentroCustoRepository(BaseRepository):
    model = CentroCusto
