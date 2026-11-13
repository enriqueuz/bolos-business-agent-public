"""Decimal boundary adapters. Monetary calculations remain in the pure domain."""

from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

from app.domain.brokerage import parse_broker_decimal


def serialize_broker_decimal(value: Decimal) -> str:
    text = format(value, "f")
    # A canonical decimal with three fractional places must not look like a Venezuelan thousands group on replay.
    if "." in text and len(text.partition(".")[2]) == 3:
        text += "0"
    return text


BrokerDecimal = Annotated[Decimal, BeforeValidator(parse_broker_decimal), PlainSerializer(serialize_broker_decimal, return_type=str)]
