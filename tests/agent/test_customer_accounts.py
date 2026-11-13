"""The real command path is shared by WhatsApp tools and panel writes."""
from sqlalchemy import func, select

from app.application.queries.brokerage import BrokerageQueries
from app.db.models import BrokerMovement, BusinessMember, CustomerPayment, Receivable
from app.domain.enums import MemberRole
from tests.agent.test_brokerage_vertical import _set_brokerage
from tests.agent.test_pipeline import send


async def test_whatsapp_pending_sale_purchase_and_real_payout(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "settlement": "pending", "operation_amount": "1524",
        "exchange_rate": "1000", "customer_name": "Hum", "customer_classification": "global",
        "text_money_sources": {"principal_usd": "1524$", "exchange_rate": "1000"}}}]
    created = await send(container, "Venta 1524$ tasa 1000 Hum, pendiente")
    assert created.status == "ok"
    assert "Venta pendiente registrada" in created.replies[0]["text"]
    assert "a favor de HUM" in created.replies[0]["text"]
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {
        "amount": "200", "exchange_rate": "1000", "customer_name": "Hum",
        "text_money_sources": {"principal_usd": "200$", "exchange_rate": "1000"}}}]
    bought = await send(container, "Compra pendiente 200$ tasa 1000 Hum")
    assert "pendiente o liquidada" in bought.replies[0]["text"]
    bought = await send(container, "Pendiente")
    assert "$1.324,00" in bought.replies[0]["text"]
    script.steps = [{"tool": "record_broker_customer_payout", "args": {"customer_name": "Hum", "amount": "1324"}}]
    paid = await send(container, "Pagué 1324$ a Hum")
    assert paid.status == "ok"
    assert "Todo saldado" in paid.replies[0]["text"]
    async with session_factory() as db:
        owner = (await db.scalars(select(BusinessMember))).one()
        assert await db.scalar(select(func.count()).select_from(CustomerPayment)) == 0
        assert await db.scalar(select(func.count()).select_from(Receivable)) == 1
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 3
    accounts = await BrokerageQueries(session_factory).customer_accounts(business.id, owner.id, status="all")
    assert accounts["items"][0]["balance_usd"] == "0.00"
