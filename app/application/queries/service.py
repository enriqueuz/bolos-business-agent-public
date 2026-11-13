"""Lecturas del negocio. Lo usan igual las tools del agente y el panel.

Devuelven DTOs chicos (dicts JSON-safe). Los ids reales solo salen en `refs` (EntityRef) para que la capa
del agente los convierta en handles efímeros; el texto que ve el modelo nunca los incluye.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.common import customer_balance, get_business, profile_of, receivable_paid
from app.application.context import EntityRef
from app.application.metrics.compiler import MetricsQuery, run_metrics_query
from app.application.queries.cash import cash_summary
from app.application.rates import RateService
from app.application.resolvers import resolve_customer, search_products
from app.application.summaries import (
    exchange_summary,
    expense_summary,
    payment_summary,
    purchase_summary,
    receivable_summary,
    sale_summary,
)
from app.db.models import (
    BrokerMovement,
    Business,
    BusinessMember,
    BusinessNote,
    CashSession,
    CurrencyExchange,
    Customer,
    CustomerBalanceAdjustment,
    CustomerPayment,
    Expense,
    InboundMessage,
    Product,
    Purchase,
    Receivable,
    Sale,
    SaleItem,
)
from app.domain.currencies import USD
from app.domain.dates import PeriodName, days_ago_label, fmt_date, fmt_short_date, local_today, resolve_period
from app.domain.enums import CurrencyView, EntityType, LinkRelation, MemberRole, NoteKind
from app.domain.errors import ValidationFailed
from app.domain.money import compute_legs, fmt_amount, fmt_rate, fmt_view, q
from app.domain.text import normalize_name


@dataclass(slots=True)
class QueryResult:
    data: dict[str, Any]
    refs: list[EntityRef]


def broker_movement_summary(row: BrokerMovement) -> str:
    label = "Ajuste de saldo" if row.kind == "adjustment" else "pago entregado" if row.kind == "customer_payout" else "abono de cliente" if row.related_payment_id else "abono a cuenta"
    if row.kind == "operation":
        label = "compra" if row.operation_type == "buy" else "venta"
    account = row.broker.name if row.broker and row.broker.name else "Casa"
    amount = row.operation_amount if row.kind == "operation" else (
        row.related_payment.amount if row.related_payment is not None else row.customer_payout_amount if row.kind == "customer_payout" else row.balance_delta
    )
    return f"{label} | {account} | {fmt_amount(amount, 'USD')}"


def customer_adjustment_summary(row: CustomerBalanceAdjustment) -> str:
    return f"Ajuste de saldo | {row.customer.display_name} | {fmt_amount(row.amount_delta, 'USD')} | {row.business_date}"


class QueryService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], rates: RateService) -> None:
        self._factory = session_factory
        self._rates = rates

    # ------------------------------------------------------------------ negocio

    async def business(self, business_id: UUID) -> Business:
        async with self._factory() as s:
            return await get_business(s, business_id)

    async def rates_info(self, business_id: UUID, now_utc) -> dict[str, Any]:
        snap = await self._rates.safe_snapshot(business_id)
        if snap is None:
            return {"available": False}
        async with self._factory() as s:
            business = await get_business(s, business_id)
        today = local_today(business.timezone, now_utc)

        tz = ZoneInfo(business.timezone)

        def age(dt) -> str | None:
            return days_ago_label(dt.astimezone(tz).date(), today) if dt else None

        profile = profile_of(business)
        local = business.local_currency

        def rate(value: Decimal) -> str:
            return fmt_rate(value, local)

        out: dict[str, Any] = {
            "available": bool(snap.official),
            "local_currency": local,
            "unit": f"{profile.local.symbol} por $",
            "official_label": profile.official_rate_label,
            "market_label": profile.market_rate_label,
            "has_market_rate": profile.has_market_rate,
            "official": {"rate": rate(snap.official), "as_of": age(snap.official_as_of)} if snap.official else None,
            "market_auto": ({"rate": rate(snap.market_auto), "as_of": age(snap.market_auto_as_of)}
                            if snap.market_auto and profile.has_market_rate else None),
        }
        if snap.custom:
            out["custom"] = {"rate": rate(snap.custom), "label": snap.custom_label, "set": age(snap.custom_set_at),
                             "note": "Tasa propia del dueño para cobrar."}
        eff = snap.effective()
        # Tasa para cobrar: la que se congela como `rate_applied` en cada movimiento nuevo.
        confirmed = snap.charge_rate_confirmed_on
        out["applied"] = {"rate": rate(eff.applied), "source": eff.applied_source.value, "mode": snap.charge_rate_mode.value,
                          "label": profile.applied_rate_label(eff.applied_source),
                          "confirmed": days_ago_label(confirmed, today) if confirmed else None}
        # Tasa real de los bolívares: a cuánto los obtuvo el negocio (solo para el costo real de lo que paga en moneda local).
        if snap.funds:
            out["funds"] = {"rate": rate(snap.funds), "set": age(snap.funds_set_at),
                            "source": snap.funds_source.value if snap.funds_source else None,
                            "note": "Tasa real de sus bolívares; no cambia lo que cobra."}
        return out

    async def today_line(self, business_id: UUID, now_utc) -> str:
        """Una línea con lo del día, para el contexto del agente."""
        async with self._factory() as s:
            business = await get_business(s, business_id)
            today = local_today(business.timezone, now_utc)
            view = CurrencyView(business.display_currency)
            summary = await cash_summary(s, business_id, today, view)
            closed = (await s.execute(select(CashSession.closed_at).where(
                CashSession.business_id == business_id, CashSession.business_date == today))).scalar_one_or_none()
        return (f"Hoy ({fmt_date(today)}): {summary['sales']['count']} ventas por {summary['sales']['total']}, "
                f"gastos {summary['expenses']['total']}, compras {summary['purchases']['total']}, "
                f"abonos {summary['payments_in']['total']}. Caja {'CERRADA' if closed else 'abierta'}.")

    # ------------------------------------------------------------------ fiado

    async def customer_balance(self, business_id: UUID, customer_name: str) -> QueryResult:
        async with self._factory() as s:
            customer, _ = await resolve_customer(s, business_id, customer_name, create_if_missing=False)
            balance = await customer_balance(s, business_id, customer.id)
            receivables = (await s.execute(
                select(Receivable).where(Receivable.business_id == business_id, Receivable.customer_id == customer.id,
                                         Receivable.deleted_at.is_(None)).order_by(Receivable.business_date)
            )).scalars().all()
            open_items, refs = [], []
            for r in receivables:
                remaining = r.amount - await receivable_paid(s, r.id)
                if remaining > Decimal("0.005"):
                    open_items.append({"date": fmt_short_date(r.business_date), "description": r.description or "fiado",
                                       "original": fmt_amount(r.amount, r.currency), "remaining": fmt_amount(remaining, r.currency)})
                    refs.append(EntityRef(EntityType.RECEIVABLE, r.id, LinkRelation.SHOWN, r.version, receivable_summary(r)))
            last_payment = (await s.execute(
                select(CustomerPayment).where(CustomerPayment.business_id == business_id, CustomerPayment.customer_id == customer.id,
                                              CustomerPayment.deleted_at.is_(None))
                .order_by(CustomerPayment.business_date.desc(), CustomerPayment.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            today = local_today((await get_business(s, business_id)).timezone)
        data = {
            "customer": customer.display_name,
            "balance": balance,
            "open_items": open_items[:15],
            "last_payment": {"date": fmt_short_date(last_payment.business_date), "ago": days_ago_label(last_payment.business_date, today),
                             "amount": fmt_amount(last_payment.amount, last_payment.currency)} if last_payment else None,
        }
        return QueryResult(data, refs)

    async def list_receivables(self, business_id: UUID, limit: int = 10) -> QueryResult:
        async with self._factory() as s:
            business = await get_business(s, business_id)
            view = CurrencyView(business.display_currency)
            leg = f"amount_{view.value}"
            sql = text(f"""
                SELECT COALESCE(c.name, c.nickname), r.customer_id,
                       sum(r.{leg} * (rb.remaining_amount / nullif(r.amount, 0))) AS owed_leg,
                       min(r.business_date) FILTER (WHERE rb.remaining_amount > 0.005) AS oldest
                FROM receivables r
                JOIN receivable_balances rb ON rb.receivable_id = r.id
                JOIN customers c ON c.id = r.customer_id
                WHERE r.business_id = :bid AND r.deleted_at IS NULL AND rb.remaining_amount > 0.005
                GROUP BY COALESCE(c.name, c.nickname), r.customer_id
                ORDER BY owed_leg DESC
            """)
            rows = (await s.execute(sql, {"bid": business_id})).all()
            today = local_today(business.timezone)
            customers = []
            total = Decimal(0)
            for name, customer_id, owed_leg, oldest in rows:
                total += Decimal(owed_leg or 0)
                bal = await customer_balance(s, business_id, customer_id)
                customers.append({"customer": name, "owes": bal["display"], "in_view": fmt_view(Decimal(owed_leg or 0), view, business.local_currency),
                                  "oldest_debt": days_ago_label(oldest, today) if oldest else None})
        return QueryResult({"customers_with_debt": len(customers), "total_owed": fmt_view(q(total), view, business.local_currency),
                            "currency_view": view.value, "top": customers[:limit]}, [])

    # ------------------------------------------------------------------ catálogo

    async def product_prices(self, business_id: UUID, query: str) -> QueryResult:
        async with self._factory() as s:
            matches = await search_products(s, business_id, query, limit=5)
            rates = (await self._rates.snapshot(business_id, s)).effective()
            items = []
            for m in matches:
                p: Product = m.entity
                item: dict[str, Any] = {"product": p.name, "price": None, "unit_cost": None}
                if p.price_amount is not None and p.price_currency:
                    item["price"] = fmt_amount(p.price_amount, p.price_currency)
                    if rates.official > 0:
                        legs = compute_legs(p.price_amount, p.price_currency, rates)
                        item["price_equivalent"] = (fmt_amount(legs.local, legs.local_currency) if p.price_currency == USD
                                                    else fmt_amount(legs.usd_applied, USD))
                if p.last_unit_cost_amount is not None and p.last_unit_cost_currency:
                    item["unit_cost"] = fmt_amount(p.last_unit_cost_amount, p.last_unit_cost_currency)
                items.append(item)
        return QueryResult({"matches": items, "note": None if items else "No hay productos parecidos en el catálogo."}, [])

    # ------------------------------------------------------------------ búsqueda de registros (fuente de handles)

    async def find_records(
        self, business_id: UUID, *, kind: EntityType | None = None, customer_name: str | None = None, product: str | None = None,
        text_query: str | None = None, period: PeriodName | None = None, date_from: date | None = None, date_to: date | None = None,
        amount: Decimal | None = None, limit: int = 10,
    ) -> QueryResult:
        limit = max(1, min(limit, 15))
        async with self._factory() as s:
            business = await get_business(s, business_id)
            today = local_today(business.timezone)
            rng = resolve_period(period, today, date_from, date_to) if (period or date_from) else resolve_period(PeriodName.LAST_30_DAYS, today)
            kinds = [kind] if kind else [EntityType.SALE, EntityType.EXPENSE, EntityType.PURCHASE, EntityType.RECEIVABLE, EntityType.PAYMENT,
                                         EntityType.EXCHANGE]
            found: list[tuple[Any, EntityType, str]] = []
            selected_customer = (await resolve_customer(s, business_id, customer_name, create_if_missing=False))[0] if customer_name else None
            text_norm = f"%{normalize_name(text_query)}%" if text_query else None
            prod_norm = f"%{normalize_name(product)}%" if product else None

            def amount_filter(col):
                return col.between(amount * Decimal("0.98"), amount * Decimal("1.02")) if amount else True

            for k in kinds:
                stmt: Select
                if k == EntityType.SALE:
                    stmt = select(Sale).outerjoin(Customer, Customer.id == Sale.customer_id).where(
                        Sale.business_id == business_id, Sale.deleted_at.is_(None), Sale.business_date.between(rng.start, rng.end),
                        amount_filter(Sale.total_amount))
                    if selected_customer:
                        stmt = stmt.where(Customer.id == selected_customer.id)
                    if prod_norm or text_norm:
                        pattern = prod_norm or text_norm
                        stmt = stmt.where(
                            select(SaleItem.id).outerjoin(Product, Product.id == SaleItem.product_id)
                            .where(SaleItem.sale_id == Sale.id,
                                   or_(Product.normalized_name.like(pattern), func.lower(SaleItem.description).op("~~*")(pattern)))
                            .exists())
                    stmt = stmt.order_by(Sale.business_date.desc(), Sale.created_at.desc())
                elif k == EntityType.EXPENSE:
                    if selected_customer or prod_norm:
                        continue
                    stmt = select(Expense).where(Expense.business_id == business_id, Expense.deleted_at.is_(None),
                                                 Expense.business_date.between(rng.start, rng.end), amount_filter(Expense.amount))
                    if text_norm:
                        stmt = stmt.where(func.lower(Expense.description).op("~~*")(text_norm))
                    stmt = stmt.order_by(Expense.business_date.desc(), Expense.created_at.desc())
                elif k == EntityType.PURCHASE:
                    if selected_customer:
                        continue
                    stmt = select(Purchase).where(Purchase.business_id == business_id, Purchase.deleted_at.is_(None),
                                                  Purchase.business_date.between(rng.start, rng.end), amount_filter(Purchase.amount))
                    if text_norm or prod_norm:
                        pattern = text_norm or prod_norm
                        stmt = stmt.where(or_(func.lower(Purchase.description).op("~~*")(pattern),
                                              func.lower(func.coalesce(Purchase.supplier_name, "")).op("~~*")(pattern)))
                    stmt = stmt.order_by(Purchase.business_date.desc(), Purchase.created_at.desc())
                elif k == EntityType.RECEIVABLE:
                    if prod_norm:
                        continue
                    stmt = select(Receivable).join(Customer, Customer.id == Receivable.customer_id).where(
                        Receivable.business_id == business_id, Receivable.deleted_at.is_(None), Receivable.sale_id.is_(None),
                        Receivable.business_date.between(rng.start, rng.end), amount_filter(Receivable.amount))
                    if selected_customer:
                        stmt = stmt.where(Customer.id == selected_customer.id)
                    if text_norm:
                        stmt = stmt.where(func.lower(func.coalesce(Receivable.description, "")).op("~~*")(text_norm))
                    stmt = stmt.order_by(Receivable.business_date.desc(), Receivable.created_at.desc())
                elif k == EntityType.EXCHANGE:
                    if selected_customer or prod_norm:
                        continue
                    stmt = select(CurrencyExchange).where(
                        CurrencyExchange.business_id == business_id, CurrencyExchange.deleted_at.is_(None),
                        CurrencyExchange.business_date.between(rng.start, rng.end),
                        or_(amount_filter(CurrencyExchange.out_amount), amount_filter(CurrencyExchange.in_amount)))
                    if text_norm:
                        stmt = stmt.where(func.lower(func.coalesce(CurrencyExchange.description, "")).op("~~*")(text_norm))
                    stmt = stmt.order_by(CurrencyExchange.business_date.desc(), CurrencyExchange.created_at.desc())
                else:
                    if prod_norm or text_norm:
                        continue
                    stmt = select(CustomerPayment).join(Customer, Customer.id == CustomerPayment.customer_id).where(
                        CustomerPayment.business_id == business_id, CustomerPayment.deleted_at.is_(None),
                        CustomerPayment.business_date.between(rng.start, rng.end), amount_filter(CustomerPayment.amount))
                    if selected_customer:
                        stmt = stmt.where(Customer.id == selected_customer.id)
                    stmt = stmt.order_by(CustomerPayment.business_date.desc(), CustomerPayment.created_at.desc())
                for entity in (await s.execute(stmt.limit(limit))).unique().scalars().all():
                    summary = {
                        EntityType.SALE: sale_summary, EntityType.EXPENSE: expense_summary, EntityType.PURCHASE: purchase_summary,
                        EntityType.RECEIVABLE: receivable_summary, EntityType.PAYMENT: payment_summary,
                        EntityType.EXCHANGE: exchange_summary,
                    }[k](entity)
                    found.append((entity, k, summary))
            found.sort(key=lambda t: (t[0].business_date, t[0].created_at), reverse=True)
            found = found[:limit]
        refs = [EntityRef(k, e.id, LinkRelation.SHOWN, e.version, summ) for e, k, summ in found]
        return QueryResult({"period": rng.to_dict(), "count": len(refs)}, refs)

    # ------------------------------------------------------------------ caja y métricas

    async def cash_status(self, business_id: UUID, day: date | None, now_utc) -> QueryResult:
        async with self._factory() as s:
            business = await get_business(s, business_id)
            d = day or local_today(business.timezone, now_utc)
            closed = (await s.execute(select(CashSession).where(CashSession.business_id == business_id,
                                                                 CashSession.business_date == d))).scalar_one_or_none()
            live = await cash_summary(s, business_id, d, CurrencyView(business.display_currency))
        data: dict[str, Any] = {"closed": closed is not None, "live_summary": live}
        if closed:
            data["closed_summary_net_cash"] = closed.summary.get("net_cash")
            data["changed_since_close"] = closed.summary.get("raw") != live.get("raw")
        return QueryResult(data, [])

    async def metrics(self, business_id: UUID, query: MetricsQuery, now_utc) -> QueryResult:
        async with self._factory() as s, s.begin():
            business = await get_business(s, business_id)
            data = await run_metrics_query(s, business_id, query, local_today(business.timezone, now_utc),
                                           CurrencyView(business.display_currency), profile_of(business))
        return QueryResult(data, [])

    # ------------------------------------------------------------------ memoria

    async def search_notes(self, business_id: UUID, query: str | None = None, subject: str | None = None,
                           limit: int = 10) -> QueryResult:
        async with self._factory() as s:
            stmt = select(BusinessNote).where(BusinessNote.business_id == business_id, BusinessNote.deleted_at.is_(None),
                                              BusinessNote.superseded_at.is_(None))
            conds = []
            if query:
                conds.append(text("to_tsvector('spanish', business_notes.text) @@ plainto_tsquery('spanish', :q)").bindparams(q=query))
                conds.append(func.lower(BusinessNote.text).op("~~*")(f"%{query.lower()}%"))
            if subject:
                conds.append(func.lower(func.coalesce(BusinessNote.subject_name, "")).op("~~*")(f"%{subject.lower()}%"))
            if conds:
                stmt = stmt.where(or_(*conds))
            notes = (await s.execute(stmt.order_by(BusinessNote.created_at.desc()).limit(limit))).scalars().all()
        return QueryResult(
            {"notes": [{"date": fmt_short_date(n.created_at.date()), "kind": n.kind, "subject": n.subject_name, "text": n.text}
                       for n in notes]},
            [EntityRef(EntityType.NOTE, n.id, LinkRelation.SHOWN, n.version, f"nota | {n.text[:80]}") for n in notes],
        )

    async def notes_for_subjects(self, business_id: UUID, names: list[str], limit: int = 5) -> list[dict[str, str]]:
        if not names:
            return []
        async with self._factory() as s:
            conds = [func.lower(func.coalesce(BusinessNote.subject_name, "")).op("~~*")(f"%{n.lower()}%") for n in names if len(n) >= 3]
            if not conds:
                return []
            notes = (await s.execute(
                select(BusinessNote).where(BusinessNote.business_id == business_id, BusinessNote.deleted_at.is_(None),
                                           BusinessNote.superseded_at.is_(None), or_(*conds))
                .order_by(BusinessNote.created_at.desc()).limit(limit)
            )).scalars().all()
        return [{"subject": n.subject_name or "", "text": n.text} for n in notes]

    async def policy_notes(self, business_id: UUID, limit: int = 5) -> list[str]:
        async with self._factory() as s:
            notes = (await s.execute(
                select(BusinessNote.text).where(BusinessNote.business_id == business_id, BusinessNote.deleted_at.is_(None),
                                                BusinessNote.superseded_at.is_(None), BusinessNote.kind == NoteKind.POLICY.value)
                .order_by(BusinessNote.created_at.desc()).limit(limit)
            )).scalars().all()
        return list(notes)

    async def known_names(self, business_id: UUID) -> dict[str, list[str]]:
        """Nombres de clientes/productos con nota, para detectar menciones en el mensaje (barato, sin LLM)."""
        async with self._factory() as s:
            rows = (await s.execute(
                select(BusinessNote.subject_name).where(BusinessNote.business_id == business_id, BusinessNote.deleted_at.is_(None),
                                                        BusinessNote.subject_name.is_not(None)).distinct()
            )).scalars().all()
        return {"subjects": [r for r in rows if r]}

    async def search_messages(self, business_id: UUID, query: str, period: PeriodName | None = None, limit: int = 10) -> QueryResult:
        if not query or len(query.strip()) < 2:
            raise ValidationFailed("Indica qué buscar.")
        async with self._factory() as s:
            business = await get_business(s, business_id)
            today = local_today(business.timezone)
            rng = resolve_period(period or PeriodName.LAST_30_DAYS, today)
            doc = "to_tsvector('spanish', coalesce(inbound_messages.body, '') || ' ' || coalesce(inbound_messages.transcript, ''))"
            stmt = (
                select(InboundMessage)
                .where(InboundMessage.business_id == business_id,
                       func.date(func.timezone(business.timezone, InboundMessage.received_at)).between(rng.start, rng.end),
                       or_(text(f"{doc} @@ plainto_tsquery('spanish', :q)").bindparams(q=query),
                           func.lower(func.coalesce(InboundMessage.body, InboundMessage.transcript, "")).op("~~*")(f"%{query.lower()}%")))
                .order_by(InboundMessage.received_at.desc()).limit(limit)
            )
            msgs = (await s.execute(stmt)).scalars().all()
        return QueryResult({"period": rng.to_dict(), "messages": [
            {"date": fmt_short_date(m.received_at.astimezone(ZoneInfo(business.timezone)).date()), "text": (m.body or m.transcript or "")[:300]} for m in msgs]}, [])

    # ------------------------------------------------------------------ recuperación por id (handles, citas)

    async def load_refs(self, business_id: UUID, member_id: UUID, refs: list[tuple[EntityType, UUID]], *, reconciliation: bool = False) -> list[EntityRef]:
        """Carga registros vigentes por id y devuelve refs con resumen y versión actual (ignora borrados)."""
        models = {EntityType.SALE: (Sale, sale_summary), EntityType.EXPENSE: (Expense, expense_summary),
                  EntityType.PURCHASE: (Purchase, purchase_summary), EntityType.RECEIVABLE: (Receivable, receivable_summary),
                  EntityType.PAYMENT: (CustomerPayment, payment_summary), EntityType.EXCHANGE: (CurrencyExchange, exchange_summary),
                  EntityType.BROKER_MOVEMENT: (BrokerMovement, broker_movement_summary),
                  EntityType.CUSTOMER_BALANCE_ADJUSTMENT: (CustomerBalanceAdjustment, customer_adjustment_summary)}
        out: list[EntityRef] = []
        async with self._factory() as s:
            member = await s.get(BusinessMember, member_id)
            for etype, eid in refs:
                if etype == EntityType.NOTE:
                    note = await s.get(BusinessNote, eid)
                    if note and note.business_id == business_id and note.deleted_at is None:
                        out.append(EntityRef(etype, note.id, LinkRelation.SHOWN, note.version, f"nota | {note.text[:80]}"))
                    continue
                if etype not in models:
                    continue
                model, summarize = models[etype]
                entity = await s.get(model, eid)
                if entity is None or entity.business_id != business_id or entity.deleted_at is not None:
                    continue
                if etype == EntityType.CUSTOMER_BALANCE_ADJUSTMENT:
                    if (member is None or not member.is_active or member.business_id != business_id
                            or member.role not in {MemberRole.OWNER.value, MemberRole.BROKER.value}
                            or member.role == MemberRole.BROKER.value and entity.customer.classification == "personal"
                            and entity.customer.owner_broker_member_id != member.id):
                        continue
                reconciliation_author = (reconciliation and isinstance(entity, BrokerMovement) and entity.kind == "adjustment"
                    and member is not None and member.is_active and member.business_id == business_id
                    and entity.recorded_by_member_id == member.id)
                if (not reconciliation_author and etype in {EntityType.BROKER_MOVEMENT, EntityType.RECEIVABLE, EntityType.PAYMENT} and member is not None
                        and member.role == MemberRole.BROKER.value and entity.broker_member_id != member.id
                        and (entity.customer is None or entity.customer.classification != "global")):
                    continue
                out.append(EntityRef(etype, entity.id, LinkRelation.SHOWN, entity.version, summarize(entity)))
        return out

    async def deleted_refs(self, business_id: UUID, member_id: UUID, refs: list[tuple[EntityType, UUID]]) -> list[str]:
        """Resúmenes de registros citados que ya fueron borrados (para decírselo al dueño)."""
        models = {EntityType.SALE: (Sale, sale_summary), EntityType.EXPENSE: (Expense, expense_summary),
                  EntityType.PURCHASE: (Purchase, purchase_summary), EntityType.RECEIVABLE: (Receivable, receivable_summary),
                  EntityType.PAYMENT: (CustomerPayment, payment_summary), EntityType.EXCHANGE: (CurrencyExchange, exchange_summary),
                  EntityType.BROKER_MOVEMENT: (BrokerMovement, broker_movement_summary),
                  EntityType.CUSTOMER_BALANCE_ADJUSTMENT: (CustomerBalanceAdjustment, customer_adjustment_summary)}
        out = []
        async with self._factory() as s:
            member = await s.get(BusinessMember, member_id)
            for etype, eid in refs:
                if etype in models:
                    model, summarize = models[etype]
                    entity = await s.get(model, eid)
                    if etype == EntityType.CUSTOMER_BALANCE_ADJUSTMENT and entity and (
                            member is None or member.business_id != business_id or not member.is_active
                            or member.role not in {MemberRole.OWNER.value, MemberRole.BROKER.value}
                            or member.role == MemberRole.BROKER.value and entity.customer.classification == "personal"
                            and entity.customer.owner_broker_member_id != member.id):
                        continue
                    if (entity and entity.business_id == business_id and entity.deleted_at is not None
                            and not (etype in {EntityType.BROKER_MOVEMENT, EntityType.RECEIVABLE, EntityType.PAYMENT} and member is not None
                                     and member.role == MemberRole.BROKER.value and entity.broker_member_id != member.id
                                     and (entity.customer is None or entity.customer.classification != "global"))):
                        out.append(summarize(entity))
        return out
