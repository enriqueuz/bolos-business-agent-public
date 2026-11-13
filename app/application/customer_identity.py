"""Single write path for customer identity, shared by commands and automatic creation."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, Customer
from app.domain.customers import normalize_nickname, suggested_nickname
from app.domain.errors import Conflict, ValidationFailed
from app.domain.text import clean_display_name, normalize_name


async def lock_customer_identity(session: AsyncSession, business_id: UUID) -> None:
    # Also serializes against brokerage's existing business -> customer lock order.
    await session.execute(select(Business.id).where(Business.id == business_id).with_for_update())


async def assign_nickname(session: AsyncSession, customer: Customer, value: str | None, *, automatic: bool = False) -> str | None:
    nickname = normalize_nickname(value)
    if nickname and len(nickname) > 160:
        raise ValidationFailed("El apodo admite hasta 160 caracteres.")
    if nickname:
        exists = (await session.execute(select(Customer.id).where(
            Customer.business_id == customer.business_id, Customer.deleted_at.is_(None),
            Customer.nickname == nickname, Customer.id != customer.id,
        ).limit(1))).scalar_one_or_none()
        if exists:
            if automatic:
                return nickname
            # Do not disclose the identity or scope of a possibly hidden customer.
            raise Conflict(f"El apodo {nickname} ya está ocupado. Elige otro.")
    customer.nickname = nickname
    return None


async def create_customer(session: AsyncSession, business_id: UUID, *, name: str | None, nickname: str | None = None,
                          automatic: bool = True, classification: str | None = None, owner_broker_member_id: UUID | None = None) -> Customer:
    await lock_customer_identity(session, business_id)
    name = clean_display_name(name) if name and name.strip() else None
    if not name and not normalize_nickname(nickname):
        raise ValidationFailed("Indica al menos un nombre completo o un apodo.")
    customer = Customer(business_id=business_id, name=name, normalized_name=normalize_name(name or ""),
                        classification=classification, owner_broker_member_id=owner_broker_member_id)
    # UUID defaults are assigned on flush; set one now to exclude this same record from conflict checks.
    customer.id = uuid4()
    conflict = await assign_nickname(session, customer, suggested_nickname(name) if automatic and nickname is None else nickname,
                                     automatic=automatic and nickname is None)
    session.add(customer)
    await session.flush()
    session.info.setdefault("created_customers", []).append({
        "id": str(customer.id), "name": customer.name, "nickname": customer.nickname,
        "display_name": customer.display_name, "nickname_conflict": conflict,
    })
    return customer
