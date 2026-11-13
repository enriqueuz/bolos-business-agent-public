"""Precio acordado vs dinero recibido: reglas puras para detectar cuándo un pago o un precio no cuadra.

Todo se compara con la tasa para cobrar del negocio (`EffectiveRates.applied`). Nunca se deduce ni se inventa otra tasa:
si las cifras no cuadran, el comando pregunta al dueño qué pasó (quedó debiendo, descuento, corregir…).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.money import EffectiveRates, convert_between, q, to_decimal

# Diferencias menores a esto se consideran redondeo, no un pago incompleto: 0,5 % del esperado o 1 unidad, lo mayor.
PAYMENT_TOLERANCE_PCT = Decimal("0.005")
PAYMENT_TOLERANCE_MIN = Decimal("1")
PAYMENT_TOLERANCE_MIN_USD = Decimal("0.01")


def expected_payment(total: Decimal, total_currency: str, paid_currency: str, rates: EffectiveRates) -> Decimal:
    """Cuánto debía pagar el cliente en la moneda en que pagó, a la tasa para cobrar del negocio."""
    return convert_between(to_decimal(total), total_currency, paid_currency, rates.applied)


def tolerance(expected: Decimal, currency: str, local_currency: str) -> Decimal:
    minimum = PAYMENT_TOLERANCE_MIN if str(currency) == local_currency else PAYMENT_TOLERANCE_MIN_USD
    return max(abs(expected) * PAYMENT_TOLERANCE_PCT, minimum)


@dataclass(frozen=True, slots=True)
class Match:
    """Pagó lo que correspondía (dentro de la tolerancia)."""


@dataclass(frozen=True, slots=True)
class Underpaid:
    """Pagó menos: `missing` es lo que falta, en la moneda del pago."""

    missing: Decimal


@dataclass(frozen=True, slots=True)
class Overpaid:
    """Pagó más: `extra` es lo que sobra, en la moneda del pago."""

    extra: Decimal


PaymentCheck = Match | Underpaid | Overpaid


def classify_payment(expected: Decimal, paid: Decimal, currency: str, local_currency: str) -> PaymentCheck:
    diff = to_decimal(paid) - to_decimal(expected)
    if abs(diff) <= tolerance(expected, currency, local_currency):
        return Match()
    return Overpaid(q(diff)) if diff > 0 else Underpaid(q(-diff))


@dataclass(frozen=True, slots=True)
class SameAsCatalog:
    """El precio dicho coincide con el de catálogo (o es mayor: no hay nada que aclarar)."""


@dataclass(frozen=True, slots=True)
class LowerThanCatalog:
    """El precio dicho es menor al de catálogo. Montos por unidad, en la moneda del catálogo."""

    catalog_unit: Decimal
    stated_unit: Decimal

    @property
    def difference(self) -> Decimal:
        return q(self.catalog_unit - self.stated_unit)


PriceCheck = SameAsCatalog | LowerThanCatalog


def classify_price(stated_unit: Decimal, stated_currency: str, catalog_unit: Decimal, catalog_currency: str,
                   rates: EffectiveRates) -> PriceCheck:
    """Compara el precio que dijo el dueño con el de catálogo, llevando el dicho a la moneda del catálogo."""
    stated_in_catalog = convert_between(to_decimal(stated_unit), stated_currency, catalog_currency, rates.applied)
    catalog_unit = to_decimal(catalog_unit)
    if catalog_unit - stated_in_catalog > tolerance(catalog_unit, catalog_currency, rates.local_currency):
        return LowerThanCatalog(catalog_unit=q(catalog_unit), stated_unit=stated_in_catalog)
    return SameAsCatalog()
