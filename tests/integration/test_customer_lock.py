"""Bloqueo por cliente: dos abonos simultáneos del mismo cliente no sobrepagan un fiado."""

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.common import lock_customers
from app.db.models import Customer, PaymentAllocation, Receivable
from app.domain.enums import Currency
from app.domain.errors import Conflict


async def _two_open_receivables(bus, ctx):
    for what in ("lunes", "martes"):
        await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD, description=what), ctx)


async def test_simultaneous_payments_do_not_overpay_a_receivable(bus, panel_ctx, session_factory, monkeypatch):
    from app.application.commands import receivables

    await _two_open_receivables(bus, panel_ctx)

    # Determinista: los dos abonos llegan al punto "leer qué fiados siguen abiertos" ANTES de que cualquiera avance, que es
    # la peor carrera posible. Con el bloqueo el segundo ni llega (espera al primero), así que la espera se rinde a los 0,5 s.
    real, arrived, both_here = receivables.open_receivables, 0, asyncio.Event()

    async def read_together(session, business_id, customer_id, broker_member_id=None):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_here.set()
        try:
            await asyncio.wait_for(both_here.wait(), 0.5)
        except TimeoutError:
            pass
        return await real(session, business_id, customer_id, broker_member_id)

    monkeypatch.setattr(receivables, "open_receivables", read_together)
    pay = RecordPayment(customer_name="Ana", amount=Decimal(10), currency=Currency.USD)
    await asyncio.gather(bus.dispatch(pay, panel_ctx), bus.dispatch(pay, panel_ctx))  # uno "por WhatsApp", otro "desde el panel"

    async with session_factory() as s:
        paid = dict((await s.execute(
            select(Receivable.description, func.coalesce(func.sum(PaymentAllocation.applied_amount), 0))
            .outerjoin(PaymentAllocation, PaymentAllocation.receivable_id == Receivable.id).group_by(Receivable.description)
        )).all())
    assert paid == {"lunes": Decimal("10"), "martes": Decimal("10")}  # sin el bloqueo: lunes=20, martes=0


async def test_payment_gives_up_politely_if_the_customer_stays_busy(bus, panel_ctx, session_factory, rates):
    from app.application.bus import CommandBus, CommandDeps
    from app.db.session import make_engine, make_session_factory

    await _two_open_receivables(bus, panel_ctx)
    impatient_engine = make_engine(os.environ["DATABASE_URL"], lock_timeout_ms=300)  # en producción son 5 s
    impatient = CommandBus(make_session_factory(impatient_engine), CommandDeps(rates=rates))
    try:
        async with session_factory() as holder:
            ana = (await holder.execute(select(Customer.id))).scalar_one()
            await lock_customers(holder, panel_ctx.business_id, ana)  # otra operación tiene a Ana "ocupada"
            with pytest.raises(Conflict, match="otro movimiento de este cliente"):
                await impatient.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
            await holder.rollback()
        # liberada, el mismo abono sí pasa
        ok = await impatient.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
        assert ok.status == "ok"
    finally:
        await impatient_engine.dispose()
