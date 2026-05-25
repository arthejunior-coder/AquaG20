"""PermutaService — registra a entrega (troca cheio↔vazio) ao cliente.

A operação acontece no LOCAL VEÍCULO (o entregador está na rua):
  - sai Q cheio (validade_entregue)
  - entra Q vazio (validade_recebida)

Por isso `quantidade` é em PARES (1 cheio sai = 1 vazio entra) — assim a
permuta respeita a invariante do pool (`delta_total == 0`). Se o cliente
NÃO devolveu vazios suficientes (ou trouxe vazios a mais), o operador
passa `desbalanco_garrafoes` separadamente: esse valor ajusta o
`cliente.saldo_garrafoes` (que é DESBALANÇO, não posse) — não cria
movimento no pool.

KPIs calculados (e gravados) em cada linha:
  - `casado` = (validade_entregue == validade_recebida)
  - `concessao` = (politica_permuta == 'casar' AND NOT casado)
      Em política 'flexivel', descasamento NÃO é concessão (é o normal).

O service NÃO transiciona o status do pedido — isso é responsabilidade
do route, que orquestra "registrar entrega → status=entregue" como um
fluxo de UI. Mantém o service mais reutilizável (ex.: rota Fase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from sqlalchemy import select

from app.models.cadastros import Cliente
from app.models.pedidos import Pedido, PedidoItem, Permuta, PoliticaPermuta, StatusPedido
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    LocalEstoque,
    TipoLocal,
    TipoMovimento,
)
from app.services.pool_service import Delta, PoolService


# ---------------------------------------------------------------------------
# Exceções e DTO
# ---------------------------------------------------------------------------


class EntregaInvalidaError(ValueError):
    """Dados de entrega incoerentes (pedido em status errado, quantidades
    incompatíveis, validade ausente)."""


@dataclass(frozen=True)
class LinhaEntregaInput:
    """1 permuta concreta: cheio sai (validade_entregue), vazio entra
    (validade_recebida). Quantidade em PARES.
    """

    tipo_garrafao_id: int
    quantidade: int                # nº de pares cheio↔vazio
    validade_entregue: date        # validade do cheio que sai (NOT NULL)
    validade_recebida: date | None = None  # default = igual à entregue (casado)

    def __post_init__(self):
        if self.quantidade <= 0:
            raise EntregaInvalidaError(
                f"quantidade deve ser > 0, got {self.quantidade}"
            )
        if self.validade_entregue is None:
            raise EntregaInvalidaError("validade_entregue é obrigatória")


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------


class PermutaService:
    def __init__(self, session, tenant_id: int, usuario_id: int | None = None):
        self.session = session
        self.tenant_id = tenant_id
        self.usuario_id = usuario_id
        self.pool = PoolService(session, tenant_id, usuario_id)

    def registrar_entrega(
        self,
        *,
        pedido: Pedido,
        veiculo_local_id: int,
        linhas: Iterable[LinhaEntregaInput],
        desbalanco_garrafoes: int = 0,
        observacao: str | None = None,
        parada_id: int | None = None,
    ) -> list[Permuta]:
        """Registra a entrega: para cada linha, faz a permuta no pool e
        grava um registro em `permutas`. Tudo em UMA transação — caller
        commita.

        - Pedido deve estar em 'roteirizado' ou 'em_entrega'.
        - Local deve ser do tenant e tipo='veiculo'.
        - Cada item do pedido recebe `qtd_atendida += quantidade da linha
          casada por tipo_garrafao_id + validade_solicitada`. Sobras
          (entrega de tipo/validade não pedida) NÃO atualizam itens — só
          geram a Permuta.
        - `desbalanco_garrafoes` ajusta `cliente.saldo_garrafoes`:
            > 0  → cliente DEVE garrafões (levou mais do que devolveu)
            < 0  → cliente entregou EM EXCESSO (saldo a favor dele)
            == 0 → balanceado (caso comum)
        """
        # ---- Validações ----
        self._exigir_pedido_do_tenant(pedido)
        if pedido.status not in (StatusPedido.roteirizado, StatusPedido.em_entrega):
            raise EntregaInvalidaError(
                f"Pedido #{pedido.id} está em '{pedido.status.value}'; "
                f"entrega exige 'roteirizado' ou 'em_entrega'."
            )

        local = self.session.get(LocalEstoque, veiculo_local_id)
        if local is None or local.tenant_id != self.tenant_id:
            raise EntregaInvalidaError(
                f"Local id={veiculo_local_id} não existe neste tenant"
            )
        if local.tipo != TipoLocal.veiculo:
            raise EntregaInvalidaError(
                f"Local id={veiculo_local_id} é '{local.tipo.value}'; "
                "entrega só sai de local tipo='veiculo'."
            )

        linhas_list = list(linhas)
        if not linhas_list:
            raise EntregaInvalidaError("entrega precisa de pelo menos 1 linha")

        # ---- Para cada linha: pool + Permuta ----
        permutas_criadas: list[Permuta] = []
        for linha in linhas_list:
            permuta = self._processar_linha(
                pedido, local.id, linha, observacao, parada_id,
            )
            permutas_criadas.append(permuta)

        # ---- Atualiza qtd_atendida nos itens (match por tipo+validade) ----
        self._atualizar_itens_atendidos(pedido, linhas_list)

        # ---- Desbalanço no cliente ----
        if desbalanco_garrafoes != 0:
            cliente = self.session.get(Cliente, pedido.cliente_id)
            if cliente is None or cliente.tenant_id != self.tenant_id:
                raise EntregaInvalidaError(
                    f"Cliente do pedido (id={pedido.cliente_id}) não encontrado"
                )
            cliente.saldo_garrafoes = (cliente.saldo_garrafoes or 0) + desbalanco_garrafoes

        return permutas_criadas

    # =======================================================================
    # Helpers internos
    # =======================================================================

    def _exigir_pedido_do_tenant(self, pedido: Pedido) -> None:
        if pedido.tenant_id != self.tenant_id:
            raise PermissionError(
                f"Pedido {pedido.id} pertence ao tenant {pedido.tenant_id}, "
                f"service está no tenant {self.tenant_id}"
            )

    def _processar_linha(
        self,
        pedido: Pedido,
        veiculo_local_id: int,
        linha: LinhaEntregaInput,
        observacao: str | None,
        parada_id: int | None = None,
    ) -> Permuta:
        """Aplica o swap no pool e grava o registro de Permuta."""
        # Default: vazio volta com a mesma validade (caso casado)
        validade_recebida = linha.validade_recebida or linha.validade_entregue
        casado = (validade_recebida == linha.validade_entregue)
        concessao = (
            pedido.politica_permuta == PoliticaPermuta.casar and not casado
        )

        # Pool swap (delta_total == 0)
        self.pool.aplicar_deltas(
            tipo=TipoMovimento.permuta,
            tipo_garrafao_id=linha.tipo_garrafao_id,
            deltas=[
                Delta(
                    local_id=veiculo_local_id, estado=EstadoGarrafao.cheio,
                    validade=linha.validade_entregue,
                    quantidade=linha.quantidade, sinal=-1,
                ),
                Delta(
                    local_id=veiculo_local_id, estado=EstadoGarrafao.vazio,
                    validade=validade_recebida,
                    quantidade=linha.quantidade, sinal=+1,
                ),
            ],
            referencia_tipo="pedido",
            referencia_id=pedido.id,
            observacao=observacao,
        )

        permuta = Permuta(
            tenant_id=self.tenant_id,
            parada_id=parada_id,  # opcional — preenchido quando entrega vem de rota
            pedido_id=pedido.id,
            cliente_id=pedido.cliente_id,
            tipo_garrafao_id=linha.tipo_garrafao_id,
            quantidade=linha.quantidade,
            validade_entregue=linha.validade_entregue,
            validade_recebida=validade_recebida,
            casado=casado,
            concessao=concessao,
        )
        self.session.add(permuta)
        self.session.flush()
        return permuta

    def _atualizar_itens_atendidos(
        self, pedido: Pedido, linhas: list[LinhaEntregaInput]
    ) -> None:
        """Para cada linha entregue, soma `qtd_atendida` no item que casa
        (mesmo tipo + mesma validade_solicitada). Se não houver match
        exato, tenta match com validade_solicitada NULL (varejo); se ainda
        assim nada bate, NÃO atualiza (entrega 'extra' fora do pedido).
        """
        # Pedido já validado em _exigir_pedido_do_tenant antes de chamar.
        itens = self.session.scalars(
            select(PedidoItem).where(PedidoItem.pedido_id == pedido.id)  # NO-TENANT-FILTER
        ).all()

        for linha in linhas:
            match = next(
                (i for i in itens
                 if i.tipo_garrafao_id == linha.tipo_garrafao_id
                 and i.validade_solicitada == linha.validade_entregue),
                None,
            )
            if match is None:
                # Tenta varejo: item com validade_solicitada NULL
                match = next(
                    (i for i in itens
                     if i.tipo_garrafao_id == linha.tipo_garrafao_id
                     and i.validade_solicitada is None),
                    None,
                )
            if match is not None:
                match.qtd_atendida = (match.qtd_atendida or 0) + linha.quantidade
