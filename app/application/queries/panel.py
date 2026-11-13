"""Lecturas para el panel web (listas paginadas y detalles con ids y versiones)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.audit import json_safe
from app.application.common import customer_balance, get_business
from app.db.models import (
    AuditLog,
    BusinessMember,
    BusinessNote,
    CashSession,
    CurrencyExchange,
    Customer,
    CustomerPayment,
    CustomRate,
    ExchangeRate,
    Expense,
    Product,
    Purchase,
    Receivable,
    Sale,
)
from app.db.views import money_movements as mm
from app.db.views import receivable_balances as rb
from app.domain.countries import country_profile
from app.domain.customers import normalize_nickname
from app.domain.enums import EntityType, RatePurpose
from app.domain.errors import NotFound
from app.domain.text import normalize_name


def _money(amount, currency, local, usd_official, usd_market, usd_applied, rate_official=None, rate_market=None,
           rate_applied=None, source=None) -> dict[str, Any]:
    return json_safe({"amount": amount, "currency": currency, "local": local, "usd_official": usd_official,
                      "usd_market": usd_market, "usd_applied": usd_applied, "rate_official": rate_official,
                      "rate_market": rate_market, "rate_applied": rate_applied, "rate_applied_source": source})


class PanelQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def staff(self, business_id: UUID) -> list[dict]:
        async with self._factory() as session:
            rows = (await session.execute(
                select(BusinessMember).where(
                    BusinessMember.business_id == business_id,
                    BusinessMember.role.in_(["owner", "broker"]),
                ).order_by(BusinessMember.role.desc(), BusinessMember.name)
            )).scalars().all()
            return [{
                "id": str(row.id), "name": row.name or "", "role": row.role,
                "phone": row.phone, "abbreviation": row.abbreviation, "is_active": row.is_active,
            } for row in rows]

    async def movements(self, business_id: UUID, *, date_from: date | None, date_to: date | None, kind: str | None,
                        q: str | None, limit: int, offset: int, customer_id: UUID | None = None) -> dict[str, Any]:
        async with self._factory() as s:
            conds = [mm.c.business_id == business_id]
            if customer_id:
                conds.append(mm.c.customer_id == customer_id)
            if date_from:
                conds.append(mm.c.business_date >= date_from)
            if date_to:
                conds.append(mm.c.business_date <= date_to)
            if kind:
                conds.append(mm.c.kind == kind)
            if q:
                pattern = f"%{q.lower()}%"
                conds.append(or_(func.lower(mm.c.description).like(pattern), func.lower(func.coalesce(mm.c.customer_name, "")).like(pattern),
                    mm.c.customer_id.in_(select(Customer.id).where(Customer.business_id == business_id, Customer.nickname.contains(normalize_nickname(q) or "", autoescape=True)))))
            total = (await s.execute(select(func.count()).select_from(mm).where(*conds))).scalar_one()
            rows = (await s.execute(select(mm).where(*conds).order_by(mm.c.business_date.desc(), mm.c.created_at.desc())
                                    .limit(limit).offset(offset))).mappings().all()
        items = [{
            "kind": r["kind"], "id": str(r["id"]), "version": r["version"], "business_date": r["business_date"].isoformat(),
            "description": r["description"], "customer": r["customer_name"], "payment_method": r["payment_method"],
            "payment_status": r["payment_status"], "category": r["category"], "direction": r["direction"],
            "money": _money(r["amount"], r["currency"], r["amount_local"], r["amount_usd_official"], r["amount_usd_market"],
                            r["amount_usd_applied"], r["rate_official"], r["rate_market"], r["rate_applied"],
                            r["rate_applied_source"]),
        } for r in rows]
        return {"total": total, "items": items}

    async def record(self, business_id: UUID, entity_type: EntityType, entity_id: UUID) -> dict[str, Any]:
        model = {EntityType.SALE: Sale, EntityType.EXPENSE: Expense, EntityType.PURCHASE: Purchase,
                 EntityType.RECEIVABLE: Receivable, EntityType.PAYMENT: CustomerPayment,
                 EntityType.EXCHANGE: CurrencyExchange}.get(entity_type)
        if model is None:
            raise NotFound("Tipo no soportado")
        async with self._factory() as s:
            e = await s.get(model, entity_id)
            if e is None or e.business_id != business_id or e.deleted_at is not None:
                raise NotFound("Registro no encontrado")
            base: dict[str, Any] = {"id": str(e.id), "version": e.version, "business_date": e.business_date.isoformat(),
                                    "kind": entity_type.value}
            if isinstance(e, CurrencyExchange):
                # Cifra principal: el lado en $ del cambio, con su contraparte en moneda local a la tasa del cambio.
                usd, local = (e.out_amount, e.in_amount) if e.out_currency == "USD" else (e.in_amount, e.out_amount)
                base.update({
                    "description": e.description or "Cambio de moneda",
                    "money": _money(usd, "USD", local, usd, usd, usd, e.rate_official, e.rate_market, e.rate, "exchange"),
                    "exchange": json_safe({"out_amount": e.out_amount, "out_currency": e.out_currency, "out_method": e.out_method,
                                           "in_amount": e.in_amount, "in_currency": e.in_currency, "in_method": e.in_method,
                                           "rate": e.rate}),
                })
            elif isinstance(e, Sale):
                pending = (await s.execute(select(rb.c.remaining_amount, rb.c.currency).where(rb.c.sale_id == e.id))).first()
                base.update({
                    "customer": e.customer.display_name if e.customer else None, "payment_status": e.payment_status,
                    "payment_method": e.payment_method, "notes": e.notes, "paid_amount": str(e.paid_amount),
                    "paid_currency": e.paid_currency, "settlement": e.settlement,
                    # precio acordado (lista − descuento = neto) y dinero realmente recibido, cada uno en su moneda
                    "price": json_safe({"list_total": e.list_total_amount, "discount": e.discount_amount, "total": e.total_amount,
                                        "currency": e.total_currency, "discount_local": e.discount_local,
                                        "discount_usd_applied": e.discount_usd_applied}),
                    "paid": json_safe({"amount": e.paid_amount, "currency": e.paid_currency, "method": e.payment_method,
                                       "local": e.paid_local, "usd_applied": e.paid_usd_applied}),
                    "pending": json_safe({"amount": pending.remaining_amount, "currency": pending.currency})
                    if pending and pending.remaining_amount > Decimal("0.005") else None,
                    "money": _money(e.total_amount, e.total_currency, e.total_local, e.total_usd_official, e.total_usd_market,
                                    e.total_usd_applied, e.rate_official, e.rate_market, e.rate_applied, e.rate_applied_source),
                    "items": [{"position": i.position, "description": i.description, "quantity": str(i.quantity),
                               "unit_price": str(i.unit_price_amount), "currency": i.unit_price_currency, "price_source": i.price_source,
                               "item": _money(i.item_amount, i.item_currency, i.item_local, i.item_usd_official,
                                              i.item_usd_market, i.item_usd_applied),
                               "cost_known": i.cost_usd_official is not None} for i in e.items],
                })
            else:
                base["money"] = _money(e.amount, e.currency, e.amount_local, e.amount_usd_official, e.amount_usd_market,
                                       e.amount_usd_applied, e.rate_official, e.rate_market, e.rate_applied,
                                       e.rate_applied_source)
                for attr in ("description", "category", "payment_method", "supplier_name"):
                    if hasattr(e, attr):
                        base[attr] = getattr(e, attr)
                if isinstance(e, Purchase):
                    base["items"] = [{"position": i.position, "description": i.description, "quantity": str(i.quantity),
                                      "unit_cost": str(i.unit_cost_amount), "currency": i.unit_cost_currency} for i in e.items]
                if isinstance(e, Expense | Purchase):
                    # costo real (tasa real de los Bs) y diferencia cambiaria contra la oficial; calculada, no guardada
                    real = e.amount_usd_real if e.currency != "USD" else None
                    base["real_cost"] = json_safe({"usd": real, "rate_funds": e.rate_funds,
                                                   "fx_diff_usd": (real - e.amount_usd_official) if real is not None else None})
                if isinstance(e, Receivable | CustomerPayment):
                    base["customer"] = e.customer.display_name
            history = (await s.execute(select(AuditLog).where(AuditLog.entity_id == entity_id, AuditLog.business_id == business_id)
                                       .order_by(AuditLog.at))).scalars().all()
            base["history"] = [{"at": h.at.isoformat(), "action": h.action, "actor": h.actor_type, "command": h.command} for h in history]
        return base

    async def products(self, business_id: UUID, q: str | None) -> list[dict[str, Any]]:
        async with self._factory() as s:
            stmt = select(Product).where(Product.business_id == business_id, Product.deleted_at.is_(None))
            if q:
                stmt = stmt.where(Product.normalized_name.like(f"%{normalize_name(q)}%"))
            rows = (await s.execute(stmt.order_by(Product.name))).scalars().all()
            sold = dict((await s.execute(text_units_sold(), {"bid": business_id})).all())
        return [{"id": str(p.id), "version": p.version, "name": p.name, "sku": p.sku, "is_active": p.is_active,
                 "price_amount": json_safe(p.price_amount), "price_currency": p.price_currency,
                 "unit_cost_amount": json_safe(p.last_unit_cost_amount), "unit_cost_currency": p.last_unit_cost_currency,
                 "units_sold_30d": json_safe(sold.get(p.id, Decimal(0)))} for p in rows]

    async def customers(self, business_id: UUID, q: str | None) -> list[dict[str, Any]]:
        async with self._factory() as s:
            stmt = select(Customer).where(Customer.business_id == business_id, Customer.deleted_at.is_(None))
            if q:
                stmt = stmt.where(or_(Customer.normalized_name.contains(normalize_name(q), autoescape=True), Customer.nickname.contains(normalize_nickname(q) or "", autoescape=True)))
            rows = (await s.execute(stmt.order_by(Customer.name))).scalars().all()
            out = []
            for c in rows:
                out.append({"id": str(c.id), "version": c.version, "name": c.name, "nickname": c.nickname, "display_name": c.display_name, "phone": c.phone, "notes": c.notes,
                            "balance": await customer_balance(s, business_id, c.id)})
        return out

    async def customer_detail(self, business_id: UUID, customer_id: UUID) -> dict[str, Any]:
        async with self._factory() as s:
            c = await s.get(Customer, customer_id)
            if c is None or c.business_id != business_id or c.deleted_at is not None:
                raise NotFound("Cliente no encontrado")
            recs = (await s.execute(select(rb).where(rb.c.customer_id == customer_id).order_by(rb.c.business_date))).mappings().all()
            pays = (await s.execute(select(CustomerPayment).where(CustomerPayment.customer_id == customer_id,
                                                                  CustomerPayment.deleted_at.is_(None))
                                    .order_by(CustomerPayment.business_date.desc()))).scalars().all()
            balance = await customer_balance(s, business_id, customer_id)
            purchases = await self._customer_purchases(s, business_id, customer_id)
            rec_rows = []
            for r in recs:
                full = await s.get(Receivable, r["receivable_id"])
                rec_rows.append({"id": str(r["receivable_id"]), "version": full.version if full else None,
                                 "business_date": r["business_date"].isoformat(), "currency": r["currency"],
                                 "description": full.description if full else None, "from_sale": r["sale_id"] is not None,
                                 "sale_id": str(r["sale_id"]) if r["sale_id"] else None,
                                 "original": str(r["original_amount"]), "paid": str(r["paid_amount"]), "remaining": str(r["remaining_amount"])})
        return {"id": str(c.id), "version": c.version, "name": c.name, "nickname": c.nickname, "display_name": c.display_name, "phone": c.phone, "notes": c.notes, "balance": balance,
                "purchases": purchases, "receivables": rec_rows,
                "payments": [{"id": str(p.id), "version": p.version, "business_date": p.business_date.isoformat(),
                              "amount": str(p.amount), "currency": p.currency, "payment_method": p.payment_method,
                              "unapplied": str(p.unapplied_amount)} for p in pays]}

    @staticmethod
    async def _customer_purchases(s: AsyncSession, business_id: UUID, customer_id: UUID) -> dict[str, Any]:
        """Lo que este cliente se ha llevado: sus ventas (fiadas o no) y los fiados sueltos, sin venta detrás.
        La vista ya excluye los fiados que nacen de una venta, así que nada se cuenta dos veces."""
        row = (await s.execute(
            select(func.count(), func.max(mm.c.business_date), func.sum(mm.c.amount_local),
                   func.sum(mm.c.amount_usd_official), func.sum(mm.c.amount_usd_market), func.sum(mm.c.amount_usd_applied))
            .where(mm.c.business_id == business_id, mm.c.customer_id == customer_id,
                   mm.c.kind.in_(("sale", "receivable")))
        )).one()
        count, last, local, usd_official, usd_market, usd_applied = row
        currency = (await get_business(s, business_id)).local_currency
        zero = Decimal(0)
        return {"count": count, "last_date": last.isoformat() if last else None,
                "total": _money(local or zero, currency, local or zero, usd_official or zero, usd_market or zero,
                                usd_applied or zero)}

    async def notes(self, business_id: UUID) -> list[dict[str, Any]]:
        async with self._factory() as s:
            rows = (await s.execute(select(BusinessNote).where(BusinessNote.business_id == business_id, BusinessNote.deleted_at.is_(None))
                                    .order_by(BusinessNote.created_at.desc()))).scalars().all()
        return [{"id": str(n.id), "version": n.version, "kind": n.kind, "subject": n.subject_name, "text": n.text,
                 "created_at": n.created_at.isoformat(), "superseded": n.superseded_at is not None} for n in rows]

    async def cash_sessions(self, business_id: UUID, limit: int = 60) -> list[dict[str, Any]]:
        async with self._factory() as s:
            rows = (await s.execute(select(CashSession).where(CashSession.business_id == business_id)
                                    .order_by(CashSession.business_date.desc()).limit(limit))).scalars().all()
        return [{"business_date": c.business_date.isoformat(), "closed_at": c.closed_at.isoformat(), "closed_by": c.closed_by_actor_type,
                 "times_closed": c.times_closed, "summary": c.summary} for c in rows]

    async def rates_history(self, business_id: UUID) -> dict[str, Any]:
        async with self._factory() as s:
            own = (await s.execute(select(CustomRate).where(CustomRate.business_id == business_id)
                                   .order_by(CustomRate.set_at.desc()).limit(60))).scalars().all()
            local = (await get_business(s, business_id)).local_currency
            auto = (await s.execute(select(ExchangeRate).where(ExchangeRate.currency == local)
                                    .order_by(ExchangeRate.fetched_at.desc()).limit(40))).scalars().all()

        def rows(purpose: RatePurpose) -> list[dict[str, Any]]:
            return [{"rate": str(c.rate), "label": c.label, "source": c.source, "set_at": c.set_at.isoformat(),
                     "exchange_id": str(c.exchange_id) if c.exchange_id else None,
                     "cleared_at": c.cleared_at.isoformat() if c.cleared_at else None}
                    for c in own if c.purpose == purpose.value][:30]

        # custom = tasas propias para cobrar; funds = tasa real de los bolívares (manual o de un cambio)
        return {"custom": rows(RatePurpose.CHARGE), "funds": rows(RatePurpose.FUNDS),
                "auto": [{"currency": a.currency, "kind": a.kind, "provider": a.provider, "rate": str(a.rate), "fetched_at": a.fetched_at.isoformat()} for a in auto]}

    async def business(self, business_id: UUID) -> dict[str, Any]:
        async with self._factory() as s:
            b = await get_business(s, business_id)
        return {"id": str(b.id), "version": b.version, "vertical": b.vertical, "name": b.name, "what_sells": b.what_sells, "timezone": b.timezone,
                "country_code": b.country_code, "local_currency": b.local_currency,
                "profile": country_profile(b.country_code).to_public(),
                "display_currency": b.display_currency, "charge_rate_mode": b.charge_rate_mode,
                "payment_methods": b.payment_methods, "onboarding_state": b.onboarding_state}


def text_units_sold():
    from sqlalchemy import text

    return text("""SELECT si.product_id, sum(si.quantity) FROM sale_items si JOIN sales s ON s.id = si.sale_id
                   WHERE s.business_id = :bid AND s.deleted_at IS NULL AND s.business_date >= current_date - 30
                   AND si.product_id IS NOT NULL GROUP BY si.product_id""")
