"""EnvaseService — orquestra o ciclo de industrialização.

O distribuidor leva seus VAZIOS para a indústria; a indústria lava e
enche os MESMOS vasilhames; o distribuidor recebe os CHEIOS de volta.
A validade rastreada é a do **vasilhame** (estampada no fundo, ~3 anos),
NÃO da água — portanto **não muda** com o envase.

Sob o capô, delega ao PoolService que faz a atomicidade e valida a
invariante do pool (delta_total == 0 para envase).

NÃO confundir: a validade da água (30-60 dias pós-envase) é controle
INTERNO da indústria; o distribuidor não rastreia.
"""

from __future__ import annotations

from datetime import date

from app.models.pool import (
    EstadoGarrafao,
    GarrafaoMovimento,
    LocalEstoque,
    TipoLocal,
    TipoMovimento,
)
from app.services.pool_service import Delta, PoolService


class EnvaseService:
    def __init__(self, session, tenant_id: int, usuario_id: int | None = None):
        self.session = session
        self.tenant_id = tenant_id
        self.usuario_id = usuario_id
        self.pool = PoolService(session, tenant_id, usuario_id)

    def registrar_envase(
        self,
        *,
        tipo_garrafao_id: int,
        quantidade: int,
        local_industria_id: int,
        validade: date,
        referencia_tipo: str | None = None,
        referencia_id: int | None = None,
        observacao: str | None = None,
    ) -> list[GarrafaoMovimento]:
        """Consome `quantidade` vazios e produz `quantidade` cheios no
        mesmo local indústria, com a MESMA validade.

        O PoolService valida:
          - delta_total == 0 (invariante do pool: envase é neutro no tamanho)
          - quantidade >= 0 nos saldos resultantes (com SELECT FOR UPDATE)

        Gera 2 movimentos `tipo=envase` no livro-razão.

        TODO Fase 2: gerar Lancamento financeiro a pagar (água + serviço)
        quando os models de Financeiro existirem (passo 15).
        """
        local = self.session.get(LocalEstoque, local_industria_id)
        if local is None or local.tenant_id != self.tenant_id:
            raise ValueError(
                f"Local id={local_industria_id} não existe neste tenant"
            )
        if local.tipo != TipoLocal.industria:
            raise ValueError(
                f"Envase só pode ser realizado em local de tipo 'industria'; "
                f"local id={local_industria_id} é '{local.tipo.value}'."
            )

        return self.pool.aplicar_deltas(
            tipo=TipoMovimento.envase,
            tipo_garrafao_id=tipo_garrafao_id,
            deltas=[
                Delta(
                    local_id=local_industria_id, estado=EstadoGarrafao.vazio,
                    validade=validade, quantidade=quantidade, sinal=-1,
                ),
                Delta(
                    local_id=local_industria_id, estado=EstadoGarrafao.cheio,
                    validade=validade, quantidade=quantidade, sinal=+1,
                ),
            ],
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            observacao=observacao,
        )
