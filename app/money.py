"""Parsing de valor monetário — aceita formatos BR e US.

Centraliza pra evitar duplicação entre blueprints (pedidos, financeiro).
Robusto a entradas humanas: '6', '6,00', '1.234,56', '1234.56', 'R$ 1.234,56'.

NÃO trata como erro:
  - String vazia ou None → devolve None
  - Espaços ao redor

Trata como erro (ValueError):
  - Formato bagunçado (letras no meio, múltiplos separadores incoerentes)
  - Valor negativo é AVALIADO normalmente — quem chama decide se rejeita
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_money(raw: str | int | float | Decimal | None) -> Decimal | None:
    """Converte input do usuário em Decimal.

    Suportado:
      None / ''           → None
      6                   → Decimal('6')
      6.5                 → Decimal('6.5')
      Decimal('6.5')      → Decimal('6.5') (passa intacto)
      '6'                 → Decimal('6')
      '6,00'              → Decimal('6.00')   (BR sem milhar)
      '6.00'              → Decimal('6.00')   (US sem milhar)
      '1.234,56'          → Decimal('1234.56') (BR com milhar)
      '1,234.56'          → Decimal('1234.56') (US com milhar)
      '1234,56'           → Decimal('1234.56')
      ' R$ 1.234,56 '     → Decimal('1234.56')
      '-1234,56'          → Decimal('-1234.56')

    Levanta ValueError pra entradas malformadas.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        # Decimal direto de float perde precisão — vai via str
        return Decimal(str(raw))

    s = str(raw).strip()
    if not s:
        return None

    # Remove prefixo R$ e espaços internos
    s = s.replace("R$", "").replace(" ", "").strip()
    if not s:
        return None

    # Casos com AMBOS ',' e '.': o último é separator decimal, o outro é
    # separador de milhar (que removemos). Ex: '1.234,56' → '1234.56'.
    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        # Identifica qual vem por último
        if s.rfind(",") > s.rfind("."):
            # BR: vírgula é decimal, ponto é milhar
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: ponto é decimal, vírgula é milhar
            s = s.replace(",", "")
    elif has_comma:
        # Só vírgula → trata como decimal BR
        s = s.replace(",", ".")
    # Se só tem ponto, deixa (já é formato Decimal-friendly)

    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"valor inválido: {raw!r}") from exc
