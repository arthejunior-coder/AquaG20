"""RotaService — planejamento e execução de rotas de entrega.

Orquestra o ciclo:
    planejada → em_andamento → concluida
    planejada/em_andamento → cancelada

Quando rota é INICIADA, todos os pedidos das paradas vão para 'em_entrega'.
Cada parada delivered marca a parada como 'entregue' (entregue_em=now).
Quando todas as paradas estiverem entregues, o caller pode chamar
`concluir(rota)` para fechar.

Por que separar da PermutaService:
  - Permuta é a unidade atômica de troca cheio/vazio (1 pedido por vez).
  - Rota é o agrupamento operacional (N pedidos, 1 dia, 1 veículo).
  Misturar atrapalha — Permuta funciona sem rota (uso MVP), e rota usa
  PermutaService como uma das ações sobre uma parada.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select

from app.models.frota import Entregador, Veiculo
from app.models.logistica import Rota, RotaParada, StatusParada, StatusRota
from app.models.pedidos import Pedido, StatusPedido
from app.services.pedido_service import PedidoService, TransicaoInvalidaError


# ---------------------------------------------------------------------------
# Exceções
# ---------------------------------------------------------------------------


class RotaInvalidaError(ValueError):
    """Dados de rota incoerentes ou transição não permitida."""


# Transições permitidas para Rota
_TRANSICOES: dict[StatusRota, frozenset[StatusRota]] = {
    StatusRota.planejada: frozenset({StatusRota.em_andamento, StatusRota.cancelada}),
    StatusRota.em_andamento: frozenset({StatusRota.concluida, StatusRota.cancelada}),
    StatusRota.concluida: frozenset(),
    StatusRota.cancelada: frozenset(),
}


class RotaService:
    def __init__(self, session, tenant_id: int):
        self.session = session
        self.tenant_id = tenant_id

    # =======================================================================
    # CRIAÇÃO / EDIÇÃO DE CABEÇALHO
    # =======================================================================

    def criar_rota(
        self,
        *,
        data_rota: date,
        veiculo_id: int | None = None,
        entregador_id: int | None = None,
    ) -> Rota:
        """Cria rota em 'planejada'. Veículo/entregador opcionais — a UI
        pode forçar antes de iniciar."""
        if data_rota is None:
            raise RotaInvalidaError("data_rota obrigatória")

        if veiculo_id is not None:
            v = self.session.get(Veiculo, veiculo_id)
            if v is None or v.tenant_id != self.tenant_id:
                raise RotaInvalidaError(
                    f"veículo {veiculo_id} não existe neste tenant"
                )
        if entregador_id is not None:
            e = self.session.get(Entregador, entregador_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise RotaInvalidaError(
                    f"entregador {entregador_id} não existe neste tenant"
                )

        rota = Rota(
            tenant_id=self.tenant_id,
            data_rota=data_rota,
            veiculo_id=veiculo_id,
            entregador_id=entregador_id,
            status=StatusRota.planejada,
        )
        self.session.add(rota)
        self.session.flush()
        return rota

    def editar_cabecalho(
        self,
        rota: Rota,
        *,
        data_rota: date | None = None,
        veiculo_id: int | None = None,
        entregador_id: int | None = None,
    ) -> None:
        """Edita data/veículo/entregador. Permitido apenas em 'planejada'."""
        self._exigir_do_tenant(rota)
        if rota.status != StatusRota.planejada:
            raise RotaInvalidaError(
                f"rota em '{rota.status.value}' — só edite quando 'planejada'"
            )
        if data_rota is not None:
            rota.data_rota = data_rota
        if veiculo_id is not None:
            v = self.session.get(Veiculo, veiculo_id)
            if v is None or v.tenant_id != self.tenant_id:
                raise RotaInvalidaError("veículo não existe neste tenant")
            rota.veiculo_id = veiculo_id
        if entregador_id is not None:
            e = self.session.get(Entregador, entregador_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise RotaInvalidaError("entregador não existe neste tenant")
            rota.entregador_id = entregador_id

    # =======================================================================
    # PARADAS
    # =======================================================================

    def adicionar_parada(
        self,
        rota: Rota,
        *,
        pedido_id: int,
        ordem: int | None = None,
    ) -> RotaParada:
        """Adiciona pedido como parada. Permitido apenas em rota 'planejada'.

        Pedido deve estar em 'aberto' OU 'roteirizado' (não em_entrega/entregue).
        Se ordem=None, vira max(ordem)+1 (final da fila).
        Mesmo pedido não pode ser parada duplicada na mesma rota — checagem.
        """
        self._exigir_do_tenant(rota)
        if rota.status != StatusRota.planejada:
            raise RotaInvalidaError(
                f"rota em '{rota.status.value}' — adicione paradas só na 'planejada'"
            )

        pedido = self.session.get(Pedido, pedido_id)
        if pedido is None or pedido.tenant_id != self.tenant_id:
            raise RotaInvalidaError(f"pedido {pedido_id} não existe neste tenant")
        if pedido.status not in (StatusPedido.aberto, StatusPedido.roteirizado):
            raise RotaInvalidaError(
                f"pedido #{pedido.id} está em '{pedido.status.value}' — só "
                "roteirizamos pedidos 'aberto' ou 'roteirizado'."
            )

        # Não duplicar pedido na mesma rota
        # NO-TENANT-FILTER: filtramos por rota.id que já é do tenant
        existing = self.session.scalar(
            select(RotaParada).where(  # NO-TENANT-FILTER
                RotaParada.rota_id == rota.id,
                RotaParada.pedido_id == pedido_id,
            )
        )
        if existing is not None:
            raise RotaInvalidaError(
                f"pedido #{pedido.id} já é parada desta rota (ordem={existing.ordem})"
            )

        if ordem is None:
            # NO-TENANT-FILTER: filtro por rota.id é mais restritivo.
            # coalesce já trata caso sem paradas (devolve -1).
            ultima = self.session.scalar(
                select(func.coalesce(func.max(RotaParada.ordem), -1))  # NO-TENANT-FILTER
                .where(RotaParada.rota_id == rota.id)
            )
            ordem = int(ultima) + 1

        parada = RotaParada(
            tenant_id=self.tenant_id,
            rota_id=rota.id,
            pedido_id=pedido_id,
            ordem=ordem,
            status=StatusParada.pendente,
        )
        self.session.add(parada)

        # Se pedido ainda está em 'aberto', já transiciona para 'roteirizado'
        if pedido.status == StatusPedido.aberto:
            PedidoService(self.session, self.tenant_id).transicionar(
                pedido, StatusPedido.roteirizado,
            )

        self.session.flush()
        return parada

    def remover_parada(self, parada: RotaParada) -> None:
        """Remove parada de rota 'planejada'. Pedido NÃO volta para 'aberto'
        automaticamente — pode estar em outra rota. Operador decide."""
        self._exigir_do_tenant(parada)
        rota = self.session.get(Rota, parada.rota_id)
        if rota is None:
            raise RotaInvalidaError("rota associada não encontrada")
        if rota.status != StatusRota.planejada:
            raise RotaInvalidaError(
                f"rota em '{rota.status.value}' — só remova paradas quando 'planejada'"
            )
        self.session.delete(parada)
        self.session.flush()

    def reordenar(self, rota: Rota, ordem_por_parada_id: dict[int, int]) -> None:
        """Atualiza ordem de várias paradas de uma vez. `ordem_por_parada_id`
        é {parada_id: nova_ordem}. Só em rota 'planejada'."""
        self._exigir_do_tenant(rota)
        if rota.status != StatusRota.planejada:
            raise RotaInvalidaError(
                f"rota em '{rota.status.value}' — reordene só na 'planejada'"
            )
        # NO-TENANT-FILTER: filtro por rota.id é mais restritivo
        paradas = self.session.scalars(
            select(RotaParada).where(RotaParada.rota_id == rota.id)  # NO-TENANT-FILTER
        ).all()
        by_id = {p.id: p for p in paradas}
        for pid, nova_ordem in ordem_por_parada_id.items():
            p = by_id.get(pid)
            if p is None:
                raise RotaInvalidaError(
                    f"parada {pid} não pertence à rota {rota.id}"
                )
            p.ordem = nova_ordem

    # =======================================================================
    # TRANSIÇÕES
    # =======================================================================

    def iniciar(self, rota: Rota) -> None:
        """planejada → em_andamento. Promove TODOS os pedidos das paradas
        de 'roteirizado' → 'em_entrega' (já em em_entrega passa adiante)."""
        self._exigir_do_tenant(rota)
        self._validar_transicao(rota, StatusRota.em_andamento)

        # NO-TENANT-FILTER: filtro por rota.id é mais restritivo
        paradas = self.session.scalars(
            select(RotaParada).where(RotaParada.rota_id == rota.id)  # NO-TENANT-FILTER
        ).all()
        if not paradas:
            raise RotaInvalidaError("rota sem paradas — adicione pedidos antes")

        ped_svc = PedidoService(self.session, self.tenant_id)
        for p in paradas:
            pedido = self.session.get(Pedido, p.pedido_id)
            if pedido is None:
                continue
            if pedido.status == StatusPedido.roteirizado:
                ped_svc.transicionar(pedido, StatusPedido.em_entrega)

        rota.status = StatusRota.em_andamento

    def concluir(self, rota: Rota) -> None:
        """em_andamento → concluida. Não exige que todas paradas estejam
        entregues — a UI decide se quer permitir concluir com falhas."""
        self._exigir_do_tenant(rota)
        self._validar_transicao(rota, StatusRota.concluida)
        rota.status = StatusRota.concluida

    def cancelar(self, rota: Rota) -> None:
        """planejada/em_andamento → cancelada. Não reverte estado dos
        pedidos — operador decide pedido a pedido."""
        self._exigir_do_tenant(rota)
        self._validar_transicao(rota, StatusRota.cancelada)
        rota.status = StatusRota.cancelada

    # =======================================================================
    # AÇÕES DE PARADA (chamadas APÓS PermutaService.registrar_entrega)
    # =======================================================================

    def marcar_parada_entregue(
        self,
        parada: RotaParada,
        *,
        qtd_entregue: int | None = None,
        qtd_recolhido: int | None = None,
    ) -> None:
        """Marca a parada como entregue + grava timestamp e quantidades.
        NÃO faz a permuta — quem faz é PermutaService chamado antes."""
        self._exigir_do_tenant(parada)
        rota = self.session.get(Rota, parada.rota_id)
        if rota is None or rota.status not in (
            StatusRota.em_andamento, StatusRota.planejada,
        ):
            raise RotaInvalidaError(
                f"rota em '{rota.status.value if rota else '?'}' — não dá "
                "para marcar paradas como entregues"
            )
        parada.status = StatusParada.entregue
        parada.entregue_em = datetime.now()
        if qtd_entregue is not None:
            parada.qtd_entregue = qtd_entregue
        if qtd_recolhido is not None:
            parada.qtd_recolhido = qtd_recolhido

    def marcar_parada_falhou(self, parada: RotaParada) -> None:
        """Cliente ausente, recusa, etc. Diferente de 'entregue': não
        gera permuta. Pedido continua em 'em_entrega' — operador decide."""
        self._exigir_do_tenant(parada)
        parada.status = StatusParada.falhou
        parada.entregue_em = datetime.now()

    # =======================================================================
    # Helpers internos
    # =======================================================================

    def _exigir_do_tenant(self, obj) -> None:
        if getattr(obj, "tenant_id", None) != self.tenant_id:
            raise PermissionError(
                f"{type(obj).__name__} id={getattr(obj, 'id', '?')} "
                f"pertence ao tenant {obj.tenant_id}; service no {self.tenant_id}"
            )

    def _validar_transicao(self, rota: Rota, novo: StatusRota) -> None:
        permitidos = _TRANSICOES.get(rota.status, frozenset())
        if novo not in permitidos:
            raise RotaInvalidaError(
                f"transição inválida: {rota.status.value} → {novo.value}. "
                f"Permitidos: {[s.value for s in permitidos] or '(nenhum)'}"
            )
