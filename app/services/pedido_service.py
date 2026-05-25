"""PedidoService — criação e transições de estado de pedidos.

Responsável por:
  - Criar pedidos com 1+ itens POR FAIXA DE VALIDADE.
  - Validar cliente/tipos pertencem ao tenant.
  - Calcular `qtd_total` e `valor_total` (denormalizados) a partir dos itens.
  - Aplicar a máquina de estados:
        aberto → roteirizado → em_entrega → entregue
        aberto/roteirizado → cancelado
  - NÃO mexe em saldos. O efeito no pool acontece via PermutaService
    (passo 16) na entrega real — separação física pode até ser feita à
    parte por FEFOService.

Por que não mexer no pool aqui:
  Criar um pedido é uma INTENÇÃO de venda. O garrafão só sai do CD
  quando vira parte de uma rota (sai do CD) ou efetivamente é entregue
  ao cliente (vira permuta). Reservar saldo virtualmente é tentador,
  mas adiciona complexidade que o MVP não pediu — o ciclo é curto
  (mesmo dia) e o pedido pode ser editado/cancelado antes da entrega.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select

from app.models.cadastros import Cliente
from app.models.pedidos import (
    CanalPedido,
    FormaPagamento,
    Pedido,
    PedidoItem,
    PoliticaPermuta,
    StatusPedido,
)
from app.models.pool import TipoGarrafao


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------


class PedidoInvalidoError(ValueError):
    """Pedido com dados inválidos (cliente/tipo inexistente, item zerado, etc)."""


class TransicaoInvalidaError(RuntimeError):
    """Tentativa de transição de status que viola a máquina de estados."""


# ---------------------------------------------------------------------------
# DTO de entrada
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemPedidoInput:
    """1 linha do pedido. `validade_solicitada=None` = sem exigência (varejo)."""

    tipo_garrafao_id: int
    quantidade: int
    validade_solicitada: date | None = None
    preco_unitario: Decimal | None = None

    def __post_init__(self):
        if self.quantidade <= 0:
            raise PedidoInvalidoError(
                f"quantidade deve ser > 0, got {self.quantidade}"
            )
        if self.preco_unitario is not None and self.preco_unitario < 0:
            raise PedidoInvalidoError(
                f"preco_unitario deve ser >= 0, got {self.preco_unitario}"
            )


# ---------------------------------------------------------------------------
# Máquina de estados
# ---------------------------------------------------------------------------

# Transições permitidas. Sem map = travado.
_TRANSICOES: dict[StatusPedido, frozenset[StatusPedido]] = {
    StatusPedido.aberto: frozenset({StatusPedido.roteirizado, StatusPedido.cancelado}),
    StatusPedido.roteirizado: frozenset({StatusPedido.em_entrega, StatusPedido.cancelado}),
    StatusPedido.em_entrega: frozenset({StatusPedido.entregue}),
    StatusPedido.entregue: frozenset(),   # estado final
    StatusPedido.cancelado: frozenset(),  # estado final
}


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------


class PedidoService:
    def __init__(self, session, tenant_id: int, usuario_id: int | None = None):
        self.session = session
        self.tenant_id = tenant_id
        self.usuario_id = usuario_id

    # =======================================================================
    # CRIAÇÃO
    # =======================================================================

    def criar_pedido(
        self,
        *,
        cliente_id: int,
        itens: Iterable[ItemPedidoInput],
        politica_permuta: PoliticaPermuta = PoliticaPermuta.casar,
        forma_pagamento: FormaPagamento | None = None,
        canal: CanalPedido | None = None,
        observacao: str | None = None,
    ) -> Pedido:
        """Cria pedido com 1+ itens em uma transação (sem commit).

        Valida que cliente e todos os tipos de garrafão pertencem ao tenant.
        Calcula totais a partir dos itens — não confiar em valores externos.
        """
        itens = list(itens)
        if not itens:
            raise PedidoInvalidoError("pedido precisa ter pelo menos 1 item")

        # Cliente do tenant?
        cliente = self.session.scalar(
            select(Cliente).where(
                Cliente.tenant_id == self.tenant_id, Cliente.id == cliente_id
            )
        )
        if cliente is None:
            raise PedidoInvalidoError(f"cliente {cliente_id} não existe neste tenant")

        # Tipos de garrafão do tenant?
        tipos_ids = {i.tipo_garrafao_id for i in itens}
        tipos_existentes = set(self.session.scalars(
            select(TipoGarrafao.id).where(
                TipoGarrafao.tenant_id == self.tenant_id,
                TipoGarrafao.id.in_(tipos_ids),
            )
        ).all())
        faltantes = tipos_ids - tipos_existentes
        if faltantes:
            raise PedidoInvalidoError(
                f"tipos de garrafão inexistentes neste tenant: {sorted(faltantes)}"
            )

        # Cabeçalho
        pedido = Pedido(
            tenant_id=self.tenant_id,
            cliente_id=cliente_id,
            status=StatusPedido.aberto,
            politica_permuta=politica_permuta,
            forma_pagamento=forma_pagamento,
            canal=canal,
            observacao=observacao,
            criado_por=self.usuario_id,
            qtd_total=0,
            valor_total=Decimal("0.00"),
        )
        self.session.add(pedido)
        self.session.flush()  # garante pedido.id

        # Itens
        for inp in itens:
            self.session.add(PedidoItem(
                tenant_id=self.tenant_id,
                pedido_id=pedido.id,
                tipo_garrafao_id=inp.tipo_garrafao_id,
                validade_solicitada=inp.validade_solicitada,
                qtd_solicitada=inp.quantidade,
                qtd_atendida=0,
                preco_unitario=inp.preco_unitario,
            ))
        self.session.flush()

        self._recalcular_totais(pedido)
        return pedido

    # =======================================================================
    # TRANSIÇÕES DE ESTADO
    # =======================================================================

    def transicionar(self, pedido: Pedido, novo: StatusPedido) -> None:
        """Aplica uma transição de status validando a máquina de estados.

        Recebe a INSTÂNCIA (não o id) para forçar o caller a obter o pedido
        via repository — o que já garante isolamento por tenant.
        """
        if pedido.tenant_id != self.tenant_id:
            raise PermissionError(
                f"pedido {pedido.id} pertence ao tenant {pedido.tenant_id}, "
                f"service está no tenant {self.tenant_id}"
            )

        permitidos = _TRANSICOES.get(pedido.status, frozenset())
        if novo not in permitidos:
            raise TransicaoInvalidaError(
                f"transição inválida: {pedido.status.value} → {novo.value}. "
                f"Permitidos: {[s.value for s in permitidos] or '(nenhum, estado final)'}"
            )
        pedido.status = novo

    def cancelar(self, pedido: Pedido) -> None:
        """Atalho para `transicionar(..., cancelado)`."""
        self.transicionar(pedido, StatusPedido.cancelado)

    # =======================================================================
    # EDIÇÃO DE ITENS (apenas em pedido aberto)
    # =======================================================================

    def adicionar_item(self, pedido: Pedido, item: ItemPedidoInput) -> PedidoItem:
        """Adiciona um item a um pedido AINDA aberto. Recalcula totais."""
        self._exige_aberto(pedido)
        # Reusa a validação de tipo do criar_pedido — query simples basta
        existe = self.session.scalar(
            select(TipoGarrafao.id).where(
                TipoGarrafao.tenant_id == self.tenant_id,
                TipoGarrafao.id == item.tipo_garrafao_id,
            )
        )
        if existe is None:
            raise PedidoInvalidoError(
                f"tipo_garrafao {item.tipo_garrafao_id} não existe neste tenant"
            )
        pi = PedidoItem(
            tenant_id=self.tenant_id,
            pedido_id=pedido.id,
            tipo_garrafao_id=item.tipo_garrafao_id,
            validade_solicitada=item.validade_solicitada,
            qtd_solicitada=item.quantidade,
            qtd_atendida=0,
            preco_unitario=item.preco_unitario,
        )
        self.session.add(pi)
        self.session.flush()
        self._recalcular_totais(pedido)
        return pi

    def remover_item(self, pedido: Pedido, item: PedidoItem) -> None:
        """Remove um item de um pedido AINDA aberto. Recalcula totais."""
        self._exige_aberto(pedido)
        if item.pedido_id != pedido.id or item.tenant_id != self.tenant_id:
            raise PedidoInvalidoError("item não pertence a este pedido/tenant")
        self.session.delete(item)
        self.session.flush()
        self._recalcular_totais(pedido)

    # =======================================================================
    # Helpers internos
    # =======================================================================

    def _exige_aberto(self, pedido: Pedido) -> None:
        if pedido.tenant_id != self.tenant_id:
            raise PermissionError(
                f"pedido {pedido.id} pertence ao tenant {pedido.tenant_id}"
            )
        if pedido.status != StatusPedido.aberto:
            raise TransicaoInvalidaError(
                f"pedido {pedido.id} está em {pedido.status.value}; "
                "itens só podem ser editados em 'aberto'"
            )

    def _recalcular_totais(self, pedido: Pedido) -> None:
        """Recalcula qtd_total e valor_total a partir dos itens persistidos."""
        # Pedido já validado por callers; filtro por pedido.id é mais restritivo.
        itens = self.session.scalars(
            select(PedidoItem).where(PedidoItem.pedido_id == pedido.id)  # NO-TENANT-FILTER
        ).all()
        pedido.qtd_total = sum(i.qtd_solicitada for i in itens)
        pedido.valor_total = sum(
            (i.qtd_solicitada * (i.preco_unitario or Decimal("0.00"))
             for i in itens),
            Decimal("0.00"),
        )
