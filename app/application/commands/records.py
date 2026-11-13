"""Corregir y borrar cualquier registro.

Reglas:
- Al corregir un monto se conservan las TASAS ORIGINALES del registro (no las de hoy).
- Cambiar una venta fiada propaga al fiado asociado en la misma transacción.
- No se permite dejar un fiado por debajo de lo ya abonado, ni mover de cliente un fiado con abonos.
- Bloqueo optimista con `expected_version` (viene del handle, nunca del modelo).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.exchanges import restore_funds_after_exchange_removed
from app.application.commands.receivables import allocate_payment, reallocate_customer_payments
from app.application.common import (
    bump,
    check_version,
    customer_balance,
    get_business,
    lock_customers,
    receivable_paid,
)
from app.application.context import EntityRef, ExecContext
from app.application.resolvers import resolve_customer
from app.application.summaries import (
    exchange_summary,
    expense_summary,
    payment_summary,
    purchase_summary,
    receivable_summary,
    sale_summary,
)
from app.db.models import BusinessNote, CurrencyExchange, CustomerPayment, Expense, Purchase, Receivable, Sale
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import (
    ENTITY_LABELS,
    AuditAction,
    Currency,
    EntityType,
    ExpenseCategory,
    LinkRelation,
    PaymentMethod,
    PaymentStatus,
    RateAppliedSource,
    SaleSettlement,
)
from app.domain.errors import NotFound, ValidationFailed
from app.domain.money import (
    convert_between,
    fmt_amount,
    q,
    real_cost_usd,
    recompute_with_frozen_rates,
    sub_legs,
    sum_legs,
)

EDITABLE = {EntityType.SALE, EntityType.EXPENSE, EntityType.PURCHASE, EntityType.RECEIVABLE, EntityType.PAYMENT, EntityType.NOTE,
            EntityType.EXCHANGE}
MODELS = {
    EntityType.SALE: Sale, EntityType.EXPENSE: Expense, EntityType.PURCHASE: Purchase,
    EntityType.RECEIVABLE: Receivable, EntityType.PAYMENT: CustomerPayment, EntityType.NOTE: BusinessNote,
    EntityType.EXCHANGE: CurrencyExchange,
}
SENSITIVE_FIELDS = {"amount", "currency", "quantity", "unit_price", "customer_name", "payment_status", "amount_paid",
                    "paid_currency"}


class RecordChanges(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    amount: Decimal | None = Field(default=None, gt=0)
    currency: Currency | None = None
    description: str | None = Field(default=None, min_length=1, max_length=300)
    category: ExpenseCategory | None = None
    payment_method: PaymentMethod | None = None
    business_date: date | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=160)
    force_new_customer: bool = False
    supplier_name: str | None = Field(default=None, max_length=160)
    item_position: int | None = Field(default=None, ge=1, description="Ítem de la venta (1 = primero). Obligatorio si la venta tiene varios ítems.")
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, gt=0)
    payment_status: PaymentStatus | None = None
    amount_paid: Decimal | None = Field(default=None, ge=0)
    paid_currency: Currency | None = None
    notes: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, min_length=3, max_length=1000)

    def set_fields(self) -> set[str]:
        return {k for k, v in self.model_dump(exclude={"force_new_customer"}).items() if v is not None}


class EditRecord(Command):
    kind: ClassVar[str] = "edit_record"

    entity_type: EntityType
    entity_id: UUID
    expected_version: int | None = None
    changes: RecordChanges
    item_changes: list[RecordChanges] = Field(default_factory=list, max_length=50)


def _edit_risk(cmd: EditRecord, ctx: ExecContext, res: CommandResult) -> str | None:
    sensitive = (cmd.changes.set_fields() & SENSITIVE_FIELDS) or cmd.item_changes
    if sensitive and not ctx.target_is_quoted:
        return "Cambia montos o el cliente de un registro ya guardado."
    return None


async def _load(session: AsyncSession, ctx: ExecContext, etype: EntityType, eid: UUID) -> Any:
    if etype not in EDITABLE:
        raise ValidationFailed(f"No se puede modificar un registro de tipo {etype.value} por aquí.")
    entity = await session.get(MODELS[etype], eid)
    if entity is None or entity.business_id != ctx.business_id or entity.deleted_at is not None:
        raise NotFound(f"Ese {ENTITY_LABELS[etype]} ya no existe (quizás fue borrado).")
    return entity


def _reject_unsupported(etype: EntityType, fields: set[str], allowed: set[str]) -> None:
    bad = fields - allowed
    if bad:
        raise ValidationFailed(
            f"Para un {ENTITY_LABELS[etype]} no se puede cambiar: {', '.join(sorted(bad))}.",
            allowed_fields=sorted(allowed),
        )


def _frozen(entity: Any, amount: Decimal, currency: str, local_currency: str):
    """Recalcula patas con las tasas ORIGINALES del registro (una corrección no usa las tasas de hoy)."""
    return recompute_with_frozen_rates(amount, currency, local_currency, entity.rate_official, entity.rate_market,
                                       entity.rate_applied, RateAppliedSource(entity.rate_applied_source))


async def _apply_date(session: AsyncSession, ctx: ExecContext, entity: Any, new_date: date) -> None:
    business = await get_business(session, ctx.business_id)
    d = validate_business_date(new_date, business.timezone, ctx.now)
    entity.business_date = d
    entity.occurred_at = occurred_at_for(d, business.timezone, ctx.now)


async def _edit_sale(session: AsyncSession, ctx: ExecContext, sale: Sale, ch: RecordChanges, fields: set[str],
                     item_changes: list[RecordChanges]) -> dict:
    _reject_unsupported(EntityType.SALE, fields, {"customer_name", "payment_method", "business_date", "item_position",
                                                  "quantity", "unit_price", "payment_status", "amount_paid", "paid_currency",
                                                  "notes", "description"})
    receivable = (await session.execute(
        select(Receivable).where(Receivable.sale_id == sale.id, Receivable.deleted_at.is_(None))
    )).scalar_one_or_none()
    old_customer_id = sale.customer_id
    local = (await get_business(session, ctx.business_id)).local_currency

    edits = ([ch] if "quantity" in fields or "unit_price" in fields else []) + item_changes
    for item_change in edits:
        if item_change.item_position is None and len(sale.items) > 1:
            raise ValidationFailed("La venta tiene varios ítems; indica cuál (item_position).",
                                   items=[f"{i.position}: {i.description}" for i in sale.items])
        position = item_change.item_position or 1
        item = next((i for i in sale.items if i.position == position), None)
        if item is None:
            raise ValidationFailed(f"La venta no tiene ítem {position}.")
        if item_change.quantity is not None:
            old_qty = item.quantity
            item.quantity = item_change.quantity
            if item.cost_local is not None and old_qty:
                ratio = item_change.quantity / old_qty
                item.cost_local, item.cost_usd_official, item.cost_usd_market = (
                    q(item.cost_local * ratio), q(item.cost_usd_official * ratio), q(item.cost_usd_market * ratio))
                if item.cost_usd_applied is not None:
                    item.cost_usd_applied = q(item.cost_usd_applied * ratio)
        if item_change.unit_price is not None:
            item.unit_price_amount = q(item_change.unit_price)
            item.price_source = "stated"
        line = _frozen(sale, item.unit_price_amount * item.quantity, item.unit_price_currency, local)
        item.item_amount, item.item_currency = line.amount, str(line.currency)
        item.item_local, item.item_usd_official, item.item_usd_market = line.local, line.usd_official, line.usd_market
        item.item_usd_applied = line.usd_applied
    if edits:
        lines = [_frozen(sale, i.unit_price_amount * i.quantity, i.unit_price_currency, local) for i in sale.items]
        list_total = sum_legs(lines)
        # el descuento se conserva (en la moneda del total), nunca mayor que el nuevo precio de lista
        discount = min(sale.discount_amount, list_total.amount) if sale.discount_amount else Decimal(0)
        discount_legs = _frozen(sale, discount, list_total.currency, local)
        sale.list_total_amount = list_total.amount
        sale.set_total(sub_legs(list_total, discount_legs))
        sale.set_discount(discount_legs)

    if "customer_name" in fields:
        customer, _ = await resolve_customer(session, ctx.business_id, ch.customer_name, create_if_missing=True,  # type: ignore[arg-type]
                                             force_new=ch.force_new_customer)
        sale.customer_id = customer.id
        sale.customer = customer
    if "payment_method" in fields:
        sale.payment_method = ch.payment_method.value  # type: ignore[union-attr]
    if "notes" in fields or "description" in fields:
        sale.notes = ch.notes or ch.description
    if "business_date" in fields:
        await _apply_date(session, ctx, sale, ch.business_date)  # type: ignore[arg-type]

    paid_amount, paid_currency = _edited_payment(sale, ch, fields)
    # lo que falta, en la moneda del precio, con la tasa congelada de la venta
    paid_in_price = convert_between(paid_amount, paid_currency, sale.total_currency, sale.rate_applied)
    pending = q(sale.total_amount - paid_in_price)
    if pending <= Decimal("0.005"):
        pending = Decimal(0)
    sale.set_paid(_frozen(sale, paid_amount, paid_currency, local))
    sale.payment_status = (PaymentStatus.PAID if pending == 0 else
                           PaymentStatus.CREDIT if paid_amount == 0 else PaymentStatus.PARTIAL).value
    sale.settlement = (SaleSettlement.DEBT if pending > 0 else
                       SaleSettlement.DISCOUNT if sale.discount_amount else SaleSettlement.EXACT).value
    if pending > 0 and sale.customer_id is None:
        raise ValidationFailed("Una venta fiada necesita cliente.", missing="customer_name")
    if pending > 0:
        legs = _frozen(sale, pending, sale.total_currency, local)
        if receivable is None:
            receivable = Receivable(business_id=sale.business_id, customer_id=sale.customer_id, sale_id=sale.id,
                                    business_date=sale.business_date, occurred_at=sale.occurred_at,
                                    description=", ".join(i.description for i in sale.items)[:300])
            session.add(receivable)
        else:
            bump(receivable)
        receivable.set_amount_legs(legs)
        receivable.customer_id = sale.customer_id
        receivable.business_date, receivable.occurred_at = sale.business_date, sale.occurred_at
    elif receivable is not None:
        receivable.deleted_at = ctx.now
        bump(receivable)
    await session.flush()
    # Los abonos son dinero real ya recibido: nunca se bloquea la corrección. Se reasignan FIFO en los clientes
    # afectados; lo que ya no tenga a qué aplicarse queda como saldo a favor (y el recibo lo muestra).
    await reallocate_customer_payments(session, ctx, *{c for c in (old_customer_id, sale.customer_id) if c})
    await session.refresh(sale, ["customer"])
    result: dict[str, Any] = {"summary": sale_summary(sale)}
    if sale.customer_id:
        result["customer_balance"] = await customer_balance(session, ctx.business_id, sale.customer_id)
    if old_customer_id and old_customer_id != sale.customer_id:
        result["previous_customer_balance"] = await _named_balance(session, ctx, old_customer_id)
    return result


def _edited_payment(sale: Sale, ch: RecordChanges, fields: set[str]) -> tuple[Decimal, str]:
    """Lo que el cliente pagó tras la corrección, en su moneda. Con tasa congelada de la venta; nunca se inventa otra."""
    currency = str(ch.paid_currency or sale.paid_currency)
    status = ch.payment_status or PaymentStatus(sale.payment_status)
    full = convert_between(sale.total_amount, sale.total_currency, currency, sale.rate_applied)
    if status == PaymentStatus.CREDIT:
        return Decimal(0), currency
    if status == PaymentStatus.PAID:
        if ch.amount_paid is not None and ch.amount_paid < full - Decimal("0.01"):
            raise ValidationFailed(
                f"Con {fmt_amount(ch.amount_paid, currency)} no se cubre el total ({fmt_amount(full, currency)}). Si quedó "
                "debiendo el resto usa payment_status=partial.")
        return full, currency
    if "amount_paid" in fields or "paid_currency" in fields or "payment_status" in fields:
        paid = ch.amount_paid if ch.amount_paid is not None else sale.paid_amount
    else:
        paid = min(sale.paid_amount, full)  # cambió el precio: lo cobrado sigue siendo lo que entró
    if paid <= 0:
        raise ValidationFailed("Para un pago parcial indica cuánto abonó en el momento (amount_paid).", missing="amount_paid")
    if paid >= full:
        raise ValidationFailed(f"El abono ({fmt_amount(paid, currency)}) no es menor que el total ({fmt_amount(full, currency)}); "
                               "entonces la venta está pagada.")
    return q(paid), currency


async def _named_balance(session: AsyncSession, ctx: ExecContext, customer_id: UUID) -> dict[str, Any]:
    from app.db.models import Customer

    customer = await session.get(Customer, customer_id)
    return {"customer": customer.display_name if customer else None, **await customer_balance(session, ctx.business_id, customer_id)}


async def _edit_simple_amount(session: AsyncSession, ctx: ExecContext, entity: Any, ch: RecordChanges) -> None:
    if ch.amount is not None or ch.currency is not None:
        amount = ch.amount if ch.amount is not None else entity.amount
        currency = str(ch.currency or entity.currency)
        local = (await get_business(session, ctx.business_id)).local_currency
        legs = _frozen(entity, amount, currency, local)
        entity.set_amount_legs(legs)
        if hasattr(entity, "set_real_cost"):
            # la tasa real también queda congelada: la del día del gasto (si era en $ y pasa a Bs, no se inventa)
            entity.amount_usd_real = real_cost_usd(amount, currency, local, entity.rate_funds)


async def _edit(session: AsyncSession, ctx: ExecContext, etype: EntityType, entity: Any, ch: RecordChanges,
                item_changes: list[RecordChanges]) -> dict:
    fields = ch.set_fields()
    if not fields and not item_changes:
        raise ValidationFailed("No indicaste ningún cambio.")
    if etype == EntityType.SALE:
        return await _edit_sale(session, ctx, entity, ch, fields, item_changes)
    if etype == EntityType.EXPENSE:
        _reject_unsupported(etype, fields, {"amount", "currency", "description", "category", "payment_method", "business_date"})
        await _edit_simple_amount(session, ctx, entity, ch)
        if ch.description:
            entity.description = ch.description
        if ch.category:
            entity.category = ch.category.value
        if ch.payment_method:
            entity.payment_method = ch.payment_method.value
        if ch.business_date:
            await _apply_date(session, ctx, entity, ch.business_date)
        return {"summary": expense_summary(entity)}
    if etype == EntityType.PURCHASE:
        _reject_unsupported(etype, fields, {"amount", "currency", "description", "supplier_name", "payment_method", "business_date"})
        if entity.items and (ch.amount is not None or ch.currency is not None):
            raise ValidationFailed("Esta compra tiene ítems con costo. Para corregir sus montos, bórrala y vuelve a registrarla.")
        await _edit_simple_amount(session, ctx, entity, ch)
        if ch.description:
            entity.description = ch.description
        if ch.supplier_name is not None:
            entity.supplier_name = ch.supplier_name or None
        if ch.payment_method:
            entity.payment_method = ch.payment_method.value
        if ch.business_date:
            await _apply_date(session, ctx, entity, ch.business_date)
        return {"summary": purchase_summary(entity)}
    if etype == EntityType.RECEIVABLE:
        if entity.sale_id:
            raise ValidationFailed("Este fiado viene de una venta: corrige la venta y el fiado se ajusta solo.",
                                   hint="edita la venta asociada")
        _reject_unsupported(etype, fields, {"amount", "currency", "description", "customer_name", "business_date"})
        old_customer_id = entity.customer_id
        await _edit_simple_amount(session, ctx, entity, ch)
        if ch.customer_name:
            customer, _ = await resolve_customer(session, ctx.business_id, ch.customer_name, create_if_missing=True,
                                                 force_new=ch.force_new_customer)
            entity.customer_id, entity.customer = customer.id, customer
        if ch.description:
            entity.description = ch.description
        if ch.business_date:
            await _apply_date(session, ctx, entity, ch.business_date)
        await session.flush()
        await reallocate_customer_payments(session, ctx, *{old_customer_id, entity.customer_id})
        extra = {}
        if old_customer_id != entity.customer_id:
            extra["previous_customer_balance"] = await _named_balance(session, ctx, old_customer_id)
        return {**extra, "summary": receivable_summary(entity),
                "customer_balance": await customer_balance(session, ctx.business_id, entity.customer_id)}
    if etype == EntityType.PAYMENT:
        _reject_unsupported(etype, fields, {"amount", "currency", "payment_method", "business_date", "customer_name"})
        await _edit_simple_amount(session, ctx, entity, ch)
        if ch.payment_method:
            entity.payment_method = ch.payment_method.value
        if ch.business_date:
            await _apply_date(session, ctx, entity, ch.business_date)
        old_customer = entity.customer_id
        if ch.customer_name:
            customer, _ = await resolve_customer(session, ctx.business_id, ch.customer_name, create_if_missing=False)
            entity.customer_id, entity.customer = customer.id, customer
        # los dos clientes juntos y en orden fijo: si se bloquearan uno por paso, dos correcciones cruzadas podrían trabarse
        await lock_customers(session, ctx.business_id, old_customer, entity.customer_id)
        await session.flush()
        applied = await allocate_payment(session, entity)
        await session.flush()
        out = {"summary": payment_summary(entity), "applied_to": applied,
               "customer_balance": await customer_balance(session, ctx.business_id, entity.customer_id)}
        if old_customer != entity.customer_id:
            await reallocate_customer_payments(session, ctx, old_customer)
        return out
    if etype == EntityType.NOTE:
        _reject_unsupported(etype, fields, {"text"})
        entity.text = ch.text
        return {"summary": f"nota | {entity.text[:80]}"}
    if etype == EntityType.EXCHANGE:
        if fields & {"amount", "currency"}:
            raise ValidationFailed("Para corregir los montos de un cambio, bórralo y vuelve a registrarlo (así se recalcula "
                                   "la tasa real de tus bolívares).", hint="delete_record y luego record_exchange")
        _reject_unsupported(etype, fields, {"description", "business_date"})
        if ch.description:
            entity.description = ch.description
        if ch.business_date:
            await _apply_date(session, ctx, entity, ch.business_date)
        return {"summary": exchange_summary(entity)}
    raise ValidationFailed("Tipo de registro no soportado.")


@command_handler(EditRecord, risk=_edit_risk)
async def edit_record(cmd: EditRecord, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    entity = await _load(session, ctx, cmd.entity_type, cmd.entity_id)
    check_version(entity, cmd.expected_version, ENTITY_LABELS[cmd.entity_type])
    etype = cmd.entity_type
    if etype == EntityType.RECEIVABLE and entity.sale_id and cmd.changes.set_fields() <= {"customer_name", "business_date"}:
        # El fiado de una venta se corrige corrigiendo la venta (el fiado se ajusta solo): redirigimos.
        entity, etype = await _load(session, ctx, EntityType.SALE, entity.sale_id), EntityType.SALE
    before = audit.snapshot(entity)
    before_summary = await _summary_of(etype, entity)
    if cmd.item_changes and etype != EntityType.SALE:
        raise ValidationFailed("Solo las ventas permiten corregir varios ítems.")
    for change in cmd.item_changes:
        _reject_unsupported(etype, change.set_fields(), {"item_position", "quantity", "unit_price"})
    data = await _edit(session, ctx, etype, entity, cmd.changes, cmd.item_changes)
    bump(entity)
    await session.flush()
    audit.record(session, ctx, entity_type=etype, entity_id=entity.id, action=AuditAction.UPDATE, before=before,
                 after=audit.snapshot(entity), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={"entity": ENTITY_LABELS[etype], "before": before_summary, "after": data.pop("summary"), **data},
        affected=[EntityRef(etype, entity.id, LinkRelation.UPDATED, version=entity.version)],
    )


async def _summary_of(etype: EntityType, entity: Any) -> str:
    return {
        EntityType.SALE: lambda e: sale_summary(e),
        EntityType.EXPENSE: lambda e: expense_summary(e),
        EntityType.PURCHASE: lambda e: purchase_summary(e),
        EntityType.RECEIVABLE: lambda e: receivable_summary(e),
        EntityType.PAYMENT: lambda e: payment_summary(e),
        EntityType.NOTE: lambda e: f"nota | {e.text[:80]}",
        EntityType.EXCHANGE: lambda e: exchange_summary(e),
    }[etype](entity)


class DeleteRecord(Command):
    kind: ClassVar[str] = "delete_record"

    entity_type: EntityType
    entity_id: UUID
    expected_version: int | None = None


def _delete_risk(cmd: DeleteRecord, ctx: ExecContext, res: CommandResult) -> str | None:
    return f"Voy a borrar este {ENTITY_LABELS[cmd.entity_type]}: {res.data.get('deleted')}"


@command_handler(DeleteRecord, risk=_delete_risk)
async def delete_record(cmd: DeleteRecord, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    entity = await _load(session, ctx, cmd.entity_type, cmd.entity_id)
    check_version(entity, cmd.expected_version, ENTITY_LABELS[cmd.entity_type])
    summary = await _summary_of(cmd.entity_type, entity)
    before = audit.snapshot(entity)
    extra: dict[str, Any] = {}
    customer_to_rebalance: UUID | None = None
    if cmd.entity_type == EntityType.SALE:
        receivable = (await session.execute(
            select(Receivable).where(Receivable.sale_id == entity.id, Receivable.deleted_at.is_(None))
        )).scalar_one_or_none()
        if receivable:
            if await receivable_paid(session, receivable.id) > 0:
                extra["note"] = "Esa venta tenía abonos: quedaron como saldo a favor del cliente."
            receivable.deleted_at = ctx.now
            bump(receivable)
            customer_to_rebalance = receivable.customer_id
    elif cmd.entity_type == EntityType.RECEIVABLE:
        if entity.sale_id:
            raise ValidationFailed("Este fiado viene de una venta: borra la venta (o márcala como pagada).")
        if await receivable_paid(session, entity.id) > 0:
            extra["note"] = "Ese fiado tenía abonos: quedaron como saldo a favor del cliente."
        customer_to_rebalance = entity.customer_id
    elif cmd.entity_type == EntityType.PAYMENT:
        customer_to_rebalance = entity.customer_id
    elif cmd.entity_type == EntityType.EXCHANGE:
        entity.deleted_at = ctx.now
        await session.flush()
        if note := await restore_funds_after_exchange_removed(session, ctx, entity):
            extra["note"] = note
    entity.deleted_at = ctx.now
    bump(entity)
    await session.flush()
    if customer_to_rebalance:
        await reallocate_customer_payments(session, ctx, customer_to_rebalance)
    customer_id = getattr(entity, "customer_id", None)
    if customer_id and cmd.entity_type in {EntityType.SALE, EntityType.RECEIVABLE, EntityType.PAYMENT}:
        extra["customer_balance"] = await customer_balance(session, ctx.business_id, customer_id)
    audit.record(session, ctx, entity_type=cmd.entity_type, entity_id=entity.id, action=AuditAction.DELETE,
                 before=before, after=None, command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={"entity": ENTITY_LABELS[cmd.entity_type], "deleted": summary, **extra},
        affected=[EntityRef(cmd.entity_type, entity.id, LinkRelation.UPDATED, version=entity.version)],
    )
