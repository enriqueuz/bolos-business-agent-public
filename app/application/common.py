"""Helpers compartidos por comandos y queries."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import json_safe
from app.application.context import ExecContext
from app.db.models import Business, Customer, CustomerPayment, Receivable
from app.domain.countries import CountryProfile, country_profile
from app.domain.currencies import USD
from app.domain.dates import days_ago_label, local_today
from app.domain.enums import ActorType, RateAppliedSource
from app.domain.errors import NeedsChoice, NotFound, Stale
from app.domain.money import EffectiveRates, MoneyLegs, ensure_supported, fmt_amount, fmt_rate, q


async def get_business(session: AsyncSession, business_id: UUID) -> Business:
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFound("Negocio no encontrado.")
    return business


async def lock_business(session: AsyncSession, business_id: UUID) -> Business:
    """Bloquea la fila del negocio (`FOR UPDATE`) y la relee.

    El CommandBus ya cargó el negocio al empezar; si otra operación lo cambió (y subió su versión) mientras esperábamos
    el candado, la copia en memoria quedó vieja y guardarla chocaría con el bloqueo optimista. Por eso se descarta
    y se vuelve a leer, ya con el candado tomado.
    """
    cached = await session.get(Business, business_id)
    if cached is not None:
        session.expire(cached)
    business = await session.get(Business, business_id, with_for_update=True)
    if business is None:
        raise NotFound("Negocio no encontrado.")
    return business


async def lock_customers(session: AsyncSession, business_id: UUID, *customer_ids: UUID | None) -> None:
    """Pone en fila las operaciones de fiado y abono de un mismo cliente (`SELECT ... FOR UPDATE` sobre su fila).

    Un abono decide a qué fiado se aplica leyendo el estado actual; si dos abonos del mismo cliente leen a la vez, los dos
    ven el mismo fiado como abierto y lo sobrepagan. Con este bloqueo el segundo espera a que el primero termine (la
    transacción dura milisegundos) y entonces lee el estado ya actualizado. Se llama ANTES de leer fiados o asignaciones.

    Es `FOR NO KEY UPDATE` y no `FOR UPDATE` a propósito: no choca con el candado que PostgreSQL toma solo al insertar un
    abono o fiado que apunta al cliente (llave foránea). Con `FOR UPDATE`, dos abonos que ya insertaron su fila y luego
    piden el bloqueo se esperarían mutuamente (deadlock).

    Varios clientes se bloquean siempre en el mismo orden (por id) para que dos operaciones cruzadas no se traben entre sí.
    Se sostiene hasta el fin de la transacción; el tiempo máximo de espera lo pone la conexión (`lock_timeout`).
    """
    ids = sorted({c for c in customer_ids if c})
    if not ids:
        return
    await session.execute(select(Customer.id).where(Customer.business_id == business_id, Customer.id.in_(ids))
                          .order_by(Customer.id).with_for_update(key_share=True))


def check_version(entity: Any, expected_version: int | None, label: str) -> None:
    if expected_version is not None and entity.version != expected_version:
        raise Stale(
            f"Este {label} cambió desde que lo consultaste (quizás desde el panel). Vuelve a buscarlo antes de modificarlo.",
            current_version=entity.version,
        )


def bump(entity: Any) -> None:
    entity.version = (entity.version or 1) + 1


def profile_of(business: Business) -> CountryProfile:
    return country_profile(business.country_code)


def command_option(title: str, command: Any, *, description: str | None = None, confirmed: bool = False,
                   **overrides: Any) -> dict[str, Any]:
    """Opción de un `NeedsChoice` que, al tocarla, ejecuta `command` (con `overrides`) sin pasar por el LLM."""
    return {"title": title, "description": description, "action": "command", "command_kind": command.kind,
            "command_payload": {**command.model_dump(mode="json"), **json_safe(overrides)}, "confirmed": confirmed}


def reply_option(title: str, description: str | None = None) -> dict[str, Any]:
    """Opción de un `NeedsChoice` cuyo título vuelve al agente como la respuesta del dueño (p. ej. "Actualizar tasa")."""
    return {"title": title, "description": description, "action": "reply", "command_kind": None, "command_payload": None,
            "confirmed": False}


async def require_fresh_charge_rate(session: AsyncSession, ctx: ExecContext, business: Business, rates: EffectiveRates,
                                    command: Any, *, involves_local: bool) -> None:
    """Tasa propia para cobrar: se confirma una vez al día, antes de la primera operación que la usa.

    Solo aplica si el negocio cobra con su tasa propia, el movimiento toca moneda local (su valor en $ depende de la
    tasa) y lo pide el dueño por WhatsApp (el panel muestra la tasa a la vista). No pregunta si ya la confirmó o la fijó
    hoy. Si el comando llega con `charge_rate_confirmed` (botón "Mantener"), marca el día y sigue.
    """
    if not involves_local or rates.applied_source != RateAppliedSource.CUSTOM or ctx.actor.type != ActorType.AGENT:
        return
    tz = ZoneInfo(business.timezone)
    today = local_today(business.timezone, ctx.now)
    set_on = rates.applied_set_at.astimezone(tz).date() if rates.applied_set_at else None
    confirmed_on = business.charge_rate_confirmed_on
    if today in (confirmed_on, set_on):
        return
    if getattr(command, "charge_rate_confirmed", False):
        # Columna de control, no un dato que el dueño edite: no sube la versión del negocio (no invalida el panel).
        await session.execute(update(Business).where(Business.id == business.id).values(charge_rate_confirmed_on=today))
        return
    last = max(d for d in (confirmed_on, set_on) if d is not None) if (confirmed_on or set_on) else None
    rate = fmt_rate(rates.applied, business.local_currency)
    symbol = profile_of(business).local.symbol
    when = f" y la fijaste {days_ago_label(last, today)}" if last else ""
    raise NeedsChoice(
        f"Tu tasa para cobrar es *{rate} {symbol}/$*{when}. ¿La mantenemos para hoy?",
        [command_option(f"Mantener {rate}", command, charge_rate_confirmed=True), reply_option("Actualizar tasa")],
    )


def assume_currency(amount: Decimal, currency: str | None, business: Business) -> tuple[str, bool]:
    """Moneda explícita, o asumida por magnitud según el perfil del país (el resultado lo marca `currency_assumed`)."""
    if currency is not None:
        ensure_supported(str(currency), business.local_currency)
        return str(currency), False
    threshold = profile_of(business).assume_usd_below
    return (USD if amount < threshold else business.local_currency), True


def real_cost_brief(entity: Any, legs: MoneyLegs, business: Business) -> dict[str, Any] | None:
    """Para salidas pagadas en moneda local: su costo real (tasa real de los Bs) y la referencia oficial, cada una con su nombre.

    Diferencia cambiaria = costo real − referencia oficial (positiva: los bolívares le costaron más que a la oficial).
    """
    if legs.currency != business.local_currency:
        return None
    profile = profile_of(business)
    out: dict[str, Any] = {"official_label": profile.official_rate_label,
                           "official_ref": fmt_amount(legs.usd_official, USD),
                           "applied_equivalent": fmt_amount(legs.usd_applied, USD)}
    if entity.amount_usd_real is None:
        return {**out, "known": False}
    diff = q(entity.amount_usd_real - legs.usd_official)
    return {**out, "known": True, "usd": fmt_amount(entity.amount_usd_real, USD),
            "rate": fmt_rate(entity.rate_funds, business.local_currency), "unit": f"{profile.local.symbol}/$",
            "fx_diff": fmt_amount(diff, USD), "fx_diff_raw": str(diff)}


def legs_brief(legs: MoneyLegs) -> dict[str, str]:
    return {
        "amount": str(legs.amount),
        "currency": legs.currency,
        "local_currency": legs.local_currency,
        "display": fmt_amount(legs.amount, legs.currency),
        "local": str(legs.local),
        "usd_official": str(legs.usd_official),
        "usd_market": str(legs.usd_market),
        "usd_applied": str(legs.usd_applied),
        "rate_market": str(legs.rate_market),
        "rate_applied": str(legs.rate_applied),
        "rate_applied_source": legs.rate_applied_source.value,
    }


async def customer_balance(
    session: AsyncSession,
    business_id: UUID,
    customer_id: UUID,
    broker_member_id: UUID | None = None,
) -> dict[str, Any]:
    """Brokerage uses the signed account; retail retains its multicurrency FIFO balance."""
    business = await get_business(session, business_id)
    if business.vertical == "brokerage":
        from app.application.customer_accounts import customer_account_balance

        customer = await session.get(Customer, customer_id)
        if customer is None or customer.business_id != business_id:
            raise NotFound("Cliente no encontrado.")
        portfolio = customer.owner_broker_member_id if customer.classification == "personal" else None
        return await customer_account_balance(session, business_id, customer.id, portfolio)
    rows = (
        await session.execute(
            select(Receivable.currency, func.sum(_remaining_expr()))
            .select_from(Receivable)
            .where(
                Receivable.business_id == business_id,
                Receivable.customer_id == customer_id,
                Receivable.broker_member_id.is_not_distinct_from(broker_member_id),
                Receivable.deleted_at.is_(None),
            )
            .group_by(Receivable.currency)
        )
    ).all()
    by_currency = {cur: q(Decimal(total or 0)) for cur, total in rows}
    credit_rows = (
        await session.execute(
            select(CustomerPayment.currency, func.sum(CustomerPayment.unapplied_amount))
            .where(
                CustomerPayment.business_id == business_id,
                CustomerPayment.customer_id == customer_id,
                CustomerPayment.broker_member_id.is_not_distinct_from(broker_member_id),
                CustomerPayment.deleted_at.is_(None),
                CustomerPayment.unapplied_amount > 0,
            )
            .group_by(CustomerPayment.currency)
        )
    ).all()
    owed = {c: str(v) for c, v in by_currency.items() if v > Decimal("0.005")}
    credit = {c: str(q(Decimal(v))) for c, v in credit_rows if v and v > Decimal("0.005")}
    display = " + ".join(fmt_amount(Decimal(v), c) for c, v in sorted(owed.items())) or fmt_amount(Decimal(0), USD)
    return {"owed": owed, "credit_in_favor": credit, "display": display}


def _remaining_expr():
    from sqlalchemy import literal_column

    # amount - asignaciones de abonos no borrados
    return literal_column(
        "receivables.amount - coalesce((SELECT sum(pa.applied_amount) FROM payment_allocations pa "
        "JOIN customer_payments cp ON cp.id = pa.payment_id "
        "WHERE pa.receivable_id = receivables.id AND cp.deleted_at IS NULL), 0)"
    )


async def receivable_paid(session: AsyncSession, receivable_id: UUID) -> Decimal:
    from app.db.models import PaymentAllocation

    value = (
        await session.execute(
            select(func.coalesce(func.sum(PaymentAllocation.applied_amount), 0))
            .join(CustomerPayment, CustomerPayment.id == PaymentAllocation.payment_id)
            .where(PaymentAllocation.receivable_id == receivable_id, CustomerPayment.deleted_at.is_(None))
        )
    ).scalar_one()
    return q(Decimal(value))
