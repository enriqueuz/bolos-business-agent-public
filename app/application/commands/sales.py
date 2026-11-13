"""Registrar ventas: precio acordado vs dinero realmente recibido.

- El PRECIO sale de los ítems ("lo vendí en…", o el de catálogo). El PAGO es lo que el cliente entregó, en su moneda real
  (`amount_paid` + `paid_currency`) y con su método. Son datos distintos de la misma venta.
- Todo se compara con la tasa para cobrar del negocio. No existen tasas especiales por operación: si el precio o el pago
  no cuadran, el comando no adivina, pregunta con botones (`NeedsChoice`) qué pasó:
    · precio menor al de catálogo → lo vendí más barato (solo esta venta / cambiar el precio habitual) o descuento
    · pagó menos de lo que correspondía → quedó debiendo, pagada con descuento, o corregir
  La respuesta vuelve en el mismo comando como `price_resolution` / `payment_resolution`.
- Lo que quedó debiendo es un fiado en la moneda del precio. Costo congelado por ítem para el margen.
- Precio tomado del pago: "vendí un aceite por pago móvil" + la captura del pago (sin precio dicho). Si el producto no tiene
  precio de catálogo, el precio es lo que pagó: en $ tal cual; en moneda local ÷ la tasa para cobrar (el precio queda en $).
  Si el producto ya tiene precio, se usa ese y el pago se compara con él (las mismas preguntas de siempre).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar, NoReturn
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.receivables import apply_credit_in_favor
from app.application.common import (
    assume_currency,
    bump,
    command_option,
    customer_balance,
    get_business,
    legs_brief,
    profile_of,
    reply_option,
    require_fresh_charge_rate,
)
from app.application.context import EntityRef, ExecContext
from app.application.resolvers import get_or_create_product, resolve_customer
from app.application.summaries import sale_summary
from app.db.models import Business, Product, Receivable, Sale, SaleItem
from app.domain.currencies import USD
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import (
    LOCAL_METHODS,
    USD_METHODS,
    AuditAction,
    Currency,
    EntityType,
    PaymentMethod,
    PaymentStatus,
    SaleResolution,
    SaleSettlement,
)
from app.domain.errors import NeedsChoice, ValidationFailed
from app.domain.money import (
    EffectiveRates,
    MoneyLegs,
    compute_legs,
    convert_between,
    fmt_amount,
    fmt_qty,
    fmt_rate,
    frozen_rates,
    q,
    sub_legs,
    sum_legs,
)
from app.domain.pricing import LowerThanCatalog, Match, Overpaid, Underpaid, classify_payment, classify_price

R = SaleResolution


class SaleItemInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    product: str = Field(min_length=1, max_length=200, description="Nombre del producto tal como lo dijo el dueño")
    quantity: Decimal = Field(default=Decimal(1), gt=0)
    unit_price: Decimal | None = Field(default=None, gt=0, description="Precio unitario si lo dijo; si no, se usa el del catálogo")
    currency: Currency | None = None
    # Solo lo pone una opción estructurada del sistema; no forma parte del schema de la tool del modelo.
    force_new_product: bool = False


class RecordSale(Command):
    kind: ClassVar[str] = "record_sale"

    items: list[SaleItemInput] = Field(min_length=1, max_length=50)
    customer_name: str | None = Field(default=None, min_length=1, max_length=160)
    # Pinned by a trusted choice callback, never exposed in the model's tool schema.
    customer_id: UUID | None = None
    force_new_customer: bool = False
    payment_status: PaymentStatus = PaymentStatus.PAID
    # Lo que el cliente ENTREGÓ, en la moneda en que pagó. Si se omite y está pagada, pagó exactamente lo que correspondía.
    amount_paid: Decimal | None = Field(default=None, ge=0)
    paid_currency: Currency | None = None
    payment_method: PaymentMethod | None = None
    # Una tasa que el dueño mencionó para esta venta: solo sirve para AVISARLE que su negocio cobra con otra. Nunca se aplica.
    mentioned_rate: Decimal | None = Field(default=None, gt=0)
    # Qué pasó cuando el precio o el pago no cuadran (lo ponen los botones, o el dueño si ya lo dijo):
    # precio menor al habitual → discount | this_sale_only | update_price (una sola pregunta, ver _price_question)
    price_resolution: SaleResolution | None = None
    # pagó menos de lo que correspondía → debt | discount
    payment_resolution: SaleResolution | None = None
    business_date: date | None = None
    notes: str | None = None
    # Lo pone el botón "Mantener" de la pregunta diaria de la tasa propia (nunca el modelo ni el panel).
    charge_rate_confirmed: bool = False


@dataclass(slots=True)
class BuiltItem:
    item: SaleItem
    legs: MoneyLegs  # lo que se guarda en el ítem (precio de lista si hubo descuento sobre el precio)
    stated: MoneyLegs  # a cuánto lo vendió realmente
    info: dict[str, Any]
    product: Product
    lower: LowerThanCatalog | None = None


def _item_row(position: int, product: Product, quantity: Decimal, unit_price: Decimal, currency: str, source: str,
              legs: MoneyLegs) -> SaleItem:
    return SaleItem(position=position, product_id=product.id, description=product.name, quantity=quantity,
                    unit_price_amount=q(unit_price), unit_price_currency=str(currency), price_source=source,
                    item_amount=legs.amount, item_currency=str(legs.currency), item_local=legs.local,
                    item_usd_official=legs.usd_official, item_usd_market=legs.usd_market, item_usd_applied=legs.usd_applied)


async def build_item(session: AsyncSession, business: Business, item: SaleItemInput, position: int, rates: EffectiveRates,
                     *, at_catalog_if_lower: bool = False) -> BuiltItem:
    product, created = await get_or_create_product(
        session,
        business.id,
        item.product,
        force_new=item.force_new_product,
        distinct_name_is_new=item.unit_price is not None,
    )
    lower = None
    if item.unit_price is not None:
        unit_price, source = item.unit_price, "stated"
        currency, assumed = assume_currency(unit_price, item.currency, business)
        if product.price_amount is not None and product.price_currency:
            check = classify_price(unit_price, currency, product.price_amount, product.price_currency, rates)
            lower = check if isinstance(check, LowerThanCatalog) else None
    elif product.price_amount is not None:
        unit_price, source = product.price_amount, "catalog"
        currency, assumed = product.price_currency, False
    else:
        raise ValidationFailed(
            f"No sé el precio de '{product.name}'. Si el dueño dijo o mandó (en una captura) cuánto le pagaron, vuelve a "
            "llamar record_sale con amount_paid y paid_currency: el precio sale del pago. Si no, pregúntale a cuánto lo vendió.",
            missing="unit_price",
            product=product.name,
        )
    stated = compute_legs(unit_price * item.quantity, currency, rates)
    legs, row_price, row_currency, row_source = stated, unit_price, currency, source
    if lower is not None and at_catalog_if_lower:
        # descuento sobre el precio: el ítem queda al precio habitual y la diferencia va como descuento de la venta
        row_price, row_currency, row_source = product.price_amount, product.price_currency, "catalog"
        legs = compute_legs(row_price * item.quantity, row_currency, rates)
    si = _item_row(position, product, item.quantity, row_price, row_currency, row_source, legs)
    if product.last_unit_cost_amount is not None and product.last_unit_cost_currency:
        cost = compute_legs(product.last_unit_cost_amount * item.quantity, product.last_unit_cost_currency, rates)
        si.cost_local, si.cost_usd_official, si.cost_usd_market = cost.local, cost.usd_official, cost.usd_market
        si.cost_usd_applied = cost.usd_applied
        si.cost_source = "catalog"
    info = {
        "product": product.name,
        "quantity": fmt_qty(item.quantity),
        "unit_price": fmt_amount(row_price, row_currency),
        "line_total": fmt_amount(legs.amount, legs.currency),
        "price_source": row_source,
        "currency_assumed": assumed,
        "new_product": created,
        "product_has_list_price": product.price_amount is not None,
        "cost_known": si.cost_source != "unknown",
    }
    return BuiltItem(si, legs, stated, info, product, lower)


def in_currency(legs: MoneyLegs, currency: str) -> Decimal:
    """El monto de estas patas expresado en `currency` (USD a la tasa aplicada, o la moneda local)."""
    if legs.currency == currency:
        return legs.amount
    return legs.usd_applied if currency == USD else legs.local


def infer_paid_currency(cmd: RecordSale, net: MoneyLegs, rates: EffectiveRates, said_currency: str) -> str:
    """Moneda del pago si el dueño no la dijo.

    - Con monto: la moneda en la que ese monto se parece más a lo que correspondía (3.000 contra $10 → bolívares;
      4 contra $10 → dólares). Un monto dicho pesa más que el método.
    - Sin monto: la que implica el método ("pago móvil" = moneda local, Zelle = $) o la moneda en que dijo el precio.
    """
    if cmd.paid_currency is not None:
        return str(cmd.paid_currency)
    if not cmd.amount_paid or not net.amount:
        method = cmd.payment_method.value if cmd.payment_method else None
        if method in LOCAL_METHODS:
            return rates.local_currency
        if method in USD_METHODS:
            return USD
        return said_currency  # pagó lo acordado, en la moneda en que lo dijo ("lo vendí en 2.000 Bs")
    other = USD if net.currency == rates.local_currency else rates.local_currency

    def distance(currency: str) -> Decimal:
        expected = convert_between(net.amount, net.currency, currency, rates.applied)
        return abs((cmd.amount_paid / expected).ln()) if expected > 0 else Decimal("Infinity")

    return net.currency if distance(net.currency) <= distance(other) else other


@dataclass(slots=True)
class Payment:
    amount: Decimal
    currency: str
    status: PaymentStatus
    settlement: SaleSettlement
    discount: Decimal = Decimal(0)  # en la moneda del precio
    pending: Decimal = Decimal(0)  # en la moneda del precio (fiado)
    overpaid: Decimal | None = None  # en la moneda del pago
    rate_warning: str | None = None  # el dueño mencionó otra tasa y aun así se registró (p. ej. pagó de más)


def _rate_phrase(rates: EffectiveRates, business: Business) -> str:
    profile = profile_of(business)
    return f"{profile.applied_rate_label(rates.applied_source)} de {fmt_rate(rates.applied, business.local_currency)}"


def _money_both(amount: Decimal, currency: str, rates: EffectiveRates) -> str:
    """"$10,00 (8.495,60 Bs)" o "8.000,00 Bs ($10,00)": el monto y su equivalente a la tasa para cobrar."""
    other = USD if currency != USD else rates.local_currency
    return f"{fmt_amount(amount, currency)} ({fmt_amount(convert_between(amount, currency, other, rates.applied), other)})"


def settle_payment(cmd: RecordSale, net: MoneyLegs, rates: EffectiveRates, business: Business, said_currency: str) -> Payment:
    """Cuánto se pagó, cuánto quedó debiendo y cuánto se descontó. Pregunta (NeedsChoice) si no cuadra y no se sabe por qué."""
    local = business.local_currency
    if cmd.payment_status == PaymentStatus.CREDIT and not cmd.amount_paid:
        return Payment(Decimal(0), net.currency, PaymentStatus.CREDIT, SaleSettlement.DEBT, pending=net.amount)
    paid_currency = infer_paid_currency(cmd, net, rates, said_currency)
    expected = convert_between(net.amount, net.currency, paid_currency, rates.applied)
    amount_paid = cmd.amount_paid
    mentioned = cmd.mentioned_rate
    other_rate = mentioned is not None and abs(mentioned - rates.applied) > rates.applied * Decimal("0.005")
    if amount_paid is None and other_rate and net.currency != local:
        # "se lo calculé a 800": lo que habría cobrado a esa tasa, para compararlo con la tasa del negocio
        amount_paid, paid_currency = q(net.amount * mentioned), local
        expected = convert_between(net.amount, net.currency, local, rates.applied)
    if amount_paid is None:
        if cmd.payment_status == PaymentStatus.PARTIAL:
            raise ValidationFailed("Para un pago parcial indica cuánto abonó en el momento (amount_paid).", missing="amount_paid")
        return Payment(expected, paid_currency, PaymentStatus.PAID, SaleSettlement.EXACT)

    check = classify_payment(expected, amount_paid, paid_currency, local)
    if isinstance(check, Match):
        return Payment(amount_paid, paid_currency, PaymentStatus.PAID, SaleSettlement.EXACT)
    if isinstance(check, Overpaid):
        warning = (f"Tu negocio cobra con la tasa {_rate_phrase(rates, business)} (serían {fmt_amount(expected, paid_currency)}), "
                   f"no a {fmt_rate(mentioned, local)}." if other_rate else None)
        return Payment(amount_paid, paid_currency, PaymentStatus.PAID, SaleSettlement.EXACT, overpaid=check.extra,
                       rate_warning=warning)
    assert isinstance(check, Underpaid)
    paid_in_price = convert_between(amount_paid, paid_currency, net.currency, rates.applied)
    missing_in_price = q(net.amount - paid_in_price)
    resolution = cmd.payment_resolution
    if resolution is None and cmd.payment_status in (PaymentStatus.PARTIAL, PaymentStatus.CREDIT) and not other_rate:
        resolution = R.DEBT  # el dueño ya dijo que quedó debiendo el resto
    if resolution == R.DEBT:
        if not cmd.customer_name:
            raise ValidationFailed("Lo que quedó debiendo necesita el nombre del cliente.", missing="customer_name")
        status = PaymentStatus.PARTIAL if amount_paid > 0 else PaymentStatus.CREDIT
        return Payment(amount_paid, paid_currency, status, SaleSettlement.DEBT, pending=missing_in_price)
    if resolution == R.DISCOUNT:
        return Payment(amount_paid, paid_currency, PaymentStatus.PAID, SaleSettlement.DISCOUNT, discount=missing_in_price)

    # No cuadra y no sabemos por qué: se le muestran las cuentas y se pregunta. Nunca se deduce una tasa.
    intro = ""
    if other_rate:
        intro = (f"Tu negocio está configurado para cobrar con la tasa {_rate_phrase(rates, business)}"
                 f" (serían {fmt_amount(expected, paid_currency)}), no a {fmt_rate(mentioned, local)}. ")
    same = paid_currency == net.currency
    price = (fmt_amount(expected, paid_currency) if same
             else f"{fmt_amount(expected, paid_currency)} a la tasa {_rate_phrase(rates, business)}")
    question = (f"{intro}El precio son {price} y el cliente entregó {fmt_amount(amount_paid, paid_currency)}. "
                f"Faltan *{fmt_amount(check.missing, paid_currency)}*.\n\n¿Cómo quedó la venta?")
    fixed = {"amount_paid": amount_paid, "paid_currency": paid_currency}
    debt = (command_option("Quedó debiendo", cmd, payment_resolution=R.DEBT, **fixed)
            if cmd.customer_name else reply_option("Quedó debiendo"))
    _ask(question, [debt, command_option("Pagada con descuento", cmd, payment_resolution=R.DISCOUNT, **fixed),
                    reply_option("Corregir")])


def _ask(question: str, options: list[dict[str, Any]]) -> NoReturn:
    raise NeedsChoice(question, options)


def _price_question(cmd: RecordSale, lower: list[BuiltItem], rates: EffectiveRates, business: Business) -> None:
    """Precio menor al habitual: ¿descuento, precio especial de esta venta, o cambió el precio de lista?

    UNA pregunta con los tres desenlaces. Antes eran hasta tres seguidas —"¿más barato o descuento?" → "¿solo esta
    venta o cambiar el precio?" → "¿confirmas el cambio?"— y la última volvía a preguntar lo mismo que la anterior.
    Cada pregunta cuesta DOS mensajes de WhatsApp (el nuestro y la respuesta del dueño), bastante más que el turno de
    modelo que la genera, así que juntarlas es la optimización más barata que hay.

    Los tres desenlaces son distintos en los DATOS, no solo en la redacción: únicamente `discount` deja el ítem al
    precio de lista y manda la diferencia a `discount_amount`, que es lo que alimenta `gross_sales` y
    `discounts_total`. Por eso el botón del descuento va primero: es el único que se puede perder.

    Los títulos se cortan a 20 caracteres sin avisar (BUTTON_TITLE_MAX), así que los montos van en el cuerpo.
    """
    label = profile_of(business).applied_rate_label(rates.applied_source)
    first = lower[0]
    catalog_unit = first.product.price_amount
    catalog_currency = first.product.price_currency
    stated_unit = cmd.items[first.item.position - 1].unit_price
    stated_currency = first.stated.currency
    usual = fmt_amount(catalog_unit, catalog_currency)
    if stated_currency != catalog_currency:
        usual += f", que hoy son {fmt_amount(convert_between(catalog_unit, catalog_currency, stated_currency, rates.applied), stated_currency)} a {label}"
    name = first.product.name
    said = fmt_amount(stated_unit, stated_currency)
    # LOWER_PRICE y UPDATE_PRICE_ASK ya no los produce ningún botón, pero siguen en la guarda que llama aquí: si el
    # dueño toca un botón de un mensaje anterior al despliegue, cae en esta pregunta en vez de quedarse trabado.
    new_price = fmt_amount(first.lower.stated_unit, catalog_currency)  # type: ignore[union-attr]
    _ask(f"*{name}* normalmente cuesta {usual}. Me dijiste que lo vendiste en {said}.\n\n¿Qué pasó? "
         f"Si cambio el precio, queda en {new_price} de ahora en adelante.",
         [command_option("Le hice un descuento", cmd, price_resolution=R.DISCOUNT),
          command_option("Precio especial", cmd, price_resolution=R.THIS_SALE_ONLY),
          command_option("Cambiar el precio", cmd, price_resolution=R.UPDATE_PRICE)])


def _risk(cmd: RecordSale, ctx: ExecContext, res: CommandResult) -> str | None:
    # Crear un cliente nuevo no es un riesgo financiero independiente. Las
    # coincidencias dudosas llegan como Ambiguous y no escriben nada.
    del cmd, ctx, res
    return None


async def price_from_payment(session: AsyncSession, business: Business, cmd: RecordSale,
                             rates: EffectiveRates) -> RecordSale | None:
    """Venta de UN producto sin precio dicho ni de catálogo, pero con el pago (p. ej. una captura de pago móvil): el precio
    es lo que pagó. En moneda local se divide entre la tasa para cobrar y el precio queda en $. None si no aplica."""
    if len(cmd.items) != 1 or cmd.items[0].unit_price is not None or not cmd.amount_paid:
        return None
    if cmd.payment_status != PaymentStatus.PAID:
        return None  # si quedó debiendo, lo que pagó no es el precio: hay que preguntarlo
    item = cmd.items[0]
    product, _ = await get_or_create_product(
        session, business.id, item.product, force_new=item.force_new_product, distinct_name_is_new=True
    )
    if product.price_amount is not None:
        return None  # ya tiene precio: se compara el pago con él
    currency = str(cmd.paid_currency) if cmd.paid_currency else None
    method = cmd.payment_method.value if cmd.payment_method else None
    if currency is None and method in LOCAL_METHODS:
        currency = business.local_currency
    elif currency is None and method in USD_METHODS:
        currency = USD
    elif currency is None:
        currency, _ = assume_currency(cmd.amount_paid, None, business)
    if rates.applied <= 0 and currency != USD:
        return None
    total_usd = cmd.amount_paid if currency == USD else cmd.amount_paid / rates.applied
    unit = q(total_usd / item.quantity)
    return cmd.model_copy(update={"items": [item.model_copy(update={"unit_price": unit, "currency": USD})],
                                  "paid_currency": currency})


@command_handler(RecordSale, risk=_risk)
async def record_sale(cmd: RecordSale, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    rates = await deps.rates.effective(session, ctx.business_id)
    if cmd.payment_status != PaymentStatus.PAID and not (cmd.customer_name or cmd.customer_id):
        raise ValidationFailed("Una venta fiada o con pago parcial necesita el nombre del cliente.", missing="customer_name")
    from_payment = await price_from_payment(session, business, cmd, rates)
    if from_payment is not None:
        cmd = from_payment

    # 1) Precio: ítems al precio dicho (o de catálogo). Si fue un descuento sobre el precio, al habitual + descuento.
    price_discount = cmd.price_resolution == R.DISCOUNT
    built = [await build_item(session, business, item, position, rates, at_catalog_if_lower=price_discount)
             for position, item in enumerate(cmd.items, start=1)]
    if from_payment is not None:  # que el recibo diga de dónde salió el precio
        built[0].item.price_source = "payment"
        built[0].info["price_source"] = "payment"
        built[0].info["price_from_payment"] = {
            "paid": fmt_amount(cmd.amount_paid, str(cmd.paid_currency)),
            "rate": (f"{profile_of(business).applied_rate_label(rates.applied_source)} "
                     f"{fmt_rate(rates.applied, business.local_currency)}" if cmd.paid_currency != USD else None)}
    lower = [b for b in built if b.lower is not None]
    list_total = sum_legs([b.legs for b in built])
    stated_total = sum_legs([b.stated for b in built])
    await require_fresh_charge_rate(session, ctx, business, rates, cmd, involves_local=any(
        b.stated.currency == business.local_currency for b in built) or cmd.paid_currency == business.local_currency
        or (cmd.payment_method is not None and cmd.payment_method.value in LOCAL_METHODS))
    if lower and cmd.price_resolution in (None, R.LOWER_PRICE, R.UPDATE_PRICE_ASK):
        _price_question(cmd, lower, rates, business)

    discount_amount = Decimal(0)  # en la moneda del total
    if price_discount and lower:
        discount_amount = q(list_total.amount - in_currency(stated_total, list_total.currency))
    rates_frozen = frozen_rates(list_total)
    net = sub_legs(list_total, compute_legs(discount_amount, list_total.currency, rates_frozen)) if discount_amount else list_total

    # 2) Pago real contra lo que correspondía (el precio neto) a la tasa para cobrar.
    payment = settle_payment(cmd, net, rates, business, stated_total.currency)
    if payment.discount:
        discount_amount = q(discount_amount + payment.discount)
        net = sub_legs(list_total, compute_legs(discount_amount, list_total.currency, rates_frozen))
    settlement = (SaleSettlement.DISCOUNT if discount_amount and payment.settlement != SaleSettlement.DEBT
                  else payment.settlement)

    customer, customer_created = (None, False)
    if cmd.customer_name or cmd.customer_id:
        customer, customer_created = await resolve_customer(
            session, ctx.business_id, cmd.customer_name, create_if_missing=True, force_new=cmd.force_new_customer,
            customer_id=cmd.customer_id,
        )

    # 3) Precio habitual nuevo (solo con confirmación explícita).
    price_updated = None
    if cmd.price_resolution == R.UPDATE_PRICE and lower:
        price_updated = []
        for b in lower:
            before = audit.snapshot(b.product)
            old = fmt_amount(b.product.price_amount, b.product.price_currency)
            b.product.price_amount = q(b.lower.stated_unit)  # type: ignore[union-attr]  (ya en la moneda del catálogo)
            bump(b.product)
            audit.record(session, ctx, entity_type=EntityType.PRODUCT, entity_id=b.product.id, action=AuditAction.UPDATE,
                         before=before, after=audit.snapshot(b.product), command=cmd.kind)
            price_updated.append({"product": b.product.name, "from": old,
                                  "to": fmt_amount(b.product.price_amount, b.product.price_currency)})

    sale = Sale(
        business_id=ctx.business_id,
        customer_id=customer.id if customer else None,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        payment_status=payment.status.value,
        payment_method=cmd.payment_method.value if cmd.payment_method else None,
        notes=cmd.notes,
        served_by_member_id=ctx.actor.member_id,
        source_message_id=ctx.source_message_id,
        list_total_amount=list_total.amount,
        settlement=settlement.value,
    )
    sale.set_total(net)
    sale.set_discount(compute_legs(discount_amount, list_total.currency, rates_frozen))
    sale.set_paid(compute_legs(payment.amount, payment.currency, rates))
    sale.items = [b.item for b in built]
    sale.customer = customer
    session.add(sale)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.SALE, entity_id=sale.id, action=AuditAction.CREATE,
                 before=None, after=audit.snapshot(sale), command=cmd.kind)
    affected = [EntityRef(EntityType.SALE, sale.id, version=sale.version, summary=sale_summary(sale))]

    profile = profile_of(business)
    touches_local = payment.currency != net.currency or net.currency == business.local_currency
    data: dict = {
        "sale": {
            "business_date": bdate.isoformat(),
            "items": [b.info for b in built],
            "list_total": legs_brief(list_total),
            "discount": (_money_both(discount_amount, list_total.currency, rates) if discount_amount else None),
            "total": legs_brief(net),
            "payment_status": payment.status.value,
            "settlement": settlement.value,
            "paid": fmt_amount(payment.amount, payment.currency),
            "paid_currency": payment.currency,
            "paid_in_other_currency": payment.currency != net.currency,
            "payment_method": cmd.payment_method.value if cmd.payment_method else None,
            "overpaid": fmt_amount(payment.overpaid, payment.currency) if payment.overpaid else None,
            "rate_warning": payment.rate_warning,
            "rate": ({"label": profile.applied_rate_label(rates.applied_source),
                      "rate": fmt_rate(rates.applied, business.local_currency)} if touches_local else None),
            "price_kept": ({"product": lower[0].product.name,
                            "usual": fmt_amount(lower[0].product.price_amount, lower[0].product.price_currency),
                            "this_sale": _money_both(stated_total.amount, stated_total.currency, rates)}
                           if lower and cmd.price_resolution == R.THIS_SALE_ONLY else None),
            "price_updated": price_updated,
        },
        "customer": customer.display_name if customer else None,
        "customer_created": customer_created,
        "rates_used": {"official": str(rates.official), "market": str(rates.market), "applied": str(rates.applied),
                       "applied_source": rates.applied_source.value},
    }
    if payment.pending > 0 and customer is not None:
        legs = compute_legs(payment.pending, net.currency, rates)
        receivable = Receivable(
            business_id=ctx.business_id,
            customer_id=customer.id,
            sale_id=sale.id,
            business_date=bdate,
            occurred_at=sale.occurred_at,
            description=", ".join(f"{i['quantity']}x {i['product']}" for i in data["sale"]["items"])[:300],
            source_message_id=ctx.source_message_id,
        )
        receivable.set_amount_legs(legs)
        receivable.customer = customer
        session.add(receivable)
        await session.flush()
        audit.record(session, ctx, entity_type=EntityType.RECEIVABLE, entity_id=receivable.id, action=AuditAction.CREATE,
                     before=None, after=audit.snapshot(receivable), command=cmd.kind)
        affected.append(EntityRef(EntityType.RECEIVABLE, receivable.id, version=receivable.version))
        data["receivable"] = {
            "pending": fmt_amount(payment.pending, net.currency),
            "pending_today": (fmt_amount(convert_between(payment.pending, net.currency, business.local_currency, rates.applied),
                                         business.local_currency) if net.currency != business.local_currency else None),
            "credit_applied": await apply_credit_in_favor(session, ctx, customer.id),
            "customer_balance": await customer_balance(session, ctx.business_id, customer.id),
        }
    return CommandResult(kind=cmd.kind, data=data, affected=affected)
