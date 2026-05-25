"""PoolService — coração transacional do sistema.

Materializa as 4 invariantes do pool em código:

  (1) garrafao_movimentos é livro-razão imutável; saldos devem ser
      sempre reconstruíveis a partir dele (via `reconstruir_saldos`).
  (2) Apenas movimentos 'compra' (+) e 'descarte' (-) alteram o
      TAMANHO do pool. Os demais (envase, transferencia, permuta,
      avaria) devem ter `sum(deltas) == 0`. Violação levanta
      InvariantePoolViolada — é bug.
  (3) `garrafao_saldos.quantidade >= 0`. Violação durante uma operação
      levanta EstoqueInsuficienteError + rollback.
  (4) Toda leitura/escrita em saldos durante um movimento usa
      SELECT ... FOR UPDATE (gap lock no InnoDB), prevenindo race
      entre entregas/permutas simultâneas.

API pública:
  Operações de alto nível (1 chamada = 1 movimento conceitual):
    - registrar_compra(...)
    - registrar_descarte(...)
    - registrar_transferencia(...)
    - registrar_avaria(...)        # gera 2 deltas (cheio→avariado)
    - registrar_ajuste(...)        # correção manual

  Primitiva (chamada por EnvaseService e PermutaService nos passos 12/15):
    - aplicar_deltas(tipo, tipo_garrafao_id, deltas, ...)

  Auditoria:
    - reconstruir_saldos(dry_run=True) → lista de SaldoDivergencia
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from sqlalchemy import select

from app.extensions import db
from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    GarrafaoSaldo,
    TipoMovimento,
)


# ---------------------------------------------------------------------------
# Tipos auxiliares e exceções
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delta:
    """Efeito sobre UM saldo: adiciona/subtrai `quantidade` em um
    (local, estado, validade) específico.

    `quantidade` é sempre POSITIVA aqui — o sinal vem de `sinal`.
    """

    local_id: int
    estado: EstadoGarrafao
    validade: date
    quantidade: int
    sinal: int  # +1 (entrada) ou -1 (saída)

    def __post_init__(self):
        if self.sinal not in (-1, 1):
            raise ValueError(f"sinal deve ser +1 ou -1, got {self.sinal}")
        if self.quantidade <= 0:
            raise ValueError(f"quantidade deve ser > 0, got {self.quantidade}")
        if self.validade is None:
            # Validade NULL em saldos quebra o UNIQUE (NULL != NULL no MySQL).
            # Lotes sem validade declarada devem usar sentinela (ver docs).
            raise ValueError("validade não pode ser None em delta de saldo")


@dataclass(frozen=True)
class SaldoDivergencia:
    tipo_garrafao_id: int
    local_id: int
    estado: EstadoGarrafao
    validade: date
    esperado: int    # do livro-razão (reconstruído)
    real: int        # da tabela garrafao_saldos


class EstoqueInsuficienteError(RuntimeError):
    """Movimento tentou subtrair mais garrafões do que existem no saldo."""


class InvariantePoolViolada(RuntimeError):
    """Movimento que não deveria alterar o tamanho do pool gerou
    soma de deltas != 0. É bug de programação, não erro operacional."""


# Tipos de movimento que NÃO podem alterar o tamanho do pool —
# apenas movem/transformam vasilhames já existentes.
_TIPOS_DELTA_ZERO = frozenset({
    TipoMovimento.envase,
    TipoMovimento.transferencia,
    TipoMovimento.permuta,
    TipoMovimento.avaria,
})


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------


class PoolService:
    def __init__(self, session, tenant_id: int, usuario_id: int | None = None):
        self.session = session
        self.tenant_id = tenant_id
        self.usuario_id = usuario_id

    # =======================================================================
    # OPERAÇÕES DE ALTO NÍVEL
    # =======================================================================

    def registrar_compra(
        self,
        *,
        tipo_garrafao_id: int,
        quantidade: int,
        local_destino_id: int,
        validade: date,
        estado: EstadoGarrafao = EstadoGarrafao.vazio,
        referencia_tipo: str | None = None,
        referencia_id: int | None = None,
        observacao: str | None = None,
    ) -> list[GarrafaoMovimento]:
        """Vasilhames NOVOS entrando no pool. Aumenta o tamanho do pool em `quantidade`.

        Por padrão entram como 'vazio'; raro fornecedor entregar cheio.
        """
        delta = Delta(
            local_id=local_destino_id, estado=estado,
            validade=validade, quantidade=quantidade, sinal=+1,
        )
        return self.aplicar_deltas(
            tipo=TipoMovimento.compra,
            tipo_garrafao_id=tipo_garrafao_id,
            deltas=[delta],
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            observacao=observacao,
        )

    def registrar_descarte(
        self,
        *,
        tipo_garrafao_id: int,
        quantidade: int,
        local_origem_id: int,
        estado: EstadoGarrafao,
        validade: date,
        referencia_tipo: str | None = None,
        referencia_id: int | None = None,
        observacao: str | None = None,
    ) -> list[GarrafaoMovimento]:
        """Saída DEFINITIVA do pool. Reduz tamanho em `quantidade`."""
        delta = Delta(
            local_id=local_origem_id, estado=estado,
            validade=validade, quantidade=quantidade, sinal=-1,
        )
        return self.aplicar_deltas(
            tipo=TipoMovimento.descarte,
            tipo_garrafao_id=tipo_garrafao_id,
            deltas=[delta],
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            observacao=observacao,
        )

    def registrar_transferencia(
        self,
        *,
        tipo_garrafao_id: int,
        quantidade: int,
        local_origem_id: int,
        local_destino_id: int,
        estado: EstadoGarrafao,
        validade: date,
        referencia_tipo: str | None = None,
        referencia_id: int | None = None,
        observacao: str | None = None,
    ) -> list[GarrafaoMovimento]:
        """Move garrafões entre locais, mesmo estado e validade."""
        if local_origem_id == local_destino_id:
            raise ValueError("transferência entre o mesmo local não faz sentido")
        deltas = [
            Delta(local_id=local_origem_id, estado=estado, validade=validade,
                  quantidade=quantidade, sinal=-1),
            Delta(local_id=local_destino_id, estado=estado, validade=validade,
                  quantidade=quantidade, sinal=+1),
        ]
        return self.aplicar_deltas(
            tipo=TipoMovimento.transferencia,
            tipo_garrafao_id=tipo_garrafao_id,
            deltas=deltas,
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            observacao=observacao,
        )

    def registrar_avaria(
        self,
        *,
        tipo_garrafao_id: int,
        quantidade: int,
        local_id: int,
        estado_origem: EstadoGarrafao,  # geralmente cheio ou vazio
        validade: date,
        referencia_tipo: str | None = None,
        referencia_id: int | None = None,
        observacao: str | None = None,
    ) -> list[GarrafaoMovimento]:
        """Garrafões cheios/vazios viram 'avariado' no mesmo local."""
        if estado_origem == EstadoGarrafao.avariado:
            raise ValueError("avaria a partir de 'avariado' não faz sentido")
        deltas = [
            Delta(local_id=local_id, estado=estado_origem, validade=validade,
                  quantidade=quantidade, sinal=-1),
            Delta(local_id=local_id, estado=EstadoGarrafao.avariado, validade=validade,
                  quantidade=quantidade, sinal=+1),
        ]
        return self.aplicar_deltas(
            tipo=TipoMovimento.avaria,
            tipo_garrafao_id=tipo_garrafao_id,
            deltas=deltas,
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            observacao=observacao,
        )

    def registrar_ajuste(
        self,
        *,
        tipo_garrafao_id: int,
        quantidade: int,
        local_id: int,
        estado: EstadoGarrafao,
        validade: date,
        sinal: int,
        observacao: str,  # obrigatório em ajuste — justificativa
    ) -> list[GarrafaoMovimento]:
        """Correção manual de inventário. Requer justificativa."""
        if not observacao or not observacao.strip():
            raise ValueError("ajuste requer observacao não-vazia (justificativa)")
        delta = Delta(local_id=local_id, estado=estado, validade=validade,
                      quantidade=quantidade, sinal=sinal)
        return self.aplicar_deltas(
            tipo=TipoMovimento.ajuste,
            tipo_garrafao_id=tipo_garrafao_id,
            deltas=[delta],
            observacao=observacao,
        )

    # =======================================================================
    # PRIMITIVA — chamada pelos wrappers acima e pelos services dos próximos passos
    # =======================================================================

    def aplicar_deltas(
        self,
        *,
        tipo: TipoMovimento,
        tipo_garrafao_id: int,
        deltas: list[Delta],
        referencia_tipo: str | None = None,
        referencia_id: int | None = None,
        observacao: str | None = None,
    ) -> list[GarrafaoMovimento]:
        """Aplica uma lista de deltas atomicamente, gera movimento(s) no
        livro-razão. Tudo em UMA transação — caller faz commit depois.

        Para tipos em `_TIPOS_DELTA_ZERO`, valida que `sum(sinal*qtd) == 0`.
        """
        if not deltas:
            raise ValueError("deltas vazio")

        # ---- Invariante do pool (antes de qualquer escrita) ----
        delta_total = sum(d.sinal * d.quantidade for d in deltas)
        if tipo in _TIPOS_DELTA_ZERO and delta_total != 0:
            raise InvariantePoolViolada(
                f"Movimento {tipo.value} deve ter delta_total=0; obteve {delta_total}"
            )
        if tipo == TipoMovimento.compra and delta_total <= 0:
            raise InvariantePoolViolada("compra precisa de delta_total > 0")
        if tipo == TipoMovimento.descarte and delta_total >= 0:
            raise InvariantePoolViolada("descarte precisa de delta_total < 0")

        # ---- Aplica cada delta com FOR UPDATE ----
        for d in deltas:
            self._aplicar_delta(tipo_garrafao_id, d)

        # ---- Gera movimento(s) no livro-razão ----
        # Caso especial: 2 deltas com mesma (estado, validade), sinais opostos,
        # locais diferentes → 1 movimento de transferência clássico.
        if (
            len(deltas) == 2
            and deltas[0].estado == deltas[1].estado
            and deltas[0].validade == deltas[1].validade
            and deltas[0].sinal == -deltas[1].sinal
            and deltas[0].local_id != deltas[1].local_id
        ):
            origem, destino = (
                (deltas[0], deltas[1]) if deltas[0].sinal < 0 else (deltas[1], deltas[0])
            )
            mov = self._novo_movimento(
                tipo, tipo_garrafao_id,
                local_origem_id=origem.local_id, local_destino_id=destino.local_id,
                estado=origem.estado, validade=origem.validade, quantidade=origem.quantidade,
                referencia_tipo=referencia_tipo, referencia_id=referencia_id,
                observacao=observacao,
            )
            return [mov]

        # Caso geral: 1 movimento por delta.
        movs = []
        for d in deltas:
            mov = self._novo_movimento(
                tipo, tipo_garrafao_id,
                local_origem_id=d.local_id if d.sinal < 0 else None,
                local_destino_id=d.local_id if d.sinal > 0 else None,
                estado=d.estado, validade=d.validade, quantidade=d.quantidade,
                referencia_tipo=referencia_tipo, referencia_id=referencia_id,
                observacao=observacao,
            )
            movs.append(mov)
        return movs

    # =======================================================================
    # AUDITORIA — reconstrói saldos a partir de movimentos
    # =======================================================================

    def reconstruir_saldos(self, *, dry_run: bool = True) -> list[SaldoDivergencia]:
        """Recalcula saldos somando todos os movimentos do tenant e compara
        com a tabela garrafao_saldos. Retorna divergências.

        Se `dry_run=False`, ZERA todos os saldos do tenant e regrava com
        os valores reconstruídos. Use só em correção pós-incidente.
        """
        # ---- Reconstrói via livro-razão ----
        movs_stmt = (
            select(GarrafaoMovimento)
            .where(GarrafaoMovimento.tenant_id == self.tenant_id)
            .order_by(GarrafaoMovimento.criado_em, GarrafaoMovimento.id)
        )
        reconstruido: dict[tuple, int] = {}
        for m in self.session.scalars(movs_stmt):
            if m.local_origem_id is not None:
                k = (m.tipo_garrafao_id, m.local_origem_id, m.estado, m.validade)
                reconstruido[k] = reconstruido.get(k, 0) - m.quantidade
            if m.local_destino_id is not None:
                k = (m.tipo_garrafao_id, m.local_destino_id, m.estado, m.validade)
                reconstruido[k] = reconstruido.get(k, 0) + m.quantidade

        # ---- Lê saldos reais ----
        saldos_stmt = select(GarrafaoSaldo).where(GarrafaoSaldo.tenant_id == self.tenant_id)
        reais: dict[tuple, int] = {}
        for s in self.session.scalars(saldos_stmt):
            reais[(s.tipo_garrafao_id, s.local_id, s.estado, s.validade)] = s.quantidade

        # ---- Compara ----
        chaves = set(reconstruido) | set(reais)
        divergencias: list[SaldoDivergencia] = []
        for k in chaves:
            esp = reconstruido.get(k, 0)
            rea = reais.get(k, 0)
            if esp != rea:
                divergencias.append(SaldoDivergencia(
                    tipo_garrafao_id=k[0], local_id=k[1], estado=k[2],
                    validade=k[3], esperado=esp, real=rea,
                ))

        if not dry_run and divergencias:
            self._regravar_saldos(reconstruido)

        return divergencias

    # =======================================================================
    # Helpers internos
    # =======================================================================

    def _aplicar_delta(self, tipo_garrafao_id: int, delta: Delta) -> None:
        """Aplica UM delta a um saldo com SELECT ... FOR UPDATE."""
        stmt = (
            select(GarrafaoSaldo)
            .where(
                GarrafaoSaldo.tenant_id == self.tenant_id,
                GarrafaoSaldo.tipo_garrafao_id == tipo_garrafao_id,
                GarrafaoSaldo.local_id == delta.local_id,
                GarrafaoSaldo.estado == delta.estado,
                GarrafaoSaldo.validade == delta.validade,
            )
            .with_for_update()
        )
        saldo = self.session.scalar(stmt)
        delta_qtd = delta.sinal * delta.quantidade

        if saldo is None:
            if delta_qtd < 0:
                raise EstoqueInsuficienteError(
                    f"sem saldo em tipo={tipo_garrafao_id} local={delta.local_id} "
                    f"{delta.estado.value} val={delta.validade} (tentou tirar {delta.quantidade})"
                )
            saldo = GarrafaoSaldo(
                tenant_id=self.tenant_id,
                tipo_garrafao_id=tipo_garrafao_id,
                local_id=delta.local_id,
                estado=delta.estado,
                validade=delta.validade,
                quantidade=delta_qtd,
            )
            self.session.add(saldo)
            self.session.flush()  # garante visibilidade na mesma transação
        else:
            nova = saldo.quantidade + delta_qtd
            if nova < 0:
                raise EstoqueInsuficienteError(
                    f"saldo insuficiente: tipo={tipo_garrafao_id} local={delta.local_id} "
                    f"{delta.estado.value} val={delta.validade}: "
                    f"saldo={saldo.quantidade}, tentou tirar {delta.quantidade}"
                )
            saldo.quantidade = nova

    def _novo_movimento(
        self,
        tipo: TipoMovimento,
        tipo_garrafao_id: int,
        *,
        local_origem_id: int | None,
        local_destino_id: int | None,
        estado: EstadoGarrafao,
        validade: date,
        quantidade: int,
        referencia_tipo: str | None,
        referencia_id: int | None,
        observacao: str | None,
    ) -> GarrafaoMovimento:
        mov = GarrafaoMovimento(
            tenant_id=self.tenant_id,
            tipo_garrafao_id=tipo_garrafao_id,
            tipo=tipo,
            local_origem_id=local_origem_id,
            local_destino_id=local_destino_id,
            estado=estado,
            validade=validade,
            quantidade=quantidade,
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            usuario_id=self.usuario_id,
            observacao=observacao,
        )
        self.session.add(mov)
        return mov

    def _regravar_saldos(self, reconstruido: dict[tuple, int]) -> None:
        """Usado apenas pelo `reconstruir_saldos(dry_run=False)`."""
        # Apaga saldos do tenant; recria do dict reconstruído.
        from sqlalchemy import delete
        self.session.execute(
            delete(GarrafaoSaldo).where(GarrafaoSaldo.tenant_id == self.tenant_id)
        )
        for (tipo_garrafao_id, local_id, estado, validade), qtd in reconstruido.items():
            if qtd == 0:
                continue  # não vale a pena materializar saldos zerados
            self.session.add(GarrafaoSaldo(
                tenant_id=self.tenant_id,
                tipo_garrafao_id=tipo_garrafao_id,
                local_id=local_id,
                estado=estado,
                validade=validade,
                quantidade=qtd,
            ))
