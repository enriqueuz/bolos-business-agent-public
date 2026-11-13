from decimal import Decimal

import pytest

from app.domain.brokerage import operation_money, parse_broker_decimal
from app.domain.errors import ValidationFailed


@pytest.mark.parametrize("raw,expected", [("80.000,50", "80000.50"), ("80.000", "80000"),
                                         ("80000.50", "80000.50"), ("100,25", "100.25")])
def test_venezuelan_amounts(raw, expected):
    assert parse_broker_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["0", "-500", "NaN", "Infinity", "1e16"])
def test_direct_withdrawal_requires_positive_storable_usd(raw):
    from app.application.commands.brokerage import RecordBrokerAccountWithdrawal
    with pytest.raises(ValueError):
        RecordBrokerAccountWithdrawal(amount=raw)


@pytest.mark.parametrize("raw", ["1,000", "NaN", "Infinity", "teléfono"])
def test_ambiguous_or_nonfinite_numbers(raw):
    with pytest.raises(ValueError):
        parse_broker_decimal(raw)


def test_real_rate_only_comes_from_user_inputs():
    assert operation_money(Decimal("100")).exchange_rate is None
    from_bs = operation_money(Decimal("100"), Decimal("80000"))
    assert from_bs.exchange_rate == Decimal("800.00")
    assert from_bs.rate_source == "calculated"
    from_rate = operation_money(Decimal("100"), exchange_rate=Decimal("800"))
    assert from_rate.amount_bs == Decimal("80000.00")
    assert from_rate.basis == "usd_rate"


def test_inconsistent_triple_cannot_be_persisted():
    with pytest.raises(ValidationFailed, match="no coinciden"):
        operation_money(Decimal("100"), Decimal("80000"), Decimal("801"))


def test_repeating_rate_keeps_two_places():
    assert operation_money(Decimal("3"), Decimal("100")).exchange_rate == Decimal("33.33")


def test_decimal_freezing_does_not_change_a_three_place_decimal_into_thousands():
    from app.application.commands.brokerage import RecordBrokerOperation
    command = RecordBrokerOperation(operation_type="buy", operation_amount=Decimal("1.123"))
    replayed = RecordBrokerOperation.model_validate(command.model_dump(mode="json"))
    assert replayed.operation_amount == Decimal("1.123")


@pytest.mark.parametrize("usd,bs,rate", [("1e16", None, None), ("100", "1e25", None), ("100", None, "1e22")])
def test_out_of_storage_range_is_a_domain_error(usd, bs, rate):
    with pytest.raises(ValidationFailed):
        operation_money(Decimal(usd), Decimal(bs) if bs else None, Decimal(rate) if rate else None)
