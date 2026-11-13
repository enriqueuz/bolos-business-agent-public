"""Reglas puras del ledger de corredores."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from app.domain.errors import ValidationFailed

DEFAULT_COMMISSION_RATE = Decimal("0.01")
CENT = Decimal("0.01")
RATE_QUANTUM = Decimal("0.01")


def parse_broker_decimal(value: object) -> Decimal:
    """Parse monetary syntax, never business intent or a reference exchange rate."""
    if isinstance(value, str):
        raw = value.strip().replace("\u00a0", "").replace(" ", "")
        if re.fullmatch(r"[1-9]\d{0,2}(?:\.\d{3})+(?:,\d+)?", raw):
            raw = raw.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d+,\d{1,8}", raw) and (len(raw.partition(",")[2]) != 3 or raw.startswith("0,")):
            raw = raw.replace(",", ".")
        elif "," in raw:
            raise ValueError("Formato ambiguo: usa 1000,00 o 1000.00, sin separadores de miles.")
        value = raw
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError("El monto debe ser un decimal válido.") from exc
    if not amount.is_finite():
        raise ValueError("El monto debe ser finito.")
    return amount


@dataclass(frozen=True, slots=True)
class OperationMoney:
    amount_bs: Decimal | None
    exchange_rate: Decimal | None
    basis: str | None
    rate_source: str | None


def operation_money(
    amount_usd: Decimal, amount_bs: Decimal | None = None, exchange_rate: Decimal | None = None,
) -> OperationMoney:
    """Resolve only user-provided monetary inputs; unknowns remain unknown."""
    if not amount_usd.is_finite():
        raise ValidationFailed("El monto USD debe ser finito.")
    if amount_usd > Decimal("99999999999999.99"):
        raise ValidationFailed("El monto USD excede el límite admitido.")
    usd = q_money(amount_usd)
    if usd <= 0:
        raise ValidationFailed("El monto USD debe ser de al menos $0,01.")
    for value in (amount_bs, exchange_rate):
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValidationFailed("Bs y tasa deben ser valores positivos y finitos.")
    if amount_bs is not None and amount_bs > Decimal("9999999999999999999999.99") or exchange_rate is not None and exchange_rate > Decimal("99999999999999999999.99"):
        raise ValidationFailed("Bs o tasa exceden el límite admitido.")
    with localcontext() as context:
        context.prec = 64
        bs = q_money(amount_bs) if amount_bs is not None else None
        rate = exchange_rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP) if exchange_rate is not None else None
        if bs is not None and bs > Decimal("9999999999999999999999.99") or rate is not None and rate > Decimal("99999999999999999999.99"):
            raise ValidationFailed("Bs o tasa exceden el límite admitido.")
        if bs is not None and bs <= 0 or rate is not None and rate <= 0:
            raise ValidationFailed("Bs o tasa están por debajo de la precisión admitida.")
        if bs is not None and rate is not None and abs(q_money(usd * rate) - bs) > CENT:
            raise ValidationFailed("USD, Bs y tasa no coinciden. Aclara los valores antes de guardar.")
        if bs is not None:
            computed_rate = rate if rate is not None else (bs / usd).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
            if computed_rate <= 0:
                raise ValidationFailed("La tasa calculada está por debajo de los dos decimales admitidos.")
            if computed_rate > Decimal("99999999999999999999.99"):
                raise ValidationFailed("La tasa calculada excede el límite admitido.")
            return OperationMoney(bs, computed_rate, "usd_bs", "explicit" if rate is not None else "calculated")
        if rate is not None:
            bs = q_money(usd * rate)
            if bs > Decimal("9999999999999999999999.99"):
                raise ValidationFailed("Los Bs calculados exceden el límite admitido.")
            if bs <= 0:
                raise ValidationFailed("El monto calculado debe ser de al menos Bs 0,01.")
            return OperationMoney(bs, rate, "usd_rate", "explicit")
        return OperationMoney(None, None, None, None)


@dataclass(frozen=True, slots=True)
class OperationCredit:
    excess: Decimal
    commission: Decimal
    credit: Decimal


@dataclass(frozen=True, slots=True)
class AccountPurchaseEffect:
    commission: Decimal
    balance_delta: Decimal

def q_money(value: Decimal) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = 64
            return value.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValidationFailed("El monto excede la precisión admitida.") from exc


def default_broker_abbreviation(name: str) -> str:
    """Iniciales estables para el atajo de una cuenta de corredor."""
    initials = "".join(next((char for char in part if char.isalnum()), "") for part in name.split()).upper()
    if not initials:
        raise ValidationFailed("El nombre del corredor no permite generar una abreviación.")
    return initials[:12]


def normalize_broker_abbreviation(value: str) -> str:
    """Normaliza una abreviación editable a letras/números, sin espacios ni puntuación."""
    normalized = "".join(char for char in value.strip().upper() if char.isalnum())
    if not 1 <= len(normalized) <= 12:
        raise ValidationFailed("La abreviación debe tener entre 1 y 12 letras o números.")
    return normalized


def account_purchase_effect(operation_amount: Decimal, rate: Decimal) -> AccountPurchaseEffect:
    """Compra propia: el principal queda en la cuenta y la comisión se acredita de inmediato."""
    if operation_amount <= 0:
        raise ValidationFailed("El monto debe ser mayor que cero.")
    if rate < 0 or rate > Decimal("1"):
        raise ValidationFailed("La comisión debe estar entre 0% y 100%.")
    amount = q_money(operation_amount)
    if amount <= 0:
        raise ValidationFailed("El monto debe ser de al menos $0,01.")
    commission = q_money(amount * rate)
    return AccountPurchaseEffect(commission=commission, balance_delta=q_money(commission - amount))


def purchase_excess(operation_amount: Decimal, received_amount: Decimal | None) -> Decimal:
    """USD recibidos explícitos, independientes de pagos y modalidad de compra."""
    if received_amount is None:
        return Decimal("0.00")
    if not received_amount.is_finite() or received_amount <= 0:
        raise ValidationFailed("Los USD recibidos deben ser positivos y finitos.")
    received = q_money(received_amount)
    if received > Decimal("99999999999999.99"):
        raise ValidationFailed("Los USD recibidos exceden el límite admitido.")
    if received < q_money(operation_amount):
        raise ValidationFailed("Los USD recibidos son menores que la compra. Aclara los importes antes de registrar.")
    return q_money(received - q_money(operation_amount))


def operation_credit(
    operation_amount: Decimal,
    delivered_amount: Decimal | None,
    rate: Decimal,
    *,
    operation_type: str = "sell",
    received_amount: Decimal | None = None,
) -> OperationCredit:
    """Beneficio independiente del principal; ventas nunca reconocen excedentes."""
    if operation_type not in {"buy", "sell"}:
        raise ValidationFailed("La operación debe ser una compra o una venta de divisas.")
    if not operation_amount.is_finite() or operation_amount <= 0:
        raise ValidationFailed("Los montos de la operación deben ser mayores que cero.")
    amount = q_money(operation_amount)
    if amount <= 0:
        raise ValidationFailed("Los montos de la operación deben ser de al menos $0,01.")
    if delivered_amount is not None and q_money(delivered_amount) != amount:
        raise ValidationFailed("Aclara el monto nominal de la operación. No se registran excedentes en ventas ni se usa entregado para compras.")
    if operation_type == "sell" and received_amount is not None:
        raise ValidationFailed("USD recibidos solo aplica a compras. Aclara el monto nominal de la venta.")
    if not rate.is_finite() or rate < 0 or rate > Decimal("1"):
        raise ValidationFailed("La comisión debe estar entre 0% y 100%.")
    excess = purchase_excess(amount, received_amount) if operation_type == "buy" else Decimal("0.00")
    commission = q_money(amount * rate)
    return OperationCredit(excess, commission, q_money(excess + commission))
