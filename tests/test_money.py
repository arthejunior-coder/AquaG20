"""Testes do app/money.py — parse_money aceita BR e US.

Cobre todas as variações de input humano (vírgula, ponto, milhar, prefixo R$,
espaços) sem perder precisão.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.money import parse_money


class TestNoneEVazio:
    def test_none_devolve_none(self):
        assert parse_money(None) is None

    def test_string_vazia(self):
        assert parse_money("") is None

    def test_so_espacos(self):
        assert parse_money("   ") is None


class TestPassthrough:
    def test_decimal_passa_intacto(self):
        d = Decimal("123.45")
        assert parse_money(d) is d  # mesmo objeto

    def test_int_aceito(self):
        assert parse_money(6) == Decimal("6")

    def test_float_via_str(self):
        # Decimal direto de float perde precisão; parse_money vai via str
        assert parse_money(6.5) == Decimal("6.5")


class TestStringFormatosBR:
    def test_inteiro_simples(self):
        assert parse_money("6") == Decimal("6")

    def test_decimal_virgula(self):
        assert parse_money("6,00") == Decimal("6.00")

    def test_com_milhar(self):
        assert parse_money("1.234,56") == Decimal("1234.56")

    def test_milhar_grande(self):
        assert parse_money("1.234.567,89") == Decimal("1234567.89")

    def test_sem_milhar_decimal(self):
        assert parse_money("1234,56") == Decimal("1234.56")


class TestStringFormatosUS:
    def test_decimal_ponto(self):
        assert parse_money("6.00") == Decimal("6.00")

    def test_com_milhar(self):
        assert parse_money("1,234.56") == Decimal("1234.56")


class TestEdgeCases:
    def test_prefixo_rs_removido(self):
        assert parse_money("R$ 1.234,56") == Decimal("1234.56")

    def test_espacos_internos(self):
        assert parse_money("R$  1.234,56  ") == Decimal("1234.56")

    def test_negativo_preservado(self):
        assert parse_money("-1234,56") == Decimal("-1234.56")

    def test_zero(self):
        assert parse_money("0") == Decimal("0")
        assert parse_money("0,00") == Decimal("0.00")


class TestInvalidos:
    def test_letras(self):
        with pytest.raises(ValueError):
            parse_money("abc")

    def test_so_separator(self):
        with pytest.raises(ValueError):
            parse_money(",")

    def test_multiplos_decimais_incoerentes(self):
        # Cinco vírgulas seguidas não fazem sentido
        with pytest.raises(ValueError):
            parse_money("1,,2,,3")
