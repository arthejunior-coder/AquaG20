"""Repositórios de Rota e RotaParada."""

from app.models.logistica import Rota, RotaParada
from app.repositories.base import BaseRepository


class RotaRepository(BaseRepository):
    model = Rota


class RotaParadaRepository(BaseRepository):
    model = RotaParada
