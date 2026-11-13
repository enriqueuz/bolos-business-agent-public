"""Catálogo: precios, costos y mantenimiento de productos."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.common import bump, check_version, get_business
from app.application.context import EntityRef, ExecContext
from app.application.resolvers import find_product, get_or_create_product
from app.db.models import Product, PurchaseItem, SaleItem
from app.domain.enums import AuditAction, Currency, EntityType
from app.domain.errors import Conflict, NotFound
from app.domain.money import ensure_supported, fmt_amount, q
from app.domain.text import clean_display_name, normalize_name


def _price_text(amount: Decimal | None, currency: str | None) -> str | None:
    return fmt_amount(amount, currency) if amount is not None and currency else None


class SetProductPrice(Command):
    kind: ClassVar[str] = "set_product_price"

    product_name: str = Field(min_length=1, max_length=200)
    price: Decimal = Field(gt=0)
    currency: Currency
    # Campo interno para la opción determinista "Es un producto nuevo".
    force_new_product: bool = False


@command_handler(SetProductPrice)
async def set_product_price(cmd: SetProductPrice, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    ensure_supported(str(cmd.currency), (await get_business(session, ctx.business_id)).local_currency)
    product, created = await get_or_create_product(
        session, ctx.business_id, cmd.product_name, force_new=cmd.force_new_product, distinct_name_is_new=True
    )
    before = None if created else audit.snapshot(product)
    previous = _price_text(product.price_amount, product.price_currency)
    product.price_amount, product.price_currency = q(cmd.price), str(cmd.currency)
    if not created:
        bump(product)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.PRODUCT, entity_id=product.id,
                 action=AuditAction.CREATE if created else AuditAction.UPDATE, before=before,
                 after=audit.snapshot(product), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={"product": product.name, "price": fmt_amount(cmd.price, cmd.currency), "previous_price": previous, "created": created},
        affected=[EntityRef(EntityType.PRODUCT, product.id, version=product.version)],
    )


class SetProductCost(Command):
    kind: ClassVar[str] = "set_product_cost"

    product_name: str = Field(min_length=1, max_length=200)
    unit_cost: Decimal = Field(gt=0)
    currency: Currency
    # Campo interno para la opción determinista "Es un producto nuevo".
    force_new_product: bool = False


@command_handler(SetProductCost)
async def set_product_cost(cmd: SetProductCost, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    ensure_supported(str(cmd.currency), (await get_business(session, ctx.business_id)).local_currency)
    product, created = await get_or_create_product(
        session, ctx.business_id, cmd.product_name, force_new=cmd.force_new_product, distinct_name_is_new=True
    )
    before = None if created else audit.snapshot(product)
    previous = _price_text(product.last_unit_cost_amount, product.last_unit_cost_currency)
    product.last_unit_cost_amount, product.last_unit_cost_currency = q(cmd.unit_cost), str(cmd.currency)
    product.last_cost_at = ctx.now
    if not created:
        bump(product)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.PRODUCT, entity_id=product.id,
                 action=AuditAction.CREATE if created else AuditAction.UPDATE, before=before,
                 after=audit.snapshot(product), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={"product": product.name, "unit_cost": fmt_amount(cmd.unit_cost, cmd.currency), "previous_cost": previous,
              "created": created},
        affected=[EntityRef(EntityType.PRODUCT, product.id, version=product.version)],
    )


class UpsertProduct(Command):
    """Para el panel: crear o editar un producto con todos sus campos."""

    kind: ClassVar[str] = "upsert_product"

    product_id: UUID | None = None
    expected_version: int | None = None
    name: str = Field(min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=64)
    price_amount: Decimal | None = Field(default=None, gt=0)
    price_currency: Currency | None = None
    unit_cost_amount: Decimal | None = Field(default=None, gt=0)
    unit_cost_currency: Currency | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def money_needs_currency(self) -> UpsertProduct:
        for amount, currency in ((self.price_amount, self.price_currency), (self.unit_cost_amount, self.unit_cost_currency)):
            if (amount is None) != (currency is None):
                raise ValueError("Indica el monto y su moneda juntos.")
        return self


@command_handler(UpsertProduct)
async def upsert_product(cmd: UpsertProduct, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    for currency in (cmd.price_currency, cmd.unit_cost_currency):
        if currency is not None:
            ensure_supported(str(currency), business.local_currency)
    norm = normalize_name(cmd.name)
    clash = (
        await session.execute(
            select(Product).where(Product.business_id == ctx.business_id, Product.normalized_name == norm,
                                  Product.deleted_at.is_(None), Product.id != (cmd.product_id or UUID(int=0)))
        )
    ).scalar_one_or_none()
    if clash:
        raise Conflict(f"Ya existe un producto llamado '{clash.name}'.")
    if cmd.product_id:
        product = await session.get(Product, cmd.product_id)
        if product is None or product.business_id != ctx.business_id or product.deleted_at is not None:
            raise NotFound("Producto no encontrado.")
        check_version(product, cmd.expected_version, "producto")
        before = audit.snapshot(product)
        bump(product)
        action = AuditAction.UPDATE
    else:
        product = Product(business_id=ctx.business_id)
        session.add(product)
        before, action = None, AuditAction.CREATE
    product.name, product.normalized_name, product.sku = clean_display_name(cmd.name), norm, cmd.sku
    product.price_amount = q(cmd.price_amount) if cmd.price_amount else None
    product.price_currency = str(cmd.price_currency) if cmd.price_amount and cmd.price_currency else None
    if cmd.unit_cost_amount != product.last_unit_cost_amount:
        product.last_unit_cost_amount = q(cmd.unit_cost_amount) if cmd.unit_cost_amount else None
        product.last_cost_at = ctx.now if cmd.unit_cost_amount else None
    product.last_unit_cost_currency = str(cmd.unit_cost_currency) if cmd.unit_cost_amount and cmd.unit_cost_currency else None
    product.is_active = cmd.is_active
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.PRODUCT, entity_id=product.id, action=action,
                 before=before, after=audit.snapshot(product), command=cmd.kind)
    return CommandResult(kind=cmd.kind, data={"product_id": str(product.id), "version": product.version},
                         affected=[EntityRef(EntityType.PRODUCT, product.id, version=product.version)])


class DeleteProduct(Command):
    """Saca un producto del catálogo. Borrado suave: las ventas y compras ya registradas conservan su nombre."""

    kind: ClassVar[str] = "delete_product"

    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    product_id: UUID | None = None
    expected_version: int | None = None

    @model_validator(mode="after")
    def one_way_to_name_it(self) -> DeleteProduct:
        if (self.product_name is None) == (self.product_id is None):
            raise ValueError("Indica el producto por nombre o por id, no ambos.")
        return self


def _delete_risk(cmd: DeleteProduct, ctx: ExecContext, res: CommandResult) -> str:
    return "Voy a sacar este producto del catálogo."


async def _movements(session: AsyncSession, product_id: UUID) -> int:
    total = 0
    for model in (SaleItem, PurchaseItem):
        total += (await session.execute(
            select(func.count()).select_from(model).where(model.product_id == product_id)
        )).scalar_one()
    return total


@command_handler(DeleteProduct, risk=_delete_risk)
async def delete_product(cmd: DeleteProduct, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    if cmd.product_id:
        product = await session.get(Product, cmd.product_id)
        if product is None or product.business_id != ctx.business_id or product.deleted_at is not None:
            raise NotFound("Producto no encontrado.")
    else:
        product = await find_product(session, ctx.business_id, cmd.product_name or "")
        if product is None:
            raise NotFound(f"No tengo ningún producto llamado '{cmd.product_name}' en el catálogo.")
    check_version(product, cmd.expected_version, "producto")
    moved = await _movements(session, product.id)
    before = audit.snapshot(product)
    product.deleted_at = ctx.now
    product.is_active = False
    bump(product)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.PRODUCT, entity_id=product.id, action=AuditAction.DELETE,
                 before=before, after=None, command=cmd.kind)
    return CommandResult(kind=cmd.kind, data={"product": product.name, "movements": moved})
