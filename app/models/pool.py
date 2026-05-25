"""Models do POOL de garrafões — coração do sistema.

Quatro entidades:
- TipoGarrafao    : catálogo de tipos (material + capacidade + custo de reposição)
- LocalEstoque    : locais lógicos do pool (CD, veículo, indústria, cliente, descarte)
- GarrafaoSaldo   : estado ATUAL agregado (tipo, local, estado, faixa de validade)
- GarrafaoMovimento : livro-razão IMUTÁVEL de todas as alterações

Invariantes que serão materializadas no PoolService (passo 10):
  1. Saldo é sempre reconstruível a partir dos movimentos.
  2. Apenas movimentos 'compra' (+) e 'descarte' (-) alteram o TAMANHO do pool.
     Envase, transferencia, permuta e ajuste devem ser delta_pool=0.
  3. quantidade em garrafao_saldos >= 0 — viola? rollback.
  4. SELECT ... FOR UPDATE em saldos durante movimentos para evitar race.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, DECIMAL, INTEGER, TINYINT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.extensions import db
from app.models.base import TenantMixin, enum_col


# ---------------------------------------------------------------------------
# ENUMs
# ---------------------------------------------------------------------------


class MaterialGarrafao(str, enum.Enum):
    PC = "PC"     # policarbonato — o mais tradicional, maior vida útil
    PP = "PP"     # polipropileno — alternativa retornável
    PET = "PET"   # PET retornável — vida útil menor


class TipoLocal(str, enum.Enum):
    cd = "cd"                # centro de distribuição (depósito)
    veiculo = "veiculo"      # 1 local por veículo (vincula via veiculo_id)
    industria = "industria"  # indústria onde rola o envase
    cliente = "cliente"      # local agregado de clientes (se for usado)
    descarte = "descarte"    # destino final dos vasilhames perdidos


class EstadoGarrafao(str, enum.Enum):
    cheio = "cheio"
    vazio = "vazio"
    avariado = "avariado"


class TipoMovimento(str, enum.Enum):
    """Tipos de movimento no livro-razão. Cada um tem efeito específico
    sobre saldos — codificado no PoolService (passo 10)."""

    envase = "envase"                  # industrialização: vazio→cheio MESMO VASILHAME, MESMA VALIDADE
                                        # (validade rastreada é a do vasilhame, não a da água)
    compra = "compra"                  # vasilhame NOVO entrando no pool (+ tamanho)
    permuta = "permuta"                # troca cheio↔vazio na entrega ao cliente
    transferencia = "transferencia"    # deslocamento entre locais (não muda estado)
    avaria = "avaria"                  # cheio/vazio → avariado
    descarte = "descarte"              # saída definitiva do pool (- tamanho)
    ajuste = "ajuste"                  # correção de inventário (manual, admin)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TipoGarrafao(TenantMixin, db.Model):
    """Catálogo de tipos de garrafão do distribuidor.

    Material e capacidade vivem AQUI; validade vive em GarrafaoSaldo (porque
    um mesmo tipo tem vasilhames de várias idades misturados no pool).
    """

    __tablename__ = "tipos_garrafao"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(80), nullable=False)
    material: Mapped[MaterialGarrafao] = mapped_column(enum_col(MaterialGarrafao), nullable=False)
    capacidade_litros: Mapped[Decimal] = mapped_column(
        DECIMAL(5, 2), nullable=False, default=Decimal("20.00"), server_default="20.00"
    )
    # Custo de comprar 1 vasilhame novo para repor o pool — KPI financeiro central
    valor_reposicao: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2), nullable=True)
    ativo: Mapped[bool] = mapped_column(TINYINT(1), nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("tenant_id", "nome", name="uq_tipogar"),
        Index("idx_tipogar_tenant", "tenant_id"),
    )

    def __repr__(self) -> str:
        return f"<TipoGarrafao {self.id} {self.nome!r} {self.material.value}>"


class LocalEstoque(TenantMixin, db.Model):
    """Locais lógicos onde garrafões do pool podem estar.

    Tipos:
        cd        — depósito central
        veiculo   — 1 local por veículo (veiculo_id obrigatório nesse caso)
        industria — indústria onde rola envase (garrafões continuam do distribuidor)
        cliente   — destino abstrato (uso opcional, ver doc do PoolService)
        descarte  — destino final pós-descarte
    """

    __tablename__ = "locais_estoque"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tipo: Mapped[TipoLocal] = mapped_column(enum_col(TipoLocal), nullable=False)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    veiculo_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("veiculos.id"), nullable=True
    )

    __table_args__ = (Index("idx_local_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return f"<LocalEstoque {self.id} {self.tipo.value} {self.nome!r}>"


class GarrafaoSaldo(TenantMixin, db.Model):
    """Saldo AGREGADO por (tipo_garrafao, local, estado, faixa de validade).

    O UNIQUE composto é fundamental — garante que existe NO MÁXIMO uma
    linha por combinação. O PoolService faz upsert via
        INSERT ... ON DUPLICATE KEY UPDATE quantidade = quantidade + ?
    e protege a leitura/escrita com SELECT ... FOR UPDATE.

    ⚠ NOTA SOBRE NULL: MySQL trata NULL como SEMPRE DISTINTO em UNIQUE.
    Por convenção do PoolService, `validade` é NOT NULL em saldos — se
    o lote não tem validade declarada, usa sentinela (ver PoolService).
    """

    __tablename__ = "garrafao_saldos"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tipo_garrafao_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tipos_garrafao.id"), nullable=False
    )
    local_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("locais_estoque.id"), nullable=False
    )
    estado: Mapped[EstadoGarrafao] = mapped_column(enum_col(EstadoGarrafao), nullable=False)
    validade: Mapped[date | None] = mapped_column(Date, nullable=True)
    quantidade: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "tipo_garrafao_id", "local_id", "estado", "validade",
            name="uq_saldo",
        ),
        Index("idx_saldo_tenant", "tenant_id"),
        Index("idx_saldo_validade", "tenant_id", "validade"),  # envelhecimento + FEFO
    )

    def __repr__(self) -> str:
        return (
            f"<GarrafaoSaldo tipo={self.tipo_garrafao_id} local={self.local_id} "
            f"{self.estado.value} val={self.validade} qtd={self.quantidade}>"
        )


class GarrafaoMovimento(TenantMixin, db.Model):
    """Livro-razão IMUTÁVEL de todos os movimentos de garrafão.

    Fonte da verdade. Cada linha registra um delta aplicado a um (ou dois)
    saldos. `referencia_tipo`/`referencia_id` ligam ao evento de origem
    (ex.: 'pedido' + pedido.id; 'rota' + rota.id).

    NÃO há FK para locais_estoque em local_origem_id/local_destino_id —
    schema não declara, e queremos flexibilidade (locais podem ser
    deletados/renomeados sem invalidar histórico).
    """

    __tablename__ = "garrafao_movimentos"

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tipo_garrafao_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), ForeignKey("tipos_garrafao.id"), nullable=False
    )
    tipo: Mapped[TipoMovimento] = mapped_column(enum_col(TipoMovimento), nullable=False)
    local_origem_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    local_destino_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    estado: Mapped[EstadoGarrafao] = mapped_column(enum_col(EstadoGarrafao), nullable=False)
    validade: Mapped[date | None] = mapped_column(Date, nullable=True)
    quantidade: Mapped[int] = mapped_column(INTEGER, nullable=False)
    referencia_tipo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    referencia_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    usuario_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), nullable=True)
    observacao: Mapped[str | None] = mapped_column(String(255), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("idx_mov_tenant", "tenant_id", "criado_em"),
        Index("idx_mov_tipogar", "tenant_id", "tipo_garrafao_id"),
        # idx_mov_tipo é fundamental para isolar custo de reposição (descarte+avaria)
        # do custo de água (envase) sem fazer table scan.
        Index("idx_mov_tipo", "tenant_id", "tipo"),
    )

    def __repr__(self) -> str:
        return (
            f"<GarrafaoMovimento {self.id} {self.tipo.value} tipo_gar={self.tipo_garrafao_id} "
            f"qtd={self.quantidade} {self.estado.value} val={self.validade}>"
        )
