"""Registro de monedas (códigos ISO 4217).

Agregar una moneda = una entrada aquí. El sistema no tiene lógica especial por moneda: la triple contabilidad es
siempre "moneda local del negocio + USD a tasa oficial + USD a tasa de mercado" (ver countries.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

USD = "USD"


@dataclass(frozen=True, slots=True)
class CurrencyInfo:
    code: str
    name: str
    plural: str
    symbol: str
    symbol_after: bool  # "1.234,56 Bs" (True) vs "$1.234,56" (False)
    decimals: int = 2
    thousands_sep: str = "."
    decimal_sep: str = ","


CURRENCIES: dict[str, CurrencyInfo] = {
    "USD": CurrencyInfo("USD", "dólar estadounidense", "dólares", "$", symbol_after=False),
    "VES": CurrencyInfo("VES", "bolívar", "bolívares", "Bs", symbol_after=True),
    # Futuro (ejemplos): "COP": CurrencyInfo("COP", "peso colombiano", "pesos colombianos", "$", symbol_after=False, decimals=0),
    #                    "ARS": CurrencyInfo("ARS", "peso argentino", "pesos argentinos", "$", symbol_after=False),
}

# Enum generado del registro: da schemas cerrados a las tools y validación a Pydantic.
# Qué monedas acepta CADA negocio lo decide su perfil de país (local + USD), no este enum.
Currency = StrEnum("Currency", {code: code for code in CURRENCIES})  # type: ignore[misc]


def currency_info(code: str) -> CurrencyInfo:
    info = CURRENCIES.get(str(code))
    if info is None:
        raise KeyError(f"Moneda no registrada: {code}")
    return info
