"""Confirmation fixtures use a real risky edit, not obsolete new-customer policy."""

from sqlalchemy import select

from app.application.commands.sales import RecordSale, SaleItemInput
from app.application.context import Actor, EntityRef, ExecContext
from app.application.conversation import conversation_scope
from app.application.summaries import sale_summary
from app.db.models import Customer, Sale
from app.domain.enums import ActorType, EntityType, PaymentStatus


async def ask_sale_customer_correction(container, script, *, customer_name="Pedro", items=None):
    from tests.agent.test_pipeline import PHONE, send

    identity = await container.identity.resolve(PHONE)
    async with container.session_factory() as session:
        existing = (await session.scalars(select(Sale).where(Sale.business_id == identity.business.id))).one_or_none()
        reference = (EntityRef(EntityType.SALE, existing.id, version=existing.version, summary=sale_summary(existing))
                     if existing else None)
    if reference is None:
        sale_items = items or [{"product": "caucho", "unit_price": "40", "currency": "USD"}]
        result = await container.bus.dispatch(RecordSale(
            items=[SaleItemInput(**item) for item in sale_items], customer_name="Moisés", payment_status=PaymentStatus.CREDIT),
            ExecContext(business_id=identity.business.id, actor=Actor(ActorType.PANEL, identity.member_id)))
        reference = result.affected[0]
    handle = await container.store.register_handle(conversation_scope(identity.business.id, PHONE), reference.entity_type,
                                                   reference.entity_id, reference.version, reference.summary)
    script.steps = [{"tool": "edit_record", "args": {
        "handle": handle.handle, "changes": {"customer_name": customer_name},
    }}]
    return await send(container, f"La venta de Moisés era de {customer_name}")


async def sale_customer_name(session_factory):
    async with session_factory() as session:
        return (await session.scalars(select(Customer.name).join(Sale, Sale.customer_id == Customer.id))).one()
