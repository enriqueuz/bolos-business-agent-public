"""Precio acordado vs dinero recibido (app/domain/pricing.py): siempre contra la tasa para cobrar, nunca otra."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import RateAppliedSource
from app.domain.money import EffectiveRates
from app.domain.pricing import (
    LowerThanCatalog,
    Match,
    Overpaid,
    SameAsCatalog,
    Underpaid,
    classify_payment,
    classify_price,
    expected_payment,
)

BCV = EffectiveRates(local_currency="VES", official=Decimal("849.56"), market=Decimal("927.22"), applied=Decimal("849.56"),
                     applied_source=RateAppliedSource.OFFICIAL)


def test_expected_payment_uses_the_charge_rate():
    assert expected_payment(Decimal(10), "USD", "VES", BCV) == Decimal("8495.6000")
    assert expected_payment(Decimal(10), "USD", "USD", BCV) == Decimal("10.0000")
    assert expected_payment(Decimal("8495.60"), "VES", "USD", BCV) == Decimal("10.0000")


def test_exact_payment_and_rounding_are_a_match():
    assert classify_payment(Decimal("8495.60"), Decimal("8495.60"), "VES", "VES") == Match()
    assert classify_payment(Decimal("8495.60"), Decimal("8495"), "VES", "VES") == Match()  # redondeo a Bs enteros
    assert classify_payment(Decimal(10), Decimal("10.01"), "USD", "VES") == Match()


def test_short_and_extra_payments_are_detected():
    assert classify_payment(Decimal("8495.60"), Decimal(3000), "VES", "VES") == Underpaid(Decimal("5495.6000"))
    assert classify_payment(Decimal(8000), Decimal(2000), "VES", "VES") == Underpaid(Decimal("6000.0000"))
    assert classify_payment(Decimal(10), Decimal(12), "USD", "VES") == Overpaid(Decimal("2.0000"))


def test_price_lower_than_catalog_is_compared_in_the_catalog_currency():
    # catálogo $10; "lo vendí en 2.000 Bs" a BCV 800 = $2,50
    rates = EffectiveRates("VES", Decimal(800), Decimal(900), Decimal(800), RateAppliedSource.OFFICIAL)
    check = classify_price(Decimal(2000), "VES", Decimal(10), "USD", rates)
    assert check == LowerThanCatalog(catalog_unit=Decimal("10.0000"), stated_unit=Decimal("2.5000"))
    assert check.difference == Decimal("7.5000")
    assert classify_price(Decimal(8000), "VES", Decimal(10), "USD", rates) == SameAsCatalog()
    assert classify_price(Decimal(12), "USD", Decimal(10), "USD", rates) == SameAsCatalog()  # más caro: nada que aclarar
