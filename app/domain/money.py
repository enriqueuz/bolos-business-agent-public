"""Dinero con triple contabilidad, independiente del país.

Cada movimiento guarda su monto nativo + cuatro patas + las tasas usadas, congeladas al momento de escribir:
  - local:        en la moneda local del negocio (VES en Venezuela, COP en Colombia…)
  - usd_applied:  en USD a la tasa para cobrar aplicada (BCV, mercado o la propia del negocio): la de los reportes
  - usd_official: en USD a la tasa oficial (BCV, TRM…): referencia
  - usd_market:   en USD a la tasa de mercado automática: referencia

Retail centraliza sus conversiones de referencia en `compute_legs`. Brokerage usa `operation_money` para Bs/tasa reales proporcionados por el usuario. Los reportes leen patas
congeladas; nunca vuelven a buscar tasas. Las tasas se expresan siempre como "unidades de moneda local por 1 USD".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.domain.currencies import USD, currency_info
from app.domain.enums import CurrencyView, RateAppliedSource
from app.domain.errors import RatesUnavailable, ValidationFailed

AMOUNT_Q = Decimal("0.0001")


def q(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_Q, rounding=ROUND_HALF_UP)


def to_decimal(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class EffectiveRates:
    """Tasas vigentes para una escritura (moneda local por USD).

    - `applied`: la tasa para cobrar del negocio (según `charge_rate_mode`). No hay tasas especiales por operación.
    - `official` y `market`: las automáticas, que se guardan solo como referencia.
    - `funds`: la tasa real de los bolívares del negocio (a la que los obtuvo), si la conoce. Sirve para el costo real
      de lo que paga en moneda local; nunca para cobrar.
    """

    local_currency: str
    official: Decimal
    market: Decimal
    applied: Decimal
    applied_source: RateAppliedSource
    official_as_of: datetime | None = None
    market_as_of: datetime | None = None
    applied_set_at: datetime | None = None
    applied_label: str | None = None
    funds: Decimal | None = None
    funds_set_at: datetime | None = None

    def ensure_usable(self) -> None:
        if self.official <= 0 or self.market <= 0 or self.applied <= 0:
            raise RatesUnavailable(
                "No tengo tasas de cambio disponibles todavía; no puedo convertir montos. "
                "Fija tu tasa para cobrar o intenta en unos minutos."
            )


@dataclass(frozen=True, slots=True)
class MoneyLegs:
    amount: Decimal
    currency: str
    local_currency: str
    local: Decimal
    usd_official: Decimal
    usd_market: Decimal
    usd_applied: Decimal
    rate_official: Decimal
    rate_market: Decimal
    rate_applied: Decimal
    rate_applied_source: RateAppliedSource

    def leg(self, view: CurrencyView) -> Decimal:
        return {CurrencyView.LOCAL: self.local, CurrencyView.USD_OFFICIAL: self.usd_official,
                CurrencyView.USD_MARKET: self.usd_market, CurrencyView.USD_APPLIED: self.usd_applied}[view]


RATE_SANITY_MIN = Decimal("0.5")
RATE_SANITY_MAX = Decimal("3")


def unusual_rate_reason(rate: Decimal, official: Decimal | None, local_currency: str, official_label: str) -> str | None:
    """Tasa fuera de 0,5×–3× de la oficial → probable error de tipeo. No se rechaza: se pide confirmación (puede ser real)."""
    if not official or RATE_SANITY_MIN * official <= rate <= RATE_SANITY_MAX * official:
        return None
    return (f"La tasa {fmt_rate(rate, local_currency)} es muy distinta a la {official_label} "
            f"({fmt_rate(official, local_currency)}). ¿Seguro que es correcta?")


def ensure_supported(currency: str, local_currency: str) -> None:
    if str(currency) not in (local_currency, USD):
        raise ValidationFailed(
            f"Este negocio registra en {local_currency} o USD; {currency} no está soportado.",
            allowed=[local_currency, USD],
        )


def compute_legs(amount: Decimal, currency: str, rates: EffectiveRates) -> MoneyLegs:
    """Convierte un monto nativo a sus cuatro patas con las tasas dadas.

    - Origen local: usd_applied = amount / applied ; usd_official = amount / official ; usd_market = amount / market.
    - Origen USD:   todas las patas en USD = amount ; local = amount * applied (lo que se cobra en moneda local).
    """
    amount = to_decimal(amount)
    currency = str(currency)
    if amount < 0:
        raise ValidationFailed("El monto no puede ser negativo.")
    ensure_supported(currency, rates.local_currency)
    rates.ensure_usable()
    if currency == rates.local_currency:
        local = amount
        usd_official, usd_market, usd_applied = amount / rates.official, amount / rates.market, amount / rates.applied
    else:
        local, usd_official, usd_market, usd_applied = amount * rates.applied, amount, amount, amount
    return MoneyLegs(amount=q(amount), currency=currency, local_currency=rates.local_currency, local=q(local),
                     usd_official=q(usd_official), usd_market=q(usd_market), usd_applied=q(usd_applied),
                     rate_official=rates.official, rate_market=rates.market, rate_applied=rates.applied,
                     rate_applied_source=rates.applied_source)


def real_cost_usd(amount: Decimal, currency: str, local_currency: str, funds: Decimal | None) -> Decimal | None:
    """Cuánto le costó realmente al negocio pagar este monto, en $, según la tasa a la que obtuvo sus bolívares.

    En $ es el mismo monto. En moneda local hace falta la tasa real; sin ella devuelve None (no se inventa).
    """
    amount = to_decimal(amount)
    if str(currency) != local_currency:
        return q(amount)
    if not funds or funds <= 0:
        return None
    return q(amount / funds)


def recompute_with_frozen_rates(amount: Decimal, currency: str, local_currency: str, rate_official: Decimal,
                                rate_market: Decimal, rate_applied: Decimal, source: RateAppliedSource) -> MoneyLegs:
    """Para correcciones: el monto cambia, pero las tasas del registro original se conservan."""
    return compute_legs(amount, currency, EffectiveRates(local_currency=local_currency, official=rate_official,
                                                         market=rate_market, applied=rate_applied, applied_source=source))


def sum_legs(legs: list[MoneyLegs]) -> MoneyLegs:
    """Suma patas de ítems de una misma escritura (mismas tasas).

    Todos en la misma moneda → el total queda en esa moneda. Mezcla local/USD → el total se expresa en USD
    con la pata aplicada.
    """
    if not legs:
        raise ValidationFailed("No hay montos que sumar.")
    first = legs[0]
    currencies = {leg.currency for leg in legs}
    local = sum((leg.local for leg in legs), Decimal(0))
    usd_official = sum((leg.usd_official for leg in legs), Decimal(0))
    usd_market = sum((leg.usd_market for leg in legs), Decimal(0))
    usd_applied = sum((leg.usd_applied for leg in legs), Decimal(0))
    if len(currencies) > 1:
        target, amount = USD, usd_applied
    elif first.currency == first.local_currency:
        target, amount = first.currency, local
    else:
        target, amount = first.currency, sum((leg.amount for leg in legs), Decimal(0))
    return MoneyLegs(amount=q(amount), currency=target, local_currency=first.local_currency, local=q(local),
                     usd_official=q(usd_official), usd_market=q(usd_market), usd_applied=q(usd_applied),
                     rate_official=first.rate_official, rate_market=first.rate_market, rate_applied=first.rate_applied,
                     rate_applied_source=first.rate_applied_source)


def sub_legs(a: MoneyLegs, b: MoneyLegs) -> MoneyLegs:
    """a − b, pata por pata (misma escritura, misma moneda): p. ej. precio de lista − descuento = venta neta."""
    if a.currency != b.currency:
        raise ValidationFailed("No se pueden restar montos en monedas distintas.")
    return MoneyLegs(amount=q(a.amount - b.amount), currency=a.currency, local_currency=a.local_currency,
                     local=q(a.local - b.local), usd_official=q(a.usd_official - b.usd_official),
                     usd_market=q(a.usd_market - b.usd_market), usd_applied=q(a.usd_applied - b.usd_applied),
                     rate_official=a.rate_official, rate_market=a.rate_market, rate_applied=a.rate_applied,
                     rate_applied_source=a.rate_applied_source)


def frozen_rates(legs: MoneyLegs) -> EffectiveRates:
    """Las tasas congeladas de una escritura, para calcular otros montos de la misma escritura con las mismas tasas."""
    return EffectiveRates(local_currency=legs.local_currency, official=legs.rate_official, market=legs.rate_market,
                          applied=legs.rate_applied, applied_source=legs.rate_applied_source)


def convert_between(amount: Decimal, from_currency: str, to_currency: str, rate: Decimal | None) -> Decimal:
    """Convierte entre la moneda local y USD con la tasa aplicada de la escritura (abonos a fiados en otra moneda)."""
    if str(from_currency) == str(to_currency):
        return q(amount)
    if rate is None or rate <= 0:
        raise RatesUnavailable("No hay tasa para convertir el abono.")
    if str(from_currency) == USD:
        return q(amount * rate)
    return q(amount / rate)


def _group(value: Decimal, decimals: int, thousands: str, decimal_sep: str) -> str:
    quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    v = to_decimal(value).quantize(quant, rounding=ROUND_HALF_UP)
    integer, _, frac = f"{abs(v):,.{decimals}f}".partition(".")
    text = integer.replace(",", thousands)
    return f"{text}{decimal_sep}{frac}" if decimals else text


def fmt_amount(amount: Decimal, currency: str) -> str:
    """Formato según el registro de monedas (símbolo, decimales, separadores). Ej.: 1.234,56 Bs · $1.234,56."""
    info = currency_info(str(currency))
    value = to_decimal(amount)
    sign = "-" if value < 0 else ""
    text = _group(value, info.decimals, info.thousands_sep, info.decimal_sep)
    return f"{sign}{text} {info.symbol}" if info.symbol_after else f"{sign}{info.symbol}{text}"


def fmt_view(amount: Decimal, view: CurrencyView, local_currency: str) -> str:
    """Monto en la pata elegida. A qué tasa está el dólar se indica una vez en el título, no en cada cifra."""
    return fmt_amount(amount, local_currency if view == CurrencyView.LOCAL else USD)


def fmt_qty(value: Decimal) -> str:
    """10 → '10' (no '1E+1'), 2.500 → '2.5'."""
    text = f"{value.normalize():f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt_rate(rate: Decimal, local_currency: str) -> str:
    info = currency_info(local_currency)
    return _group(to_decimal(rate), 2, info.thousands_sep, info.decimal_sep)
