"""Campos WTForms customizados — moeda BR.

WTForms `DecimalField` espera ponto decimal (locale C). Brasileiro digita
vírgula. Esse campo aceita ambos via app.money.parse_money.
"""

from __future__ import annotations

from wtforms import DecimalField

from app.money import parse_money


class BrlMoneyField(DecimalField):
    """DecimalField que aceita '6,00', '1.234,56', '6.00', '6' etc.

    Renderiza com atributos `data-money` + `inputmode=decimal` por padrão,
    que ativa a máscara JS em `static/js/money-mask.js` (formata como
    '1.234,56' no blur).
    """

    def __init__(self, *args, render_kw=None, **kwargs):
        # Mescla render_kw passado pelo usuário com defaults (sem sobrescrever)
        defaults = {"data-money": "", "inputmode": "decimal"}
        if render_kw:
            defaults.update(render_kw)
        super().__init__(*args, render_kw=defaults, **kwargs)

    def process_formdata(self, valuelist):
        if not valuelist:
            self.data = None
            return
        raw = valuelist[0]
        try:
            self.data = parse_money(raw)
        except ValueError as exc:
            self.data = None
            raise ValueError(str(exc)) from exc
