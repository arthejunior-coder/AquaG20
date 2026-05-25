"""Repositories do POOL de garrafões.

Para Tipo/Local, repos triviais. Para Saldo/Movimento, são pontos de
extensão: o `PoolService` (passo 10) é quem orquestra as transações
atômicas movimento+saldo — estes repos apenas dão leitura segura por
tenant; ESCRITA direta em saldos/movimentos NÃO deve ser feita por fora
do service, mesmo que a API exista.
"""

from app.models.pool import (
    GarrafaoMovimento,
    GarrafaoSaldo,
    LocalEstoque,
    TipoGarrafao,
)
from app.repositories.base import BaseRepository


class TipoGarrafaoRepository(BaseRepository):
    model = TipoGarrafao


class LocalEstoqueRepository(BaseRepository):
    model = LocalEstoque


class GarrafaoSaldoRepository(BaseRepository):
    """Leitura de saldos. Escrita SEMPRE via PoolService (passo 10)."""

    model = GarrafaoSaldo


class GarrafaoMovimentoRepository(BaseRepository):
    """Leitura do livro-razão. Inserção SEMPRE via PoolService.

    Override `add()` para tornar explícita a regra — quem insere
    movimento direto está saindo do trilho do projeto.
    """

    model = GarrafaoMovimento

    def add(self, **kwargs):
        raise RuntimeError(
            "GarrafaoMovimento.add() proibido fora do PoolService. "
            "Use PoolService.registrar_movimento() para garantir "
            "atomicidade movimento+saldo e invariantes do pool."
        )
