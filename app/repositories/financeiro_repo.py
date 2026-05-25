"""Repository do financeiro: Lancamento."""

from app.models.financeiro import Lancamento
from app.repositories.base import BaseRepository


class LancamentoRepository(BaseRepository):
    model = Lancamento
