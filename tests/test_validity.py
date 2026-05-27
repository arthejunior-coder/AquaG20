"""Testes do app/validity.py — regras de validade de garrafão.

Janela aceita: [mês atual, mês atual + 36 meses].
Sempre com `hoje` injetável pra determinismo.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.validity import (
    GARRAFAO_VALIDADE_MAX_MESES,
    _add_meses,
    parse_mes_ano_input,
    primeiro_dia_do_mes,
    validar_validade_pedida,
)


class TestAddMeses:
    def test_soma_dentro_do_mesmo_ano(self):
        assert _add_meses(date(2026, 1, 15), 5) == date(2026, 6, 1)

    def test_atravessa_ano(self):
        assert _add_meses(date(2026, 10, 1), 5) == date(2027, 3, 1)

    def test_36_meses_e_3_anos_exatos(self):
        assert _add_meses(date(2026, 5, 1), 36) == date(2029, 5, 1)


class TestPrimeiroDiaDoMes:
    def test_qualquer_data_vira_dia_1(self):
        assert primeiro_dia_do_mes(date(2026, 5, 28)) == date(2026, 5, 1)

    def test_dia_1_passa_intacto(self):
        assert primeiro_dia_do_mes(date(2026, 5, 1)) == date(2026, 5, 1)


class TestValidarValidadePedida:
    HOJE = date(2026, 5, 15)  # fixo pra testes — meio de mai/26

    def test_mes_atual_aceito(self):
        validar_validade_pedida(date(2026, 5, 28), hoje=self.HOJE)  # mesmo mês, dia diferente

    def test_dia_1_do_mes_atual_aceito(self):
        validar_validade_pedida(date(2026, 5, 1), hoje=self.HOJE)

    def test_mes_passado_rejeitado(self):
        with pytest.raises(ValueError, match="passado"):
            validar_validade_pedida(date(2026, 4, 30), hoje=self.HOJE)

    def test_ano_passado_rejeitado(self):
        with pytest.raises(ValueError, match="passado"):
            validar_validade_pedida(date(2025, 12, 31), hoje=self.HOJE)

    def test_1_mes_no_futuro_aceito(self):
        validar_validade_pedida(date(2026, 6, 1), hoje=self.HOJE)

    def test_36_meses_no_futuro_aceito_limite(self):
        # mai/26 + 36 = mai/29 — aceita
        validar_validade_pedida(date(2029, 5, 15), hoje=self.HOJE)

    def test_37_meses_no_futuro_rejeitado(self):
        with pytest.raises(ValueError, match="excede"):
            validar_validade_pedida(date(2029, 6, 1), hoje=self.HOJE)

    def test_garrafao_2030_rejeitado_em_mai_2026(self):
        """Cenário real do bug reportado pelo usuário."""
        with pytest.raises(ValueError, match="excede"):
            validar_validade_pedida(date(2030, 12, 5), hoje=self.HOJE)

    def test_default_hoje_e_date_today(self):
        """Sem `hoje=`, usa _hoje() (que respeita o override de conftest)."""
        # Data 100 anos no futuro DEVE rejeitar independente de quando rodar
        with pytest.raises(ValueError):
            validar_validade_pedida(date(2125, 1, 1))


class TestParseMesAnoInput:
    def test_input_valido(self):
        assert parse_mes_ano_input("2027-03") == date(2027, 3, 1)

    def test_vazio_devolve_none(self):
        assert parse_mes_ano_input("") is None
        assert parse_mes_ano_input(None) is None
        assert parse_mes_ano_input("   ") is None

    def test_formato_invalido(self):
        with pytest.raises(ValueError):
            parse_mes_ano_input("2027/03")
        with pytest.raises(ValueError):
            parse_mes_ano_input("abc-def")

    def test_mes_invalido(self):
        with pytest.raises(ValueError):
            parse_mes_ano_input("2027-13")


class TestConstantes:
    def test_max_meses_e_36(self):
        # Documentação inline: 36 meses = 3 anos. Garante que não mexeram sem revisão.
        assert GARRAFAO_VALIDADE_MAX_MESES == 36
