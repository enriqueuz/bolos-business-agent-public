"""Lecturas autorizadas y analítica del ledger de corredores."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import and_, case, false, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.application.common import customer_balance
from app.application.customer_accounts import account_entries, balance_fields
from app.application.resolvers import search_brokerage_customers
from app.db.models import (
    AuditLog,
    BrokerMovement,
    Business,
    BusinessMember,
    Customer,
    CustomerBalanceAdjustment,
    CustomerPayment,
    PaymentAllocation,
    Receivable,
)
from app.domain.brokerage import RATE_QUANTUM, q_money
from app.domain.customers import normalize_nickname
from app.domain.dates import PeriodName, local_today, resolve_period
from app.domain.enums import BusinessVertical, MemberRole
from app.domain.errors import NotFound, ValidationFailed
from app.domain.money import fmt_amount
from app.domain.text import normalize_name, optional_text

ZERO = Decimal("0.00")


def _paid_subquery():
    return (
        select(func.coalesce(func.sum(PaymentAllocation.applied_amount), 0))
        .join(CustomerPayment, CustomerPayment.id == PaymentAllocation.payment_id)
        .where(
            PaymentAllocation.receivable_id == Receivable.id,
            CustomerPayment.deleted_at.is_(None),
        )
        .correlate(Receivable)
        .scalar_subquery()
    )


class BrokerageQueries:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def _scope(
        self, session: AsyncSession, business_id: UUID, member_id: UUID
    ) -> tuple[Business, BusinessMember, UUID | None]:
        business = await session.get(Business, business_id)
        member = await session.get(BusinessMember, member_id)
        if business is None or member is None or member.business_id != business_id or not member.is_active:
            raise NotFound("Sesión no encontrada.")
        if business.vertical != BusinessVertical.BROKERAGE.value:
            raise ValidationFailed("El módulo de corredores no está habilitado.")
        role = MemberRole(member.role)
        if role not in {MemberRole.OWNER, MemberRole.BROKER}:
            raise ValidationFailed("No tienes permiso para consultar corredores.")
        return business, member, member.id if role == MemberRole.BROKER else None

    async def customers(self, business_id: UUID, member_id: UUID, q: str = "") -> list[dict]:
        async with self._factory() as session:
            _, _, own = await self._scope(session, business_id, member_id)
            customers = await search_brokerage_customers(session, business_id, q, own)
            return [{"id": str(customer.id), "name": customer.name, "nickname": customer.nickname, "display_name": customer.display_name, "classification": customer.classification,
                     "owner_broker_member_id": str(customer.owner_broker_member_id) if customer.owner_broker_member_id else None,
                     "owner_broker_name": customer.owner_broker.name if customer.owner_broker else None,
                     "phone": customer.phone} for customer in customers]

    @staticmethod
    def _customer_accounts_stmt(business_id, effective, own, portfolio_scope="all"):
        if portfolio_scope not in {"all", "global", "personal"}:
            raise ValidationFailed("Clasificación de cartera inválida.")
        entries = account_entries(business_id)
        totals = select(entries.c.customer_id, entries.c.portfolio_id,
                        func.sum(entries.c.delta).label("balance_usd")).group_by(
                            entries.c.customer_id, entries.c.portfolio_id).subquery()
        net = func.coalesce(totals.c.balance_usd, 0)
        stmt = select(Customer, net.label("net")).outerjoin(totals, and_(
            totals.c.customer_id == Customer.id,
            totals.c.portfolio_id.is_not_distinct_from(Customer.owner_broker_member_id))).where(
                Customer.business_id == business_id, Customer.deleted_at.is_(None))
        kind, broker = effective
        if kind == "house":
            stmt = stmt.where(Customer.classification == "global")
        elif kind == "broker":
            personal = and_(Customer.classification == "personal", Customer.owner_broker_member_id == broker)
            stmt = stmt.where(or_(personal, Customer.classification == "global") if own else personal)
        if portfolio_scope != "all":
            stmt = stmt.where(Customer.classification == portfolio_scope)
        return stmt, net

    async def customer_accounts(self, business_id: UUID, member_id: UUID, *, broker_id=None,
                                account_scope="all", portfolio_scope="all", customer_name=None,
                                status="open", limit=50, offset=0) -> dict:
        async with self._factory() as session:
            _, _, own = await self._scope(session, business_id, member_id)
            effective = self._effective_account(own, broker_id, account_scope, portfolio=True)
            stmt, net = self._customer_accounts_stmt(business_id, effective, own, portfolio_scope)
            if customer_name:
                # Resolve within the authorized scope: an exact nickname takes
                # precedence over another customer's historical name.
                nickname = Customer.nickname == normalize_nickname(customer_name)
                exact_name = Customer.normalized_name == normalize_name(customer_name)
                for match in (nickname, exact_name):
                    if await session.scalar(select(func.count()).select_from(stmt.where(match).subquery())):
                        stmt = stmt.where(match)
                        break
                else:
                    stmt = stmt.where(or_(func.lower(Customer.name).contains(customer_name.strip().casefold(), autoescape=True),
                                          Customer.nickname.contains(normalize_nickname(customer_name) or "", autoescape=True)))
            if status == "open":
                stmt = stmt.where(net != 0)
            elif status == "customer_owes":
                stmt = stmt.where(net < 0)
            elif status == "house_owes":
                stmt = stmt.where(net > 0)
            elif status == "settled":
                stmt = stmt.where(net == 0)
            elif status != "all":
                raise ValidationFailed("Estado de cuenta inválido.")
            aggregate = stmt.subquery()
            totals = (await session.execute(select(func.count(),
                func.coalesce(func.sum(func.greatest(-aggregate.c.net, 0)), 0),
                func.coalesce(func.sum(func.greatest(aggregate.c.net, 0)), 0)))).one()
            rows = (await session.execute(stmt.order_by(Customer.normalized_name, Customer.id)
                                          .limit(limit).offset(offset))).all()
            items = [{"customer_id": str(c.id), "customer": c.display_name, "version": c.version,
                      "classification": c.classification, "portfolio_scope": c.classification,
                      "member_id": str(c.owner_broker_member_id) if c.owner_broker_member_id else None,
                      "broker": c.owner_broker.name if c.owner_broker else "Casa / Global",
                      **balance_fields(Decimal(value))} for c, value in rows]
            return {"total": totals[0], "total_to_collect": str(q_money(Decimal(totals[1]))),
                    "total_to_pay": str(q_money(Decimal(totals[2]))), "items": items}

    async def customer_account(self, business_id: UUID, member_id: UUID, customer_id: UUID, *,
                               date_from=None, date_to=None, limit=50, offset=0,
                               include_balance_steps=False, account_scope="all", broker_id=None) -> dict:
        if include_balance_steps and (date_from or date_to or offset or account_scope != "all" or broker_id):
            raise ValidationFailed("La composición del saldo requiere los movimientos más recientes sin filtro de fechas.")
        async with self._factory() as session:
            _, _, own = await self._scope(session, business_id, member_id)
            stmt, _ = self._customer_accounts_stmt(business_id, ("broker", own) if own else ("all", None), own)
            row = (await session.execute(stmt.where(Customer.id == customer_id))).one_or_none()
            if row is None:
                raise NotFound("Cliente no encontrado.")
            customer, net = row
            filters = [BrokerMovement.business_id == business_id, BrokerMovement.customer_id == customer_id,
                       BrokerMovement.deleted_at.is_(None)]
            # Visibility above remains authoritative. A global customer's history
            # is shared; an execution-account restriction is an explicit option.
            history_scope = ("all", None) if account_scope == "all" and broker_id is None else self._effective_account(
                own, broker_id, account_scope)
            account_filter = self._account_filter(BrokerMovement.broker_member_id, history_scope)
            if account_filter is not None:
                filters.append(account_filter)
            if date_from:
                filters.append(BrokerMovement.business_date >= date_from)
            if date_to:
                filters.append(BrokerMovement.business_date <= date_to)
            adjustment_filters = [CustomerBalanceAdjustment.business_id == business_id,
                CustomerBalanceAdjustment.customer_id == customer_id, CustomerBalanceAdjustment.deleted_at.is_(None)]
            portfolio_filter = self._account_filter(CustomerBalanceAdjustment.portfolio_broker_member_id, history_scope)
            if portfolio_filter is not None:
                adjustment_filters.append(portfolio_filter)
            if date_from:
                adjustment_filters.append(CustomerBalanceAdjustment.business_date >= date_from)
            if date_to:
                adjustment_filters.append(CustomerBalanceAdjustment.business_date <= date_to)
            entries = union_all(
                select(BrokerMovement.id.label("id"), literal("broker_movement").label("entry_type"),
                       BrokerMovement.business_date.label("business_date"), BrokerMovement.occurred_at.label("occurred_at")).where(*filters),
                select(CustomerBalanceAdjustment.id, literal("customer_balance_adjustment"),
                       CustomerBalanceAdjustment.business_date, CustomerBalanceAdjustment.occurred_at).where(*adjustment_filters),
            ).subquery()
            total = await session.scalar(select(func.count()).select_from(entries))
            page = (await session.execute(select(entries).order_by(entries.c.business_date.desc(),
                entries.c.occurred_at.desc(), entries.c.id.desc()).limit(limit).offset(offset))).all()
            items = []
            for entry in page:
                if entry.entry_type == "customer_balance_adjustment":
                    adjustment = await session.get(CustomerBalanceAdjustment, entry.id)
                    items.append(self._customer_adjustment(adjustment))
                else:
                    movement = await session.get(BrokerMovement, entry.id)
                    items.append({**self._movement(movement), "editable": movement.kind != "adjustment" and (not own or movement.broker_member_id == own)})
            if include_balance_steps:
                # Walk backwards from the canonical current net using the existing read projection.
                # These are restated balances of ACTIVE records, never immutable historical snapshots.
                after = q_money(Decimal(net))
                for item in items:
                    before = q_money(after - Decimal(item["customer_balance_delta"]))
                    item.update(customer_balance_before=str(before), customer_balance_after=str(after))
                    after = before
            return {"customer_id": str(customer.id), "customer": customer.display_name,
                    "name": customer.name, "nickname": customer.nickname, "version": customer.version,
                    "classification": customer.classification,
                    "member_id": str(customer.owner_broker_member_id) if customer.owner_broker_member_id else None,
                    **balance_fields(Decimal(net)), "total": total,
                    "items": items}

    @staticmethod
    def _effective_account(
        own: UUID | None,
        requested: UUID | None,
        account_scope: str = "all",
        *,
        portfolio: bool = False,
    ) -> tuple[str, UUID | None]:
        if own and requested and requested != own:
            noun = "cartera" if portfolio else "cuenta"
            raise ValidationFailed(f"Solo puedes consultar tu propia {noun}.")
        if own:
            if account_scope == "house":
                raise ValidationFailed("Solo puedes consultar tu propia cuenta.")
            return "broker", own
        if requested is not None:
            if account_scope == "house":
                raise ValidationFailed("No combines Casa con un corredor.")
            return "broker", requested
        if account_scope not in {"all", "house", "broker"}:
            raise ValidationFailed("Ámbito de cuenta inválido.")
        if account_scope == "broker":
            raise ValidationFailed("Indica el corredor.")
        return account_scope, None

    @staticmethod
    def _account_filter(column, scope: tuple[str, UUID | None]):
        kind, broker_id = scope
        if kind == "house":
            return column.is_(None)
        if kind == "broker":
            return column == broker_id
        return None

    @classmethod
    def _portfolio_filter(cls, scope: tuple[str, UUID | None], *, include_global: bool = False):
        column = Receivable.broker_member_id
        own = cls._account_filter(column, scope)
        if include_global:
            return or_(own, and_(column.is_(None), Receivable.customer.has(Customer.classification == "global")))
        return own

    @staticmethod
    async def _multicurrency_summary(session: AsyncSession, filters: list) -> dict:
        complete = and_(BrokerMovement.operation_amount_bs.is_not(None), BrokerMovement.exchange_rate.is_not(None))
        rows = (await session.execute(select(
            BrokerMovement.operation_type, func.count(), func.count().filter(complete),
            func.coalesce(func.sum(BrokerMovement.operation_amount), 0),
            func.coalesce(func.sum(BrokerMovement.operation_amount).filter(complete), 0),
            func.coalesce(func.sum(BrokerMovement.operation_amount_bs).filter(complete), 0),
        ).where(*filters, BrokerMovement.kind == "operation").group_by(BrokerMovement.operation_type))).all()
        result = {}
        by_type = {row[0]: row for row in rows}
        for kind in ("buy", "sell"):
            row = by_type.get(kind)
            count, complete_count = (row[1], row[2]) if row else (0, 0)
            usd, complete_usd, bs = (Decimal(row[3]), Decimal(row[4]), Decimal(row[5])) if row else (ZERO, ZERO, ZERO)
            result[kind] = {"total_count": count, "complete_count": complete_count,
                            "incomplete_count": count - complete_count,
                            "total_usd": str(q_money(usd)), "complete_usd": str(q_money(complete_usd)),
                            "missing_usd": str(q_money(usd - complete_usd)), "amount_bs": str(q_money(bs)),
                            "weighted_rate": str((bs / complete_usd).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)) if complete_usd else None}
        return result

    async def receipt_candidates(self, business_id: UUID, member_id: UUID, *, now, minutes: int | None = None) -> list[dict]:
        async with self._factory() as session:
            business, _, _ = await self._scope(session, business_id, member_id)
            minutes = min(max(int((business.settings or {}).get("broker_receipt_window_minutes", 10)), 1), 60) if minutes is None else minutes
            rows = (await session.execute(select(BrokerMovement).where(
                BrokerMovement.business_id == business_id, BrokerMovement.recorded_by_member_id == member_id,
                BrokerMovement.kind == "operation", BrokerMovement.deleted_at.is_(None),
                BrokerMovement.created_at >= now - timedelta(minutes=minutes), BrokerMovement.created_at <= now,
                BrokerMovement.operation_amount_bs.is_(None),
            ).order_by(BrokerMovement.created_at.desc()).limit(10))).scalars().all()
            return [self._movement(row) for row in rows]

    async def accounts(self, business_id: UUID, member_id: UUID) -> list[dict]:
        async with self._factory() as session:
            _business, _member, own = await self._scope(session, business_id, member_id)
            stmt = select(BusinessMember).where(
                BusinessMember.business_id == business_id,
                BusinessMember.role == MemberRole.BROKER.value,
            ).order_by(BusinessMember.is_active.desc(), BusinessMember.name)
            if own:
                stmt = stmt.where(BusinessMember.id == own)
            rows = (await session.execute(stmt)).scalars().all()
            accounts = [] if own else [{
                "key": "house",
                "kind": "house",
                "member_id": None,
                "name": "Casa",
                "abbreviation": None,
                "is_active": True,
                "manageable": False,
            }]
            accounts.extend({
                "key": str(row.id),
                "kind": "broker",
                "member_id": str(row.id),
                "name": row.name or "Corredor",
                "abbreviation": row.abbreviation,
                "is_active": row.is_active,
                "manageable": True,
            } for row in rows)
            return accounts

    async def members(self, business_id: UUID, member_id: UUID) -> list[dict]:
        async with self._factory() as session:
            _business, member, own = await self._scope(session, business_id, member_id)
            stmt = select(BusinessMember).where(
                BusinessMember.business_id == business_id,
                BusinessMember.role == MemberRole.BROKER.value,
            ).order_by(BusinessMember.is_active.desc(), BusinessMember.name)
            if own:
                stmt = stmt.where(BusinessMember.id == own)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "id": str(row.id),
                    "name": row.name or "Corredor",
                    "abbreviation": row.abbreviation,
                    "phone": row.phone if member.role == MemberRole.OWNER.value else None,
                    "is_active": row.is_active,
                }
                for row in rows
            ]

    async def summary(
        self,
        business_id: UUID,
        member_id: UUID,
        *,
        period: PeriodName = PeriodName.THIS_MONTH,
        date_from: date | None = None,
        date_to: date | None = None,
        broker_id: UUID | None = None,
        account_scope: str = "all",
    ) -> dict:
        async with self._factory() as session:
            business, member, own = await self._scope(session, business_id, member_id)
            effective = self._effective_account(own, broker_id, account_scope)
            today = local_today(business.timezone)
            if period == PeriodName.CUSTOM and date_from and date_from > today:
                raise ValidationFailed("La fecha inicial no puede ser futura.")
            rng = resolve_period(period, today, date_from, date_to)
            base = [BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)]
            account_filter = self._account_filter(BrokerMovement.broker_member_id, effective)
            if account_filter is not None:
                base.append(account_filter)

            balances_stmt = (
                select(
                    BusinessMember.id,
                    BusinessMember.name,
                    BusinessMember.abbreviation,
                    BusinessMember.is_active,
                    func.coalesce(func.sum(BrokerMovement.balance_delta), 0).label("balance"),
                    func.coalesce(
                        func.sum(
                            case(
                                (BrokerMovement.business_date.between(rng.start, rng.end), BrokerMovement.balance_delta),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("period_net"),
                    func.max(BrokerMovement.occurred_at).label("last_movement"),
                )
                .join(
                    BrokerMovement,
                    and_(
                        BrokerMovement.broker_member_id == BusinessMember.id,
                        BrokerMovement.business_id == business_id,
                        BrokerMovement.deleted_at.is_(None),
                    ),
                    isouter=True,
                )
                .where(
                    BusinessMember.business_id == business_id,
                    BusinessMember.role == MemberRole.BROKER.value,
                )
                .group_by(
                    BusinessMember.id,
                    BusinessMember.name,
                    BusinessMember.abbreviation,
                    BusinessMember.is_active,
                )
                .order_by(BusinessMember.is_active.desc(), BusinessMember.name)
            )
            if effective[0] == "broker":
                balances_stmt = balances_stmt.where(BusinessMember.id == effective[1])
            elif effective[0] == "house":
                balances_stmt = balances_stmt.where(false())
            balances_rows = (await session.execute(balances_stmt)).all()
            balances = [
                {
                    "member_id": str(row.id),
                    "name": row.name or "Corredor",
                    "abbreviation": row.abbreviation,
                    "is_active": row.is_active,
                    "balance": str(q_money(Decimal(row.balance))),
                    "balance_display": fmt_amount(Decimal(row.balance), "USD"),
                    "period_net": str(q_money(Decimal(row.period_net))),
                    "period_net_display": fmt_amount(Decimal(row.period_net), "USD"),
                    "today_net": str(q_money(Decimal(row.period_net))) if period == PeriodName.TODAY else "0.00",
                    "today_net_display": fmt_amount(Decimal(row.period_net), "USD") if period == PeriodName.TODAY else "$0,00",
                    "last_movement": row.last_movement.isoformat() if row.last_movement else None,
                }
                for row in balances_rows
            ]
            debt = sum((-Decimal(row["balance"]) for row in balances if Decimal(row["balance"]) < 0), ZERO)
            credit = sum((Decimal(row["balance"]) for row in balances if Decimal(row["balance"]) > 0), ZERO)

            period_scope = [*base, BrokerMovement.business_date.between(rng.start, rng.end)]
            totals_row = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (BrokerMovement.kind == "in", BrokerMovement.balance_delta),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                        func.coalesce(func.sum(BrokerMovement.excess), 0),
                        func.coalesce(func.sum(BrokerMovement.commission), 0),
                        func.coalesce(
                            func.sum(case((BrokerMovement.operation_type == "buy", BrokerMovement.operation_amount), else_=0)),
                            0,
                        ),
                        func.coalesce(
                            func.sum(case((BrokerMovement.operation_type == "sell", BrokerMovement.operation_amount), else_=0)),
                            0,
                        ),
                        func.coalesce(func.sum(case((BrokerMovement.kind == "out", -BrokerMovement.balance_delta), else_=0)), 0),
                    ).where(*period_scope)
                )
            ).one()

            daily_rows = (
                await session.execute(
                    select(
                        BrokerMovement.business_date,
                        func.coalesce(
                            func.sum(case((BrokerMovement.operation_type == "buy", BrokerMovement.operation_amount), else_=0)),
                            0,
                        ),
                        func.coalesce(
                            func.sum(case((BrokerMovement.operation_type == "sell", BrokerMovement.operation_amount), else_=0)),
                            0,
                        ),
                        func.coalesce(func.sum(BrokerMovement.commission), 0),
                        func.coalesce(func.sum(BrokerMovement.excess), 0),
                        func.coalesce(func.sum(BrokerMovement.balance_delta), 0),
                    )
                    .where(*period_scope)
                    .group_by(BrokerMovement.business_date)
                    .order_by(BrokerMovement.business_date)
                )
            ).all()
            daily = {row[0]: row for row in daily_rows}
            opening = Decimal(
                (
                    await session.execute(
                        select(func.coalesce(func.sum(BrokerMovement.balance_delta), 0)).where(
                            *base, BrokerMovement.business_date < rng.start
                        )
                    )
                ).scalar_one()
            )
            series = []
            running = opening
            cursor = rng.start
            while cursor <= rng.end:
                row = daily.get(cursor)
                if row:
                    running += Decimal(row[5])
                series.append(
                    {
                        "date": cursor.isoformat(),
                        "bought": str(q_money(Decimal(row[1]) if row else ZERO)),
                        "sold": str(q_money(Decimal(row[2]) if row else ZERO)),
                        "commission": str(q_money(Decimal(row[3]) if row else ZERO)),
                        "excess": str(q_money(Decimal(row[4]) if row else ZERO)),
                        "balance": str(q_money(running)),
                    }
                )
                cursor += timedelta(days=1)

            multicurrency = await self._multicurrency_summary(session, period_scope)
            account_stmt, _ = self._customer_accounts_stmt(business_id, effective, own)
            account_rows = (await session.execute(account_stmt)).all()
            house_receivables = sum((max(-Decimal(value), ZERO) for c, value in account_rows if c.classification == "global"), ZERO)
            broker_receivables = sum((max(-Decimal(value), ZERO) for c, value in account_rows if c.classification == "personal"), ZERO)
            open_receivables = house_receivables + broker_receivables
            customer_credit = sum((max(Decimal(value), ZERO) for c, value in account_rows), ZERO)
            classified_portfolio = {"global": house_receivables, "personal": broker_receivables}
            payment_scope = [
                CustomerPayment.business_id == business_id,
                CustomerPayment.deleted_at.is_(None),
                CustomerPayment.business_date.between(rng.start, rng.end),
            ]
            payment_filter = self._account_filter(CustomerPayment.broker_member_id, effective)
            if payment_filter is not None:
                payment_scope.append(payment_filter)
            customer_payments = Decimal((await session.execute(
                select(func.coalesce(func.sum(CustomerPayment.amount), 0)).where(*payment_scope)
            )).scalar_one())
            recent = (
                await session.execute(
                    select(BrokerMovement)
                    .options(selectinload(BrokerMovement.broker), selectinload(BrokerMovement.recorded_by))
                    .where(*base)
                    .order_by(
                        BrokerMovement.business_date.desc(),
                        BrokerMovement.occurred_at.desc(),
                        BrokerMovement.id.desc(),
                    )
                    .limit(8)
                )
            ).scalars().all()

        return {
            "date": rng.end.isoformat(),
            "period": rng.to_dict(),
            "role": member.role,
            "account_scope": effective[0],
            "account_member_id": str(effective[1]) if effective[1] else None,
            "multicurrency": multicurrency,
            "balances": balances,
            "exposure": {
                "owed_to_business": str(q_money(debt)),
                "credit_to_brokers": str(q_money(credit)),
                "net": str(q_money(credit - debt)),
            },
            "totals": {
                "payments_to_accounts": str(q_money(Decimal(totals_row[0]))),
                "withdrawals_from_accounts": str(q_money(Decimal(totals_row[5]))),
                "excess": str(q_money(Decimal(totals_row[1]))),
                "commission": str(q_money(Decimal(totals_row[2]))),
                "bought": str(q_money(Decimal(totals_row[3]))),
                "sold": str(q_money(Decimal(totals_row[4]))),
                "receivables_global": str(q_money(classified_portfolio.get("global", ZERO))),
                "receivables_personal": str(q_money(classified_portfolio.get("personal", ZERO))),
                "receivables_open": str(q_money(open_receivables)),
                "receivables_house": str(q_money(house_receivables)),
                "receivables_brokers": str(q_money(broker_receivables)),
                "customer_payments": str(q_money(customer_payments)),
                "customer_credit": str(q_money(customer_credit)),
            },
            "series": series,
            "recent": [self._movement(row) for row in recent],
        }

    async def movements(
        self,
        business_id: UUID,
        member_id: UUID,
        *,
        broker_id: UUID | None = None,
        account_scope: str = "all",
        portfolio_scope: str = "all",
        completeness: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        logical_type: str | None = None,
        settlement: str | None = None,
        q: str | None = None,
        customer_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        async with self._factory() as session:
            _business, _member, own = await self._scope(session, business_id, member_id)
            effective = self._effective_account(own, broker_id, account_scope)
            base = [BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)]
            account_filter = self._account_filter(BrokerMovement.broker_member_id, effective)
            if account_filter is not None:
                base.append(account_filter)
            filters = list(base)
            if date_from:
                filters.append(BrokerMovement.business_date >= date_from)
            if date_to:
                filters.append(BrokerMovement.business_date <= date_to)
            if settlement:
                filters.append(BrokerMovement.settlement_status == settlement)
            if customer_id:
                filters.append(BrokerMovement.customer_id == customer_id)
            if q:
                term = f"%{q.strip().casefold()}%"
                filters.append(
                    or_(
                        func.lower(func.coalesce(BrokerMovement.description, "")).like(term),
                        BrokerMovement.customer.has(or_(func.lower(Customer.name).like(term), Customer.nickname.contains(normalize_nickname(q) or "", autoescape=True))),
                        BrokerMovement.broker.has(func.lower(BusinessMember.name).like(term)),
                    )
                )
            if portfolio_scope not in {"all", "global", "personal"}:
                raise ValidationFailed("Clasificación de cartera inválida.")
            if portfolio_scope != "all":
                filters.append(BrokerMovement.customer.has(Customer.classification == portfolio_scope))
            if completeness:
                if completeness == "complete":
                    filters.append(BrokerMovement.operation_amount_bs.is_not(None))
                elif completeness == "incomplete":
                    filters.extend([BrokerMovement.kind == "operation", BrokerMovement.operation_amount_bs.is_(None)])
                else:
                    raise ValidationFailed("Estado multimoneda inválido.")
            if logical_type:
                filters.append(self._logical_type_filter(logical_type))

            total = (
                await session.execute(select(func.count()).select_from(BrokerMovement).where(*filters))
            ).scalar_one()
            balance_ledger = (
                select(
                    BrokerMovement.id.label("movement_id"),
                    func.sum(BrokerMovement.balance_delta)
                    .over(
                        partition_by=BrokerMovement.broker_member_id,
                        order_by=(
                            BrokerMovement.business_date,
                            BrokerMovement.occurred_at,
                            BrokerMovement.id,
                        ),
                        rows=(None, 0),
                    )
                    .label("balance_after"),
                )
                .where(
                    BrokerMovement.business_id == business_id,
                    BrokerMovement.deleted_at.is_(None),
                )
                .subquery()
            )
            rows = (
                await session.execute(
                    select(BrokerMovement, balance_ledger.c.balance_after)
                    .join(balance_ledger, balance_ledger.c.movement_id == BrokerMovement.id)
                    .options(selectinload(BrokerMovement.broker), selectinload(BrokerMovement.recorded_by))
                    .where(*filters)
                    .order_by(
                        BrokerMovement.business_date.desc(),
                        BrokerMovement.occurred_at.desc(),
                        BrokerMovement.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            totals = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(BrokerMovement.balance_delta), 0),
                        func.coalesce(func.sum(BrokerMovement.operation_amount), 0),
                        func.coalesce(func.sum(BrokerMovement.commission), 0),
                    ).where(*filters)
                )
            ).one()
            opening_filters = list(base)
            opening_filters.append(BrokerMovement.business_date < date_from if date_from else False)
            closing_filters = list(base)
            if date_to:
                closing_filters.append(BrokerMovement.business_date <= date_to)
            opening = Decimal(
                (
                    await session.execute(
                        select(func.coalesce(func.sum(BrokerMovement.balance_delta), 0)).where(*opening_filters)
                    )
                ).scalar_one()
            )
            closing = Decimal(
                (
                    await session.execute(
                        select(func.coalesce(func.sum(BrokerMovement.balance_delta), 0)).where(*closing_filters)
                    )
                ).scalar_one()
            )
            return {
                "total": total,
                "opening_balance": str(q_money(opening)),
                "closing_balance": str(q_money(closing)),
                "totals": {
                    "balance_delta": str(q_money(Decimal(totals[0]))),
                    "operation_amount": str(q_money(Decimal(totals[1]))),
                    "commission": str(q_money(Decimal(totals[2]))),
                },
                "items": [
                    {
                        **self._movement(row),
                        "balance_after": str(q_money(Decimal(balance_after))),
                    }
                    for row, balance_after in rows
                ],
            }

    async def movement(self, business_id: UUID, member_id: UUID, movement_id: UUID) -> dict:
        async with self._factory() as session:
            _business, _member, own = await self._scope(session, business_id, member_id)
            row = await session.get(BrokerMovement, movement_id)
            if (
                row is None
                or row.business_id != business_id
                or row.deleted_at is not None
                or (own and row.broker_member_id != own and (row.customer is None or row.customer.classification != "global"))
            ):
                raise NotFound("Movimiento no encontrado.")
            history = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.business_id == business_id,
                        AuditLog.entity_type == "broker_movement",
                        AuditLog.entity_id == movement_id,
                    )
                    .order_by(AuditLog.at)
                )
            ).scalars().all()
            out = self._movement(row)
            out["editable"] = row.kind != "adjustment" and (not own or row.broker_member_id == own)
            out["history"] = [
                {"at": item.at.isoformat(), "action": item.action, "actor": item.actor_type, "command": item.command}
                for item in history
            ]
            out["customer_balance"] = await customer_balance(session, business_id, row.customer_id) if row.customer_id else None
            out["related_receivable"] = None
            if row.related_receivable_id:
                receivable = await session.get(Receivable, row.related_receivable_id)
                if receivable and receivable.deleted_at is None:
                    paid = await self._receivable_paid(session, receivable.id)
                    out["related_receivable"] = {
                        "id": str(receivable.id),
                        "customer": receivable.customer.display_name,
                        "amount": str(receivable.amount),
                        "paid": str(paid),
                        "remaining": str(q_money(receivable.amount - paid)),
                    }
            out["related_payment"] = None
            if row.related_payment_id:
                payment = await session.get(CustomerPayment, row.related_payment_id)
                if payment and payment.deleted_at is None:
                    out["related_payment"] = {
                        "id": str(payment.id), "customer": payment.customer.display_name, "amount": str(payment.amount)
                    }
            return out

    async def receivables(
        self,
        business_id: UUID,
        member_id: UUID,
        *,
        broker_id: UUID | None = None,
        account_scope: str = "all",
        portfolio_scope: str = "all",
        customer_name: str | None = None,
        status: str = "open",
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        async with self._factory() as session:
            _business, _member, own = await self._scope(session, business_id, member_id)
            effective = self._effective_account(own, broker_id, account_scope, portfolio=True)
            paid = _paid_subquery()
            filters = [
                Receivable.business_id == business_id,
                Receivable.deleted_at.is_(None),
            ]
            account_filter = self._portfolio_filter(effective, include_global=bool(own))
            if account_filter is not None:
                filters.append(account_filter)
            if portfolio_scope not in {"all", "global", "personal"}:
                raise ValidationFailed("Clasificación de cartera inválida.")
            if portfolio_scope != "all":
                filters.append(Customer.classification == portfolio_scope)
            if customer_name:
                filters.append(or_(func.lower(Customer.name).contains(customer_name.strip().casefold(), autoescape=True), Customer.nickname.contains(normalize_nickname(customer_name) or "", autoescape=True)))
            if date_from:
                filters.append(Receivable.business_date >= date_from)
            if date_to:
                filters.append(Receivable.business_date <= date_to)
            if status == "pending":
                filters.append(paid <= Decimal("0.005"))
            elif status == "partial":
                filters.extend([paid > Decimal("0.005"), paid < Receivable.amount - Decimal("0.005")])
            elif status == "settled":
                filters.append(paid >= Receivable.amount - Decimal("0.005"))
            elif status == "open":
                filters.append(paid < Receivable.amount - Decimal("0.005"))
            elif status != "all":
                raise ValidationFailed("Estado de cartera inválido.")

            stmt = select(Receivable, paid.label("paid")).join(Customer).where(*filters)
            total = (
                await session.execute(select(func.count()).select_from(Receivable).join(Customer).where(*filters))
            ).scalar_one()
            rows = (
                await session.execute(
                    stmt.order_by(Receivable.business_date.desc(), Receivable.occurred_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            origins = (await session.execute(select(BrokerMovement).where(
                BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None),
                BrokerMovement.related_receivable_id.in_([row.id for row, _ in rows]),
            ))).scalars().all() if rows else []
            origin_by_debt = {row.related_receivable_id: row for row in origins}
            items = []
            for row, paid_value in rows:
                paid_amount = q_money(Decimal(paid_value))
                broker = await session.get(BusinessMember, row.broker_member_id) if row.broker_member_id else None
                item = self._receivable(row, broker, paid_amount)
                origin = origin_by_debt.get(row.id)
                item["origin_movement_id"] = str(origin.id) if origin else None
                item["origin_movement_version"] = origin.version if origin else None
                items.append(item)
            total_remaining = Decimal(
                (
                    await session.execute(
                        select(
                            func.coalesce(
                                func.sum(func.greatest(Receivable.amount - paid, 0)), 0
                            )
                        )
                        .select_from(Receivable)
                        .join(Customer)
                        .where(*filters)
                    )
                ).scalar_one()
            )
            return {"total": total, "total_remaining": str(q_money(total_remaining)), "items": items}

    async def receivable(self, business_id: UUID, member_id: UUID, receivable_id: UUID) -> dict:
        async with self._factory() as session:
            _business, _member, own = await self._scope(session, business_id, member_id)
            row = await session.get(Receivable, receivable_id)
            if (
                row is None
                or row.business_id != business_id
                or row.deleted_at is not None
                or (own and row.broker_member_id != own and (row.customer is None or row.customer.classification != "global"))
            ):
                raise NotFound("Deuda no encontrada.")
            paid = await self._receivable_paid(session, row.id)
            broker = await session.get(BusinessMember, row.broker_member_id) if row.broker_member_id else None
            out = self._receivable(row, broker, paid)
            origin = (
                await session.execute(
                    select(BrokerMovement).where(
                        BrokerMovement.related_receivable_id == row.id,
                        BrokerMovement.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            out["origin_movement_id"] = str(origin.id) if origin else None
            payment_rows = (
                await session.execute(
                    select(CustomerPayment, PaymentAllocation.applied_amount)
                    .join(PaymentAllocation, PaymentAllocation.payment_id == CustomerPayment.id)
                    .where(
                        PaymentAllocation.receivable_id == row.id,
                        CustomerPayment.deleted_at.is_(None),
                    )
                    .order_by(CustomerPayment.business_date, CustomerPayment.occurred_at)
                )
            ).all()
            out["payments"] = [
                {
                    "id": str(payment.id),
                    "business_date": payment.business_date.isoformat(),
                    "amount": str(payment.amount),
                    "applied": str(q_money(Decimal(applied))),
                }
                for payment, applied in payment_rows
            ]
            return out

    async def balances_line(self, business_id: UUID, member_id: UUID) -> str:
        data = await self.summary(business_id, member_id, period=PeriodName.TODAY)
        if data["role"] == MemberRole.BROKER.value:
            balance = data["balances"][0]["balance_display"] if data["balances"] else "$0,00"
            abbreviation = data["balances"][0].get("abbreviation") if data["balances"] else None
            shortcut = f" Tu abreviación es {abbreviation}." if abbreviation else ""
            return f"Tu balance con el negocio: {balance}.{shortcut}"
        if not data["balances"]:
            return "Cuenta predeterminada del dueño: Casa. No hay corredores configurados todavía."
        return "Cuenta predeterminada del dueño: Casa. Balances de corredores: " + " · ".join(
            f"{row['name']} {row['balance_display']}" for row in data["balances"][:8]
        )

    @staticmethod
    async def _receivable_paid(session: AsyncSession, receivable_id: UUID) -> Decimal:
        value = (
            await session.execute(
                select(func.coalesce(func.sum(PaymentAllocation.applied_amount), 0))
                .join(CustomerPayment, CustomerPayment.id == PaymentAllocation.payment_id)
                .where(
                    PaymentAllocation.receivable_id == receivable_id,
                    CustomerPayment.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        return q_money(Decimal(value))

    @staticmethod
    def _logical_type_filter(value: str):
        mapping = {
            "adjustment": BrokerMovement.kind == "adjustment",
            "customer_payout": BrokerMovement.kind == "customer_payout",
            "pending_sell": and_(BrokerMovement.operation_type == "sell", BrokerMovement.settlement_status == "pending"),
            "account_payment": and_(BrokerMovement.kind == "in", BrokerMovement.related_payment_id.is_(None)),
            "account_withdrawal": BrokerMovement.kind == "out",
            "receivable_payment": BrokerMovement.related_payment_id.is_not(None),
            "buy": and_(
                BrokerMovement.operation_type == "buy",
                BrokerMovement.settlement_status == "settled",
                BrokerMovement.related_receivable_id.is_(None),
            ),
            "sell": BrokerMovement.operation_type == "sell",
            "account": BrokerMovement.settlement_status == "account",
            "pending": BrokerMovement.related_receivable_id.is_not(None),
        }
        if value not in mapping:
            raise ValidationFailed("Tipo de movimiento inválido.")
        return mapping[value]

    @staticmethod
    def logical_type(row: BrokerMovement) -> str:
        if row.kind == "adjustment":
            return "adjustment"
        if row.kind == "customer_payout":
            return "customer_payout"
        if row.kind == "out":
            return "account_withdrawal"
        if row.related_payment_id is not None:
            return "receivable_payment"
        if row.kind != "operation":
            return "account_payment"
        if row.settlement_status == "account":
            return "account"
        if row.related_receivable_id is not None:
            return "pending"
        return row.operation_type or "operation"

    @classmethod
    def _movement(cls, row: BrokerMovement) -> dict:
        amount = (
            row.operation_amount
            if row.kind == "operation"
            else row.related_payment.amount
            if row.related_payment is not None
            else row.customer_payout_amount if row.kind == "customer_payout"
            else abs(row.balance_delta)
        )
        return {
            "entry_type": "broker_movement",
            "id": str(row.id),
            "version": row.version,
            "member_id": str(row.broker_member_id) if row.broker_member_id else None,
            "account_kind": "broker" if row.broker_member_id else "house",
            "broker": row.broker.name if row.broker and row.broker.name else "Casa",
            "recorded_by": row.recorded_by.name or "Miembro",
            "kind": row.kind,
            "logical_type": cls.logical_type(row),
            "amount": str(q_money(amount)),
            "balance_delta": str(row.balance_delta),
            "balance_delta_display": fmt_amount(row.balance_delta, "USD"),
            "description": optional_text(row.description),
            "business_date": row.business_date.isoformat(),
            "occurred_at": row.occurred_at.isoformat(),
            "operation_type": row.operation_type,
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "customer_name": row.customer.display_name if row.customer else None,
            "customer_nickname": row.customer.nickname if row.customer else None,
            "customer_classification": row.customer.classification if row.customer else None,
            "portfolio_member_id": str(row.portfolio_broker_member_id) if row.portfolio_broker_member_id else None,
            "portfolio_scope": ("personal" if row.portfolio_broker_member_id else "global") if row.customer else None,
            "operation_amount_bs": str(row.operation_amount_bs) if row.operation_amount_bs is not None else None,
            "exchange_rate": str(row.exchange_rate) if row.exchange_rate is not None else None,
            "exchange_rate_source": row.exchange_rate_source,
            "monetary_basis": row.monetary_basis,
            "monetary_provenance": row.monetary_provenance,
            "multicurrency_status": ("complete" if row.operation_amount_bs is not None else "incomplete") if row.kind == "operation" else None,
            "operation_amount": str(row.operation_amount) if row.operation_amount is not None else None,
            "received_amount": str(row.received_amount) if row.received_amount is not None else None,
            "delivered_amount": str(row.delivered_amount) if row.delivered_amount is not None else None,
            "excess": str(row.excess) if row.excess is not None else None,
            "commission": str(row.commission) if row.commission is not None else None,
            "commission_rate": str(row.commission_rate) if row.commission_rate is not None else None,
            "sales_balance_rule": (row.sales_balance_rule or "legacy") if row.operation_type == "sell" else None,
            "settlement": row.settlement_status,
            "original_settlement": "pending" if row.related_receivable_id else row.settlement_status,
            "customer_balance_delta": str(q_money(
                -row.operation_amount if row.related_receivable_id else
                row.related_payment.amount if row.related_payment_id and row.related_payment else
                -row.customer_payout_amount if row.kind == "customer_payout" else
                row.operation_amount if row.operation_type == "sell" and row.settlement_status == "pending" else ZERO)),
        }

    @staticmethod
    def _customer_adjustment(row: CustomerBalanceAdjustment) -> dict:
        return {"entry_type": "customer_balance_adjustment", "id": str(row.id), "version": row.version,
                "logical_type": "adjustment", "kind": "adjustment", "customer_name": row.customer.display_name,
                "amount": str(q_money(row.amount_delta)), "customer_balance_delta": str(q_money(row.amount_delta)),
                "business_date": row.business_date.isoformat(), "occurred_at": row.occurred_at.isoformat(),
                "description": row.description, "recorded_by": row.recorded_by.name or "Miembro",
                "portfolio_scope": row.customer.classification, "editable": False}

    @staticmethod
    def _receivable(row: Receivable, broker: BusinessMember | None, paid: Decimal) -> dict:
        remaining = q_money(row.amount - paid)
        status = "pending" if paid <= Decimal("0.005") else "settled" if remaining <= Decimal("0.005") else "partial"
        return {
            "id": str(row.id),
            "version": row.version,
            "member_id": str(row.broker_member_id) if row.broker_member_id else None,
            "account_kind": "broker" if row.broker_member_id else "house",
            "broker": broker.name if broker and broker.name else "Casa",
            "customer_id": str(row.customer_id),
            "customer": row.customer.display_name,
            "customer_classification": row.customer.classification,
            "portfolio_scope": "personal" if row.broker_member_id else "global",
            "amount": str(row.amount),
            "paid": str(paid),
            "remaining": str(max(remaining, ZERO)),
            "remaining_display": fmt_amount(max(remaining, ZERO), "USD"),
            "status": status,
            "description": optional_text(row.description),
            "business_date": row.business_date.isoformat(),
        }
