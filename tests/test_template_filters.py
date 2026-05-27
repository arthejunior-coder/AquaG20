"""Testes dos filtros Jinja em app/template_filters.py."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.template_filters import brl, mes_ano


class TestBrl:
    def test_decimal_inteiro(self):
        assert brl(Decimal("100")) == "R$ 100,00"

    def test_decimal_com_centavos(self):
        assert brl(Decimal("123.45")) == "R$ 123,45"

    def test_milhares_com_ponto_separator(self):
        assert brl(Decimal("1234.56")) == "R$ 1.234,56"

    def test_milhoes(self):
        assert brl(Decimal("1234567.89")) == "R$ 1.234.567,89"

    def test_zero(self):
        assert brl(Decimal("0")) == "R$ 0,00"

    def test_none_devolve_traco(self):
        assert brl(None) == "—"

    def test_negativo_preserva_sinal(self):
        assert brl(Decimal("-1234.56")) == "R$ -1.234,56"

    def test_float_aceito(self):
        assert brl(100.5) == "R$ 100,50"

    def test_int_aceito(self):
        assert brl(50) == "R$ 50,00"

    def test_string_numerica_aceita(self):
        assert brl("123.45") == "R$ 123,45"


class TestMesAno:
    def test_data_normal(self):
        assert mes_ano(date(2026, 5, 15)) == "05/2026"

    def test_dia_1_indiferente(self):
        assert mes_ano(date(2026, 5, 1)) == "05/2026"

    def test_dezembro(self):
        assert mes_ano(date(2026, 12, 31)) == "12/2026"

    def test_none_devolve_traco(self):
        assert mes_ano(None) == "—"

    def test_datetime_aceito(self):
        assert mes_ano(datetime(2026, 5, 15, 14, 30)) == "05/2026"
