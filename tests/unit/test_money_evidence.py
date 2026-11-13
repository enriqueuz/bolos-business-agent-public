from decimal import Decimal

import pytest

from app.application.money_evidence import source_spans_overlap, validate_quoted_money_source


@pytest.mark.parametrize(
    "message,quote,value,is_usd_source,valid",
    [
        ("330$ Tony", "306900", "306900", False, False),
        ("330$ Tony", "330", "330", False, False),
        ("330$ Tony", "330$", "330", True, True),
        ("330$ Tony por 310.200,00 Bs", "310.200,00 Bs", "310200", False, True),
        ("100$ por 80000bs", "80000bs", "80000", False, True),
        ("100$ 80000bs", "80000bs", "80000", False, True),
        ("100$ 80000", "80000", "80000", False, True),
        ("100 USD 80000", "80000", "80000", False, True),
        ("100 $80000", "$80000", "80000", False, False),
        ("100 $80000", "$80000", "80000", True, True),
        ("Venta 900, recibí 990000 Bs", "900", "900", True, True),
        ("Venta 1000$ tasa 1000", "1000", "1000", True, False),
        ("Venta 1000$ tasa 1000", "tasa 1000", "1000", False, True),
        ("USD 330 a tasa 940", "940", "940", False, True),
        ("940", "940", "940", False, True),
        ("100$ por 100 Bs", "100", "100", False, False),
        ("100$ por 100 Bs", "100 Bs", "100", False, True),
        ("100$ por 80000 Bs", "800", "800", False, False),
        ("100$ por 80000 Bs", "100$ por 80000 Bs", "80000", False, False),
        ("330$ Tony", None, "330", False, False),
    ],
)
def test_exact_unique_money_quotes(message, quote, value, is_usd_source, valid):
    result = validate_quoted_money_source(
        message, quote, Decimal(value), expected_currency="USD" if is_usd_source else "VES"
    )
    assert bool(result) is valid
    if result:
        assert message[result["start"] : result["end"]] in quote


def test_validator_does_not_infer_semantic_roles_from_keywords():
    # These are valid source locations; deciding whether they describe THIS transaction belongs
    # to the model. Numeric occurrence alone is deliberately not called semantic verification.
    for message in ("saldo anterior 306900", "referencia bancaria 306900", "importe real 306900"):
        assert validate_quoted_money_source(message, "306900", Decimal("306900")) is not None


def test_number_cannot_be_reused_as_principal_and_bolivars():
    principal = validate_quoted_money_source("Tony 330", "330", Decimal("330"), expected_currency="USD")
    bolivars = validate_quoted_money_source("Tony 330", "330", Decimal("330"))
    assert source_spans_overlap(principal, bolivars)


def test_bolivars_cannot_supply_explicit_dollar_source():
    assert validate_quoted_money_source("330 Bs", "330", Decimal("330"), expected_currency="USD") is None
