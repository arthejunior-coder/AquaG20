"""Regras de validade de garrafão — domínio compartilhado.

Garrafão tem ~3 anos de vida útil (estampado no fundo). Logo:
  - Pedido NÃO pode pedir validade no passado (não existe garrafão vencido).
  - Pedido NÃO pode pedir validade > 36 meses no futuro (não existe).

Permuta segue mesma regra: o que entra E o que sai precisa estar em janela
de validade aceita.

Granularidade: mês/ano. Sempre que recebemos data, normalizamos pro dia 1
do mês (sistema rastreia faixa de validade, não data exata estampada).
"""

from __future__ import annotations

from datetime import date


# Garrafão tem 3 anos de validade da fabricação. 36 meses é generoso (vasilhame
# novo recém-saído da fábrica). Se mudar, ajustar testes.
GARRAFAO_VALIDADE_MAX_MESES = 36


# Override de "hoje" pra testes determinísticos. Em prod fica None → date.today().
# Testes podem setar via conftest autouse pra fixar a janela de validade.
_HOJE_OVERRIDE: date | None = None


def _hoje() -> date:
    return _HOJE_OVERRIDE if _HOJE_OVERRIDE is not None else date.today()


def _add_meses(d: date, meses: int) -> date:
    """Soma N meses a uma data, ajustando overflow de mês.

    `_add_meses(2026-05-15, 36) == 2029-05-15`.
    """
    novo_mes = d.month + meses
    novo_ano = d.year + (novo_mes - 1) // 12
    novo_mes = ((novo_mes - 1) % 12) + 1
    return date(novo_ano, novo_mes, 1)  # sempre dia 1 — só interessa o mês


def primeiro_dia_do_mes(d: date) -> date:
    """Trunca pra dia 1 do mês — granularidade do nosso sistema."""
    return date(d.year, d.month, 1)


def validar_validade_pedida(
    validade: date,
    *,
    hoje: date | None = None,
) -> None:
    """Levanta ValueError se a validade está fora da janela aceita.

    Janela: [primeiro dia do mês atual, mês atual + 36 meses].

    Por que `hoje` injetável: testes determinísticos. Default = `_hoje()`
    (que respeita `_HOJE_OVERRIDE` settado por conftest em test, ou
    `date.today()` em prod).
    """
    hoje = hoje or _hoje()
    minimo = primeiro_dia_do_mes(hoje)
    maximo = _add_meses(minimo, GARRAFAO_VALIDADE_MAX_MESES)

    val_mes = primeiro_dia_do_mes(validade)
    if val_mes < minimo:
        raise ValueError(
            f"validade {val_mes.strftime('%m/%Y')} está no passado — "
            f"garrafão vencido não pode ser usado em pedido (mín: {minimo.strftime('%m/%Y')})"
        )
    if val_mes > maximo:
        raise ValueError(
            f"validade {val_mes.strftime('%m/%Y')} excede {GARRAFAO_VALIDADE_MAX_MESES} "
            f"meses no futuro — garrafão tem ~3 anos de vida útil "
            f"(máx: {maximo.strftime('%m/%Y')})"
        )


def parse_mes_ano_input(raw: str | None) -> date | None:
    """Converte input HTML `<input type="month">` ('YYYY-MM') em date(YYYY, MM, 1).

    Retorna None se raw vazio/None. Levanta ValueError se formato inválido.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        ano, mes = raw.split("-")
        return date(int(ano), int(mes), 1)
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"validade deve estar em formato YYYY-MM, got {raw!r}"
        ) from exc
