"""IndicadoresService — 3 KPIs que justificam o produto.

Os indicadores são a razão de ser do sistema; sem eles, o controle de
faixa de validade é só esforço operacional sem retorno mensurável.

  (a) Envelhecimento do pool
      Quantos cheios estão por mês de validade — visão de curto/médio
      prazo para agir antes do vencimento (FEFO ataca isso na entrega).
      Janela default: próximos 6 meses + vencidos (atrasados).

  (b) Taxa de casamento (últimos 30 dias)
      SUM(quantidade WHERE casado) / SUM(quantidade) nas permutas.
      Permutas em política 'flexivel' já não contam descasamento como
      problema — o casamento é só métrica nesse caso. O service NÃO
      separa por política aqui; é cru no MVP. Filtragem fica para o
      relatório dedicado (Fase 2).

  (c) Taxa de reposição mensal (R$)
      Custo financeiro do que o pool perdeu nos últimos 30 dias —
      descarte (sai do pool) + avaria (cheio/vazio vira avariado).
      Multiplica quantidade × tipos_garrafao.valor_reposicao.

      ⚠ Pegadinha: avaria gera 2 movimentos com a MESMA quantidade
        (-X estado_origem, +X avariado). Somar TUDO duplica.
        Solução: contar avaria SÓ na linha estado='avariado' (1 por
        evento), descarte em todas as linhas (1 por evento).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, or_, select
from sqlalchemy.sql import func

from app.models.pedidos import Permuta
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    GarrafaoSaldo,
    TipoGarrafao,
    TipoMovimento,
)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaixaEnvelhecimento:
    ano: int
    mes: int                  # 1..12
    quantidade: int
    vencido: bool             # True se essa faixa já está vencida hoje


@dataclass(frozen=True)
class TaxaCasamento:
    permutas_quantidade_total: int     # soma de quantidade nas permutas
    permutas_quantidade_casada: int    # idem, só onde casado=1
    taxa: Decimal                      # 0..1 (cuidado: 0 se total=0)

    @property
    def percentual(self) -> Decimal:
        """Taxa em percentual (0..100)."""
        return self.taxa * Decimal("100")


@dataclass(frozen=True)
class CustoReposicao:
    descarte_unidades: int
    descarte_valor: Decimal
    avaria_unidades: int
    avaria_valor: Decimal

    @property
    def total_unidades(self) -> int:
        return self.descarte_unidades + self.avaria_unidades

    @property
    def total_valor(self) -> Decimal:
        return self.descarte_valor + self.avaria_valor


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------


class IndicadoresService:
    def __init__(self, session, tenant_id: int):
        self.session = session
        self.tenant_id = tenant_id

    # =======================================================================
    # (a) Envelhecimento do pool
    # =======================================================================

    def envelhecimento(self, *, meses_futuros: int = 6) -> list[FaixaEnvelhecimento]:
        """Retorna cheios agrupados por mês de validade.

        Janela: validades <= hoje + `meses_futuros`. Inclui vencidos
        (validade < hoje) — eles importam mais do que os futuros.

        Ordenado por (ano, mes) ASC. Buckets sem garrafões NÃO aparecem.
        """
        hoje = date.today()
        # Limite superior: último dia do mês "hoje + meses_futuros"
        # Aproximação simples: hoje + 30 * meses_futuros dias (suficiente p/ MVP).
        limite = hoje + timedelta(days=30 * meses_futuros)

        stmt = (
            select(
                func.year(GarrafaoSaldo.validade).label("ano"),
                func.month(GarrafaoSaldo.validade).label("mes"),
                func.coalesce(func.sum(GarrafaoSaldo.quantidade), 0).label("qtd"),
            )
            .where(
                GarrafaoSaldo.tenant_id == self.tenant_id,
                GarrafaoSaldo.estado == EstadoGarrafao.cheio,
                GarrafaoSaldo.quantidade > 0,
                GarrafaoSaldo.validade <= limite,
            )
            .group_by("ano", "mes")
            .order_by("ano", "mes")
        )
        rows = self.session.execute(stmt).all()
        out = []
        for r in rows:
            # Vencido: último dia do mês está antes de hoje
            ultimo_dia_mes = _ultimo_dia_do_mes(r.ano, r.mes)
            vencido = ultimo_dia_mes < hoje
            out.append(FaixaEnvelhecimento(
                ano=int(r.ano), mes=int(r.mes),
                quantidade=int(r.qtd), vencido=vencido,
            ))
        return out

    # =======================================================================
    # (b) Taxa de casamento
    # =======================================================================

    def taxa_casamento(self, *, dias: int = 30) -> TaxaCasamento:
        """Soma quantidade em Permutas dos últimos `dias` dias.

        Numerador: soma de quantidade onde casado=1.
        Denominador: soma de quantidade total.
        Taxa = numerador / denominador (0 se denominador=0).
        """
        desde = date.today() - timedelta(days=dias)
        stmt = (
            select(
                func.coalesce(func.sum(Permuta.quantidade), 0).label("total"),
                func.coalesce(
                    func.sum(
                        case((Permuta.casado == 1, Permuta.quantidade), else_=0)
                    ),
                    0,
                ).label("casada"),
            )
            .where(
                Permuta.tenant_id == self.tenant_id,
                Permuta.criado_em >= desde,
            )
        )
        row = self.session.execute(stmt).one()
        total = int(row.total)
        casada = int(row.casada)
        taxa = (
            (Decimal(casada) / Decimal(total))
            if total > 0 else Decimal("0")
        )
        return TaxaCasamento(
            permutas_quantidade_total=total,
            permutas_quantidade_casada=casada,
            taxa=taxa,
        )

    # =======================================================================
    # (c) Custo de reposição (descarte + avaria)
    # =======================================================================

    def custo_reposicao(self, *, dias: int = 30) -> CustoReposicao:
        """Custo financeiro de garrafões "perdidos" nos últimos `dias` dias.

        Descarte: cada mov é 1 evento; soma quantidade direto.
        Avaria: cada evento tem 2 movs (origem -, avariado +) com mesma
                quantidade. Conta só a linha estado='avariado' para não
                duplicar.

        Multiplica quantidade × tipos_garrafao.valor_reposicao. Tipos
        sem valor_reposicao definido contam como 0 (com COALESCE).
        """
        desde = date.today() - timedelta(days=dias)

        # Filtro: descarte (qualquer linha) OU avaria (só linha estado='avariado')
        filtro = or_(
            GarrafaoMovimento.tipo == TipoMovimento.descarte,
            and_(
                GarrafaoMovimento.tipo == TipoMovimento.avaria,
                GarrafaoMovimento.estado == EstadoGarrafao.avariado,
            ),
        )

        stmt = (
            select(
                GarrafaoMovimento.tipo.label("tipo"),
                func.coalesce(func.sum(GarrafaoMovimento.quantidade), 0).label("qtd"),
                func.coalesce(
                    func.sum(
                        GarrafaoMovimento.quantidade
                        * func.coalesce(TipoGarrafao.valor_reposicao, 0)
                    ),
                    0,
                ).label("valor"),
            )
            .join(TipoGarrafao, TipoGarrafao.id == GarrafaoMovimento.tipo_garrafao_id)
            .where(
                GarrafaoMovimento.tenant_id == self.tenant_id,
                GarrafaoMovimento.criado_em >= desde,
                filtro,
            )
            .group_by(GarrafaoMovimento.tipo)
        )
        descarte_qtd = avaria_qtd = 0
        descarte_val = avaria_val = Decimal("0")
        for row in self.session.execute(stmt):
            if row.tipo == TipoMovimento.descarte:
                descarte_qtd = int(row.qtd)
                descarte_val = Decimal(row.valor)
            elif row.tipo == TipoMovimento.avaria:
                avaria_qtd = int(row.qtd)
                avaria_val = Decimal(row.valor)
        return CustoReposicao(
            descarte_unidades=descarte_qtd, descarte_valor=descarte_val,
            avaria_unidades=avaria_qtd, avaria_valor=avaria_val,
        )

    # =======================================================================
    # Snapshot para o dashboard
    # =======================================================================

    def snapshot(self) -> dict:
        """Tudo numa única chamada — usado pelo dashboard."""
        return {
            "envelhecimento": self.envelhecimento(),
            "casamento": self.taxa_casamento(),
            "custo_reposicao": self.custo_reposicao(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ultimo_dia_do_mes(ano: int, mes: int) -> date:
    """Último dia do mês informado (28-31). Sem dependência de calendar."""
    if mes == 12:
        return date(ano + 1, 1, 1) - timedelta(days=1)
    return date(ano, mes + 1, 1) - timedelta(days=1)
