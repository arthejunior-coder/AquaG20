"""FinanceiroService — CRUD de Lancamento + ações de pagamento + fluxo de caixa.

Por que um service e não só repository:
  - Coerência natureza↔contraparte (receber tem cliente, pagar tem fornecedor)
  - Lógica de pagar/pagar-parcial/cancelar — transições simples mas com
    invariantes (não pagar lançamento cancelado, valor_pago não > valor).
  - Fluxo de caixa agregado (queries SQL com group_by mês × natureza).

Mantém-se isolado por tenant via filtro explícito nas queries — não
depende do BaseRepository para as agregações.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.sql import func

from app.models.cadastros import CentroCusto, Cliente, Fornecedor
from app.models.financeiro import (
    FormaLancamento,
    Lancamento,
    NaturezaLancamento,
    StatusLancamento,
)
from app.models.pedidos import Pedido


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------


class LancamentoInvalidoError(ValueError):
    """Dados incoerentes: natureza vs contraparte, valor inválido,
    referências de outro tenant, transição inválida."""


# ---------------------------------------------------------------------------
# DTO de fluxo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FluxoMensal:
    """Agregação de um mês no fluxo de caixa.

    `realizado` soma `valor_pago` dos lançamentos liquidados no mês;
    `previsto` soma `valor` dos lançamentos com vencimento no mês
    (independentemente do status — útil pra projeção).
    """

    ano: int
    mes: int                  # 1-12
    natureza: NaturezaLancamento
    previsto: Decimal
    realizado: Decimal


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------


class FinanceiroService:
    def __init__(self, session, tenant_id: int):
        self.session = session
        self.tenant_id = tenant_id

    # =======================================================================
    # CRIAÇÃO E EDIÇÃO
    # =======================================================================

    def criar(
        self,
        *,
        natureza: NaturezaLancamento,
        descricao: str,
        valor: Decimal,
        vencimento: date,
        centro_custo_id: int | None = None,
        cliente_id: int | None = None,
        fornecedor_id: int | None = None,
        pedido_id: int | None = None,
        forma: FormaLancamento | None = None,
    ) -> Lancamento:
        """Cria lançamento em status='pendente'. Sem commit (caller faz).

        Valida:
          - descricao/valor obrigatórios; valor > 0
          - natureza='receber' + fornecedor_id é incoerente (idem o inverso)
          - todas as FK referenciam objetos do MESMO tenant
        """
        descricao = (descricao or "").strip()
        if not descricao:
            raise LancamentoInvalidoError("descricao obrigatória")
        if valor is None or valor <= 0:
            raise LancamentoInvalidoError(f"valor deve ser > 0, got {valor}")
        if vencimento is None:
            raise LancamentoInvalidoError("vencimento obrigatório")

        # Coerência natureza ↔ contraparte
        if natureza == NaturezaLancamento.receber and fornecedor_id is not None:
            raise LancamentoInvalidoError("receber não combina com fornecedor")
        if natureza == NaturezaLancamento.pagar and cliente_id is not None:
            raise LancamentoInvalidoError("pagar não combina com cliente")

        # Valida FK (centro_custo, cliente, fornecedor, pedido) — todos do tenant
        if centro_custo_id is not None:
            cc = self.session.get(CentroCusto, centro_custo_id)
            if cc is None or cc.tenant_id != self.tenant_id:
                raise LancamentoInvalidoError(
                    f"centro_custo {centro_custo_id} não existe neste tenant"
                )
        if cliente_id is not None:
            cli = self.session.get(Cliente, cliente_id)
            if cli is None or cli.tenant_id != self.tenant_id:
                raise LancamentoInvalidoError(
                    f"cliente {cliente_id} não existe neste tenant"
                )
        if fornecedor_id is not None:
            fnc = self.session.get(Fornecedor, fornecedor_id)
            if fnc is None or fnc.tenant_id != self.tenant_id:
                raise LancamentoInvalidoError(
                    f"fornecedor {fornecedor_id} não existe neste tenant"
                )
        if pedido_id is not None:
            ped = self.session.get(Pedido, pedido_id)
            if ped is None or ped.tenant_id != self.tenant_id:
                raise LancamentoInvalidoError(
                    f"pedido {pedido_id} não existe neste tenant"
                )

        lanc = Lancamento(
            tenant_id=self.tenant_id,
            natureza=natureza,
            descricao=descricao,
            valor=valor,
            vencimento=vencimento,
            centro_custo_id=centro_custo_id,
            cliente_id=cliente_id,
            fornecedor_id=fornecedor_id,
            pedido_id=pedido_id,
            forma=forma,
            status=StatusLancamento.pendente,
        )
        self.session.add(lanc)
        self.session.flush()
        return lanc

    # =======================================================================
    # AÇÕES DE PAGAMENTO
    # =======================================================================

    def marcar_pago(
        self,
        lancamento: Lancamento,
        *,
        pago_em: date,
        valor_pago: Decimal | None = None,
        forma: FormaLancamento | None = None,
    ) -> None:
        """Liquida um lançamento. `valor_pago` default = valor total.

        Define status:
          - 'parcial' se valor_pago < valor
          - 'pago'    se valor_pago == valor
          - levanta se valor_pago > valor (não há sobra cobrável aqui)
        """
        self._exigir_do_tenant(lancamento)
        if lancamento.status == StatusLancamento.cancelado:
            raise LancamentoInvalidoError(
                "lançamento cancelado não pode ser pago — recrie um novo"
            )
        valor_pago = valor_pago if valor_pago is not None else lancamento.valor
        if valor_pago <= 0:
            raise LancamentoInvalidoError(f"valor_pago deve ser > 0, got {valor_pago}")
        if valor_pago > lancamento.valor:
            raise LancamentoInvalidoError(
                f"valor_pago ({valor_pago}) maior que valor ({lancamento.valor})"
            )

        lancamento.pago_em = pago_em
        lancamento.valor_pago = valor_pago
        lancamento.status = (
            StatusLancamento.pago
            if valor_pago == lancamento.valor
            else StatusLancamento.parcial
        )
        if forma is not None:
            lancamento.forma = forma

    def cancelar(self, lancamento: Lancamento) -> None:
        """Cancela um lançamento PENDENTE. Pagos/parciais não podem ser cancelados
        (estorno foge do MVP — recrie como movimento contrário)."""
        self._exigir_do_tenant(lancamento)
        if lancamento.status != StatusLancamento.pendente:
            raise LancamentoInvalidoError(
                f"só cancelo lançamento pendente; status atual: {lancamento.status.value}"
            )
        lancamento.status = StatusLancamento.cancelado

    def reabrir(self, lancamento: Lancamento) -> None:
        """Reverte 'pago'/'parcial'/'cancelado' → 'pendente'. Útil para
        correção de erro de digitação."""
        self._exigir_do_tenant(lancamento)
        lancamento.status = StatusLancamento.pendente
        lancamento.pago_em = None
        lancamento.valor_pago = None

    # =======================================================================
    # FLUXO DE CAIXA
    # =======================================================================

    def fluxo_mensal(
        self, *, inicio: date, fim: date
    ) -> list[FluxoMensal]:
        """Agrega lançamentos por (ano, mês, natureza) no intervalo
        [inicio, fim] (vencimento entre as datas).

        Retorna lista ordenada por ano/mes/natureza, com previsto +
        realizado em cada bucket. Buckets sem dados não aparecem.
        """
        # Previsto: agregar valor por vencimento (independente do status,
        # exceto cancelado).
        prev_stmt = (
            select(
                func.year(Lancamento.vencimento).label("ano"),
                func.month(Lancamento.vencimento).label("mes"),
                Lancamento.natureza,
                func.coalesce(func.sum(Lancamento.valor), 0).label("total"),
            )
            .where(
                Lancamento.tenant_id == self.tenant_id,
                Lancamento.vencimento >= inicio,
                Lancamento.vencimento <= fim,
                Lancamento.status != StatusLancamento.cancelado,
            )
            .group_by("ano", "mes", Lancamento.natureza)
        )
        previstos = {
            (r.ano, r.mes, r.natureza): Decimal(r.total)
            for r in self.session.execute(prev_stmt)
        }

        # Realizado: agregar valor_pago por pago_em (status pago/parcial).
        real_stmt = (
            select(
                func.year(Lancamento.pago_em).label("ano"),
                func.month(Lancamento.pago_em).label("mes"),
                Lancamento.natureza,
                func.coalesce(func.sum(Lancamento.valor_pago), 0).label("total"),
            )
            .where(
                Lancamento.tenant_id == self.tenant_id,
                Lancamento.pago_em.is_not(None),
                Lancamento.pago_em >= inicio,
                Lancamento.pago_em <= fim,
                Lancamento.status.in_([StatusLancamento.pago, StatusLancamento.parcial]),
            )
            .group_by("ano", "mes", Lancamento.natureza)
        )
        realizados = {
            (r.ano, r.mes, r.natureza): Decimal(r.total)
            for r in self.session.execute(real_stmt)
        }

        chaves = sorted(set(previstos) | set(realizados))
        return [
            FluxoMensal(
                ano=k[0], mes=k[1], natureza=k[2],
                previsto=previstos.get(k, Decimal("0")),
                realizado=realizados.get(k, Decimal("0")),
            )
            for k in chaves
        ]

    def totais_pendentes_proximos_dias(self, *, dias: int = 30) -> dict:
        """Resumo pra dashboard: quanto a pagar/receber está pendente nos
        próximos N dias (inclusive vencidos)."""
        hoje = date.today()
        limite = hoje + timedelta(days=dias)
        stmt = (
            select(
                Lancamento.natureza,
                func.coalesce(func.sum(Lancamento.valor), 0).label("total"),
            )
            .where(
                Lancamento.tenant_id == self.tenant_id,
                Lancamento.status == StatusLancamento.pendente,
                Lancamento.vencimento <= limite,
            )
            .group_by(Lancamento.natureza)
        )
        out = {NaturezaLancamento.receber: Decimal("0"),
               NaturezaLancamento.pagar: Decimal("0")}
        for row in self.session.execute(stmt):
            out[row.natureza] = Decimal(row.total)
        return out

    # =======================================================================
    # Helpers
    # =======================================================================

    def _exigir_do_tenant(self, lancamento: Lancamento) -> None:
        if lancamento.tenant_id != self.tenant_id:
            raise PermissionError(
                f"Lancamento {lancamento.id} pertence ao tenant "
                f"{lancamento.tenant_id}, service está no {self.tenant_id}"
            )
