"""Filtros Jinja customizados — registrados via register_filters().

Mantém formatação consistente em todos os templates sem repetir lógica de
locale/format em cada `{{ ... }}`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from flask import Flask


def brl(value: Any) -> str:
    """Formata Decimal/float/int como moeda BR: 'R$ 1.234,56'.

    None → '—'. Negativos preservam o sinal: 'R$ -1.234,56'.
    """
    if value is None:
        return "—"
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    # Formata com sep US (vírgula milhar, ponto decimal) e troca os símbolos
    # — evita dependência do locale do SO (que varia entre Win/Linux).
    formatted = f"{d:,.2f}"  # ex: "1,234.56"
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def mes_ano(value: date | datetime | None) -> str:
    """Formata uma data como 'MM/YYYY'. None → '—'."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%m/%Y")


def register_filters(app: Flask) -> None:
    """Registra filtros Jinja na app."""
    app.jinja_env.filters["brl"] = brl
    app.jinja_env.filters["mes_ano"] = mes_ano
