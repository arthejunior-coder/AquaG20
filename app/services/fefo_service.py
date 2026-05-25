"""FEFOService — First-Expire, First-Out.

Dado um pedido de N garrafões de um (tipo, local, estado), sugere quais
faixas de validade despachar primeiro — sempre as mais próximas do
vencimento. Reduz a perda por vencimento dentro do depósito, que é
parte do custo da taxa de reposição mensal (~15%).

Usado em duas situações principais:

  1. Pedido sem validade exigida (varejo) — sistema escolhe FEFO automático
  2. Separação de carga — operador vê quais lotes despachar primeiro

Nota: pedidos por faixa de validade casada (atacado) NÃO usam FEFO
diretamente — a validade já está fixada. Mas o serviço pode ser
chamado para sugerir alternativas se o lote pedido não tem saldo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from app.models.pool import EstadoGarrafao, GarrafaoSaldo


@dataclass(frozen=True)
class LoteRecomendado:
    validade: date
    quantidade: int


@dataclass(frozen=True)
class SugestaoFEFO:
    lotes: list[LoteRecomendado]
    quantidade_solicitada: int

    @property
    def total_atendido(self) -> int:
        return sum(l.quantidade for l in self.lotes)

    @property
    def quantidade_faltando(self) -> int:
        return max(0, self.quantidade_solicitada - self.total_atendido)

    @property
    def atende_totalmente(self) -> bool:
        return self.quantidade_faltando == 0


class FEFOService:
    def __init__(self, session, tenant_id: int):
        self.session = session
        self.tenant_id = tenant_id

    def recomendar_lotes(
        self,
        *,
        tipo_garrafao_id: int,
        local_id: int,
        quantidade: int,
        estado: EstadoGarrafao = EstadoGarrafao.cheio,
    ) -> SugestaoFEFO:
        """Retorna a sugestão FEFO: lista de (validade, qtd_a_tirar) ordenada
        por validade ascendente, parando quando atingir `quantidade`.

        Se houver menos que `quantidade` disponível no local/estado, retorna
        o que houver — não levanta. Caller decide o que fazer (`atende_totalmente`).
        """
        if quantidade <= 0:
            raise ValueError(f"quantidade deve ser > 0, got {quantidade}")

        stmt = (
            select(GarrafaoSaldo.validade, GarrafaoSaldo.quantidade)
            .where(
                GarrafaoSaldo.tenant_id == self.tenant_id,
                GarrafaoSaldo.tipo_garrafao_id == tipo_garrafao_id,
                GarrafaoSaldo.local_id == local_id,
                GarrafaoSaldo.estado == estado,
                GarrafaoSaldo.quantidade > 0,
            )
            .order_by(GarrafaoSaldo.validade.asc())
        )

        lotes: list[LoteRecomendado] = []
        restante = quantidade
        for validade, disponivel in self.session.execute(stmt):
            if restante <= 0:
                break
            tirar = min(disponivel, restante)
            lotes.append(LoteRecomendado(validade=validade, quantidade=tirar))
            restante -= tirar

        return SugestaoFEFO(lotes=lotes, quantidade_solicitada=quantidade)
