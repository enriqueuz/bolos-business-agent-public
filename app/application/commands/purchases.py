"""Registrar compras de mercancía. Los ítems son opcionales; si vienen, actualizan el costo de cada producto."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.common import (
    assume_currency,
    bump,
    get_business,
    legs_brief,
    real_cost_brief,
    require_fresh_charge_rate,
)
from app.application.context import EntityRef, ExecContext
from app.application.resolvers import get_or_create_product
from app.application.summaries import purchase_summary
from app.db.models import Purchase, PurchaseItem
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import AuditAction, Currency, EntityType, PaymentMethod
from app.domain.errors import ValidationFailed
from app.domain.money import compute_legs, fmt_amount, fmt_qty, q, sum_legs


class PurchaseItemInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    product: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(default=Decimal(1), gt=0)
    unit_cost: Decimal = Field(gt=0)
    currency: Currency | None = None
    # Solo lo pone una opción estructurada del sistema; no forma parte del schema de la tool del modelo.
    force_new_product: bool = False


class RecordPurchase(Command):
    kind: ClassVar[str] = "record_purchase"

    description: str | None = Field(default=None, max_length=300)
    amount: Decimal | None = Field(default=None, gt=0)
    currency: Currency | None = None
    supplier_name: str | None = Field(default=None, max_length=160)
    items: list[PurchaseItemInput] = Field(default_factory=list, max_length=100)
    payment_method: PaymentMethod | None = None
    business_date: date | None = None
    # Lo pone el botón "Mantener" de la pregunta diaria de la tasa propia (nunca el modelo ni el panel).
    charge_rate_confirmed: bool = False

    @model_validator(mode="after")
    def _amount_or_items(self) -> RecordPurchase:
        if self.amount is None and not self.items:
            raise ValueError("Indica el monto total de la compra o sus ítems con costo.")
        return self


@command_handler(RecordPurchase)
async def record_purchase(cmd: RecordPurchase, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    rates = await deps.rates.effective(session, ctx.business_id)
    purchase = Purchase(
        business_id=ctx.business_id,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        supplier_name=cmd.supplier_name.strip() if cmd.supplier_name else None,
        description="",
        payment_method=cmd.payment_method.value if cmd.payment_method else None,
        source_message_id=ctx.source_message_id,
        items=[],
    )
    item_infos: list[dict] = []
    assumed = False
    if cmd.items:
        lines = []
        for position, item in enumerate(cmd.items, start=1):
            product, created = await get_or_create_product(
                session,
                ctx.business_id,
                item.product,
                force_new=item.force_new_product,
                distinct_name_is_new=True,
            )
            currency, was_assumed = assume_currency(item.unit_cost, item.currency or cmd.currency, business)
            assumed = assumed or was_assumed
            line = compute_legs(item.unit_cost * item.quantity, currency, rates)
            lines.append(line)
            purchase.items.append(
                PurchaseItem(
                    position=position, product_id=product.id, description=product.name, quantity=item.quantity,
                    unit_cost_amount=q(item.unit_cost), unit_cost_currency=str(currency), item_amount=line.amount,
                    item_local=line.local, item_usd_official=line.usd_official, item_usd_market=line.usd_market,
                    item_usd_applied=line.usd_applied,
                )
            )
            previous_cost = (
                fmt_amount(product.last_unit_cost_amount, product.last_unit_cost_currency)
                if product.last_unit_cost_amount is not None else None
            )
            product.last_unit_cost_amount = q(item.unit_cost)
            product.last_unit_cost_currency = str(currency)
            product.last_cost_at = ctx.now
            bump(product)
            item_infos.append({
                "product": product.name, "quantity": fmt_qty(item.quantity),
                "unit_cost": fmt_amount(item.unit_cost, currency), "previous_unit_cost": previous_cost,
                "new_product": created,
            })
        legs = sum_legs(lines)
        mismatch = None
        if cmd.amount is not None:
            stated_currency = cmd.currency or legs.currency
            stated = compute_legs(cmd.amount, stated_currency, rates)
            if abs(stated.usd_applied - legs.usd_applied) > max(Decimal("0.5"), legs.usd_applied * Decimal("0.02")):
                mismatch = {"stated_total": fmt_amount(cmd.amount, stated_currency), "items_total": fmt_amount(legs.amount, legs.currency)}
        purchase.description = (cmd.description or ", ".join(f"{i['quantity']}x {i['product']}" for i in item_infos))[:300]
    else:
        currency, assumed = assume_currency(cmd.amount, cmd.currency, business)  # type: ignore[arg-type]
        legs = compute_legs(cmd.amount, currency, rates)  # type: ignore[arg-type]
        mismatch = None
        if not cmd.description:
            raise ValidationFailed("Indica qué se compró (description).", missing="description")
        purchase.description = cmd.description.strip()
    currencies = {str(i.unit_cost_currency) for i in purchase.items} | {legs.currency}
    await require_fresh_charge_rate(session, ctx, business, rates, cmd, involves_local=business.local_currency in currencies)
    purchase.set_amount_legs(legs)
    purchase.set_real_cost(legs, rates.funds)
    session.add(purchase)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.PURCHASE, entity_id=purchase.id, action=AuditAction.CREATE,
                 before=None, after=audit.snapshot(purchase), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={
            "purchase": {
                "description": purchase.description,
                "supplier": purchase.supplier_name,
                "business_date": bdate.isoformat(),
                "amount": legs_brief(legs),
                "items": item_infos,
                "payment_method": purchase.payment_method,
                "currency_assumed": assumed,
                "total_mismatch": mismatch,
                "real_cost": real_cost_brief(purchase, legs, business),
            }
        },
        affected=[EntityRef(EntityType.PURCHASE, purchase.id, version=purchase.version, summary=purchase_summary(purchase))],
    )
