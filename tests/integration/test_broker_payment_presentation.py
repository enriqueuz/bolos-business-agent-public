"""Los mensajes reflejan el estado calculado/persistido, sin cambiar el ledger."""

from decimal import Decimal

import pytest

from app.agent.receipts import render, render_preview
from app.application.commands.brokerage import (
    RecordBrokerAccountPayment,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from tests.integration.test_brokerage import _brokerage, _ctx


@pytest.mark.parametrize("as_owner", [False, True])
@pytest.mark.parametrize("amount, prior, owed, credit", [
    ("200", None, "300.00", None),
    ("500", None, None, None),
    ("600", None, None, "100.00"),
    ("50", "600", None, "150.00"),
])
async def test_payment_preview_and_receipt_use_authorized_accumulated_state(
    bus, session_factory, as_owner, amount, prior, owed, credit,
):
    business, owner, broker, _ = await _brokerage(session_factory)
    broker_ctx = _ctx(business, broker)
    await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_name="Juan", customer_classification="personal", amount="500"), broker_ctx)
    await bus.dispatch(RecordBrokerAccountPayment(amount="1196"), broker_ctx)
    if prior:
        await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Juan", amount=prior), broker_ctx, confirmed=True)
    ctx = _ctx(business, owner) if as_owner else broker_ctx
    command = RecordBrokerReceivablePayment(customer_name="Juan", amount=amount, broker_id=broker)
    preview_result = await bus.dispatch(command, ctx)
    # Partial/exact payments execute directly; explicit renderer still verifies their conditional presentation.
    preview = render_preview(preview_result)
    result = await bus.dispatch(command, ctx, confirmed=True) if preview_result.needs_confirmation else preview_result
    receipt = render(result)
    assert result.data["balance_raw"] == str(Decimal("701") + Decimal(prior or "0") + Decimal(amount)) + ".00"
    assert result.data["customer_balance"]["owed"] == ({"USD": owed} if owed else {})
    assert result.data["customer_balance"]["credit_in_favor"] == ({"USD": credit} if credit else {})
    if owed:
        assert "Juan debe a Casa: *$300,00*" in receipt
        assert "Juan debe a Casa quedaría en: *$300,00*" in preview
    elif credit:
        expected = "$150,00" if prior else "$100,00"
        assert f"Saldo a favor de Juan: *{expected}*" in receipt
        assert f"Saldo a favor de Juan quedaría en: *{expected}*" in preview
    else:
        assert "Saldo de Juan: *$0,00*. Todo saldado." in receipt
        assert "Quedaría todo saldado" in preview
    label = f"Balance de {result.data['broker']}" if as_owner else "Tu balance actual"
    preview_label = label if as_owner else "Tu balance quedaría en"
    assert f"{label}: *+" in receipt and f"{preview_label}: *+" in preview
