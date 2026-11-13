"""Tools de escritura. Cada una arma un comando y delega en el CommandBus vía `execute_command`.

Los tipos que ve el modelo usan float/str simples (schemas amigables para cualquier proveedor); la conversión a
Decimal y la validación estricta ocurren en los comandos.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from app.agent.deps import TurnDeps
from app.agent.tools.registry import FAST_AND_AGENT, execute_command, resolve_handle, tool
from app.application.commands.cash import CloseCash
from app.application.commands.catalog import DeleteProduct, SetProductCost, SetProductPrice
from app.application.commands.customers import EditCustomerProfile
from app.application.commands.exchanges import RecordExchange
from app.application.commands.expenses import RecordExpense
from app.application.commands.notes import RememberNote
from app.application.commands.purchases import PurchaseItemInput, RecordPurchase
from app.application.commands.rates import SetChargeRateMode, SetRate
from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.commands.records import DeleteRecord, EditRecord, RecordChanges
from app.application.commands.sales import RecordSale, SaleItemInput
from app.domain.enums import (
    BusinessVertical,
    ChargeRateMode,
    Currency,
    ExpenseCategory,
    NoteKind,
    PaymentMethod,
    PaymentStatus,
    RateScope,
    SaleResolution,
)
from app.domain.money import to_decimal

# Lo que el modelo puede decidir por el dueño cuando él ya lo dijo; los pasos intermedios (preguntas) son de los botones.
OWNER_PRICE_RESOLUTIONS = {SaleResolution.THIS_SALE_ONLY, SaleResolution.DISCOUNT}
OWNER_PAYMENT_RESOLUTIONS = {SaleResolution.DEBT, SaleResolution.DISCOUNT}


def _d(value: float | None):
    return to_decimal(value) if value is not None else None


def _cur(ctx: RunContext[TurnDeps], value: Currency | None) -> Currency | None:
    """Si el modelo omitió la moneda pero el mensaje tiene una marca inequívoca ($ o la moneda local), se usa esa (determinista)."""
    if value is not None:
        return value
    hint = ctx.deps.business.profile.currency_hint(ctx.deps.user_text)
    return Currency(hint) if hint else None


class SaleItemArg(BaseModel):
    product: str = Field(description="Producto tal como lo dijo el dueño, ej. 'bujía NGK', 'cadena 428'")
    quantity: float = Field(default=1, gt=0)
    unit_price: float | None = Field(default=None, gt=0, description="Precio UNITARIO si lo dijo. Si dijo el total de varias unidades, divide.")
    currency: Currency | None = Field(default=None, description="USD o la moneda local del negocio (código ISO), según las marcas de moneda del mensaje")


class PurchaseItemArg(BaseModel):
    product: str
    quantity: float = Field(default=1, gt=0)
    unit_cost: float = Field(gt=0, description="Costo UNITARIO")
    currency: Currency | None = None


class ChangesArg(BaseModel):
    amount: float | None = Field(default=None, gt=0)
    currency: Currency | None = None
    description: str | None = None
    category: ExpenseCategory | None = None
    payment_method: PaymentMethod | None = None
    business_date: date | None = None
    customer_name: str | None = None
    force_new_customer: bool = False
    supplier_name: str | None = None
    item_position: int | None = Field(default=None, ge=1, description="Solo ventas con varios ítems: cuál (1 = primero)")
    quantity: float | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, gt=0)
    payment_status: PaymentStatus | None = None
    amount_paid: float | None = Field(default=None, ge=0)
    paid_currency: Currency | None = Field(default=None, description="Solo ventas: moneda en que pagó el cliente")
    text: str | None = Field(default=None, description="Solo notas: el nuevo texto")

    def to_changes(self) -> RecordChanges:
        data = self.model_dump(exclude_none=True)
        for k in ("amount", "quantity", "unit_price", "amount_paid"):
            if k in data:
                data[k] = to_decimal(data[k])
        return RecordChanges(**data)


def _args(**kwargs: Any) -> dict[str, Any]:
    return {k: (v.model_dump() if isinstance(v, BaseModel) else [x.model_dump() if isinstance(x, BaseModel) else x for x in v]
                if isinstance(v, list) else v) for k, v in kwargs.items() if v is not None}


@tool("Registrar una venta (uno o varios productos; pagada, fiada o con pago parcial)", kind="write", lanes=FAST_AND_AGENT)
async def record_sale(
    ctx: RunContext[TurnDeps],
    items: list[SaleItemArg],
    payment_status: PaymentStatus = PaymentStatus.PAID,
    customer_name: str | None = None,
    amount_paid: float | None = None,
    paid_currency: Currency | None = None,
    payment_method: PaymentMethod | None = None,
    mentioned_rate: float | None = None,
    price_resolution: SaleResolution | None = None,
    payment_resolution: SaleResolution | None = None,
    business_date: date | None = None,
    force_new_customer: bool = False,
) -> dict[str, Any]:
    """Registra una venta NUEVA. Úsala solo cuando el dueño cuenta que vendió algo.

    PRECIO y PAGO son cosas distintas: "lo vendí en 2.000 Bs" / "cuesta $10" es el PRECIO (unit_price del ítem);
    "me pagó 2.000 Bs" / "recibí 8.000 Bs por pago móvil" es lo que el cliente ENTREGÓ (amount_paid + paid_currency).
    Un producto puede costar $10 y pagarse en bolívares: pasa el precio en USD y el pago en la moneda local.
    Si no dijo el precio, omite unit_price: se usa el del catálogo, o si el producto no tiene precio y pasas lo que pagó
    (amount_paid), el precio sale del pago (en Bs, dividido entre su tasa para cobrar). Si manda una CAPTURA de pago
    (pago móvil, transferencia, Zelle, Binance), el monto de la captura es amount_paid con su moneda y método: pásalo
    siempre y NO preguntes el precio. Solo si el sistema responde missing=unit_price, pregúntale al dueño. Si se lo llevó fiado usa payment_status=credit con customer_name;
    si pagó una parte y el resto lo debe, payment_status=partial con amount_paid y customer_name.
    Si el precio o el pago no cuadran con la tasa del negocio, el sistema le pregunta al dueño con botones: no calcules
    tú las diferencias ni inventes tasas.
    «Vendí 3 cadenas 428»: llama con product="cadenas 428", quantity=3 y unit_price omitido;
    no preguntes precio antes de esta llamada. «Vendí el espejo en 2.000 Bs»: unit_price=2000,
    currency=VES, amount_paid omitido. payment_status=paid no declara un importe recibido adicional.
    Una venta pagada permite cliente anónimo: «a este cliente» no obliga a preguntar su nombre.
    Omite customer_name cuando no se identifica; solo fiado o pago parcial exige cliente.

    Args:
        items: Productos vendidos (con el PRECIO al que se vendió cada uno, si lo dijo).
        payment_status: paid (pagó todo), credit (fiado), partial (pagó una parte y debe el resto).
        customer_name: Cliente, obligatorio si es fiado o parcial.
        amount_paid: Lo que el cliente ENTREGÓ: pásalo SIEMPRE que el dueño diga o muestre cuánto le pagaron ("me pagó",
            "me llegó el pago de", el monto de una captura de pago), aunque no haya dicho el precio.
            Omítelo si solo declara el precio de venta; nunca copies unit_price en este campo.
        paid_currency: Moneda en que pagó (USD o la local), si la dijo o la implica el método (pago móvil = moneda local).
        payment_method: Cómo pagó (efectivo, pago móvil, zelle…), si lo dijo. El método NO dice la moneda: efectivo puede ser $ o Bs.
        mentioned_rate: Si el dueño dijo a qué tasa calculó ESTA venta ("se lo calculé a 800"). No se aplica: el sistema le
            avisa con qué tasa cobra su negocio.
        price_resolution: Solo si el dueño YA explicó un precio menor al habitual: this_sale_only (lo vendió más barato solo
            esta vez) o discount (fue un descuento). Si no lo explicó, omítelo: el sistema pregunta.
        payment_resolution: Solo si el dueño YA explicó que pagó menos: debt (quedó debiendo) o discount (aceptó ese pago
            como completo). Si no lo explicó, omítelo: el sistema pregunta.
        business_date: Fecha si NO fue hoy (ej. "ayer"), en formato YYYY-MM-DD.
        force_new_customer: true solo si el dueño confirmó que es un cliente nuevo distinto a los candidatos.
    """
    args = _args(items=items, payment_status=payment_status, customer_name=customer_name, amount_paid=amount_paid,
                 paid_currency=paid_currency, payment_method=payment_method, mentioned_rate=mentioned_rate,
                 price_resolution=price_resolution, payment_resolution=payment_resolution, business_date=business_date,
                 force_new_customer=force_new_customer or None)
    return await execute_command(ctx, "record_sale", args, lambda: RecordSale(
        items=[SaleItemInput(product=i.product, quantity=to_decimal(i.quantity), unit_price=_d(i.unit_price), currency=_cur(ctx, i.currency))
               for i in items],
        payment_status=payment_status, customer_name=customer_name, amount_paid=_d(amount_paid), paid_currency=paid_currency,
        payment_method=payment_method, mentioned_rate=_d(mentioned_rate),
        price_resolution=price_resolution if price_resolution in OWNER_PRICE_RESOLUTIONS else None,
        payment_resolution=payment_resolution if payment_resolution in OWNER_PAYMENT_RESOLUTIONS else None,
        business_date=business_date, force_new_customer=force_new_customer))


@tool("Registrar un gasto operativo (alquiler, luz, internet, sueldos, transporte, comida, mantenimiento…)", kind="write",
      lanes=FAST_AND_AGENT)
async def record_expense(
    ctx: RunContext[TurnDeps],
    description: str,
    amount: float,
    category: ExpenseCategory,
    currency: Currency | None = None,
    payment_method: PaymentMethod | None = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """Registra un GASTO operativo del negocio. NO uses esto para mercancía que se va a revender (eso es record_purchase),
    ni para cambios de moneda (eso es record_exchange), ni para mover dinero entre cuentas de la misma moneda o sacar
    efectivo (eso no se registra).

    Args:
        description: Qué se pagó, corto (ej. "luz", "almuerzo", "flete").
        amount: Monto.
        category: Categoría del gasto.
        currency: USD o la moneda local del negocio, según lo que dijo.
        payment_method: Cómo se pagó, si lo dijo.
        business_date: Fecha si NO fue hoy (YYYY-MM-DD).
    """
    args = _args(description=description, amount=amount, category=category, currency=currency, payment_method=payment_method,
                 business_date=business_date)
    return await execute_command(ctx, "record_expense", args, lambda: RecordExpense(
        description=description, amount=to_decimal(amount), category=category, currency=_cur(ctx, currency), payment_method=payment_method,
        business_date=business_date))


@tool("Registrar una compra de mercancía para revender (con o sin detalle de productos y costos)", kind="write",
      lanes=FAST_AND_AGENT)
async def record_purchase(
    ctx: RunContext[TurnDeps],
    description: str | None = None,
    amount: float | None = None,
    currency: Currency | None = None,
    supplier_name: str | None = None,
    items: list[PurchaseItemArg] | None = None,
    payment_method: PaymentMethod | None = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """Registra una COMPRA de mercancía/inventario para vender. Si el dueño dio productos con costo unitario, pásalos
    en items (así se conoce el costo y se puede calcular la ganancia). Si solo dio un total, usa description+amount.

    Args:
        description: Qué se compró (obligatorio si no hay items).
        amount: Total de la compra (obligatorio si no hay items).
        currency: USD o la moneda local del negocio.
        supplier_name: Proveedor, si lo dijo.
        items: Productos con cantidad y costo unitario.
        payment_method: Cómo se pagó.
        business_date: Fecha si NO fue hoy (YYYY-MM-DD).
    """
    args = _args(description=description, amount=amount, currency=currency, supplier_name=supplier_name, items=items or None,
                 payment_method=payment_method, business_date=business_date)
    return await execute_command(ctx, "record_purchase", args, lambda: RecordPurchase(
        description=description, amount=_d(amount), currency=_cur(ctx, currency), supplier_name=supplier_name,
        items=[PurchaseItemInput(product=i.product, quantity=to_decimal(i.quantity), unit_cost=to_decimal(i.unit_cost),
                                 currency=_cur(ctx, i.currency)) for i in (items or [])],
        payment_method=payment_method, business_date=business_date))


@tool("Registrar que un cliente pagó/abonó a lo que debe (se aplica a sus fiados del más viejo al más nuevo)", kind="write",
      lanes=FAST_AND_AGENT)
async def record_payment(
    ctx: RunContext[TurnDeps],
    customer_name: str,
    amount: float,
    currency: Currency | None = None,
    payment_method: PaymentMethod | None = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """Registra un ABONO de un cliente a su deuda (fiado).

    Args:
        customer_name: Cliente que pagó.
        amount: Monto abonado.
        currency: USD o la moneda local del negocio.
        payment_method: Cómo pagó.
        business_date: Fecha si NO fue hoy (YYYY-MM-DD).
    """
    args = _args(customer_name=customer_name, amount=amount, currency=currency, payment_method=payment_method,
                 business_date=business_date)
    return await execute_command(ctx, "record_payment", args, lambda: RecordPayment(
        customer_name=customer_name, amount=to_decimal(amount), currency=_cur(ctx, currency), payment_method=payment_method,
        business_date=business_date))


@tool("Anotar una deuda de un cliente sin detallar la venta (\"Moisés me debe 20$\")", kind="write")
async def create_receivable(
    ctx: RunContext[TurnDeps],
    customer_name: str,
    amount: float,
    currency: Currency | None = None,
    description: str | None = None,
    business_date: date | None = None,
    force_new_customer: bool = False,
) -> dict[str, Any]:
    """Crea un FIADO (cuenta por cobrar) sin venta detallada. Si el dueño describió qué se llevó con precio,
    prefiere record_sale con payment_status=credit.

    Args:
        customer_name: Cliente que debe.
        amount: Monto adeudado.
        currency: USD o la moneda local del negocio.
        description: De qué es la deuda.
        business_date: Fecha si NO fue hoy (YYYY-MM-DD).
        force_new_customer: true solo si el dueño confirmó que es un cliente nuevo.
    """
    args = _args(customer_name=customer_name, amount=amount, currency=currency, description=description,
                 business_date=business_date, force_new_customer=force_new_customer or None)
    return await execute_command(ctx, "create_receivable", args, lambda: CreateReceivable(
        customer_name=customer_name, amount=to_decimal(amount), currency=_cur(ctx, currency), description=description,
        business_date=business_date, force_new_customer=force_new_customer))


@tool("Corregir cualquier registro (venta, gasto, compra, fiado, abono o nota), de cualquier fecha", kind="write")
async def edit_record(ctx: RunContext[TurnDeps], handle: str, changes: ChangesArg) -> dict[str, Any]:
    """Corrige un registro existente identificado por su referencia (R1, R2…). Obtén la referencia de la lista de
    registros del contexto (mensaje citado, registros recientes) o con find_records. NUNCA inventes una referencia.
    Pasa solo los campos que cambian.

    Args:
        handle: Referencia del registro, ej. "R3".
        changes: Campos a cambiar.
    """
    target = await resolve_handle(ctx, handle)
    args = {"handle": handle, "changes": changes.model_dump(exclude_none=True)}
    if isinstance(target, dict):
        ctx.deps.tool_results.append(target)
        return target
    return await execute_command(ctx, "edit_record", args, lambda: EditRecord(
        entity_type=target.entity_type, entity_id=target.entity_id, expected_version=target.version,
        changes=changes.to_changes()), target_handle=target)


@tool("Borrar cualquier registro (siempre pide confirmación al dueño)", kind="write")
async def delete_record(ctx: RunContext[TurnDeps], handle: str) -> dict[str, Any]:
    """Borra un registro identificado por su referencia (R1, R2…). El sistema siempre pide confirmación al dueño.

    Args:
        handle: Referencia del registro, ej. "R3".
    """
    target = await resolve_handle(ctx, handle)
    if isinstance(target, dict):
        ctx.deps.tool_results.append(target)
        return target
    return await execute_command(ctx, "delete_record", {"handle": handle}, lambda: DeleteRecord(
        entity_type=target.entity_type, entity_id=target.entity_id, expected_version=target.version), target_handle=target)


@tool("Fijar o cambiar el precio de venta de un producto", kind="write")
async def set_product_price(ctx: RunContext[TurnDeps], product_name: str, price: float, currency: Currency) -> dict[str, Any]:
    """Guarda el precio de lista de un producto (lo crea en el catálogo si no existe).

    Args:
        product_name: Producto.
        price: Precio unitario de venta.
        currency: USD o la moneda local del negocio.
    """
    return await execute_command(ctx, "set_product_price", _args(product_name=product_name, price=price, currency=currency),
                                 lambda: SetProductPrice(product_name=product_name, price=to_decimal(price), currency=currency))


@tool("Fijar el costo unitario de un producto (para calcular ganancias)", kind="write")
async def set_product_cost(ctx: RunContext[TurnDeps], product_name: str, unit_cost: float, currency: Currency) -> dict[str, Any]:
    """Guarda cuánto le cuesta al dueño cada unidad de un producto.

    Args:
        product_name: Producto.
        unit_cost: Costo unitario.
        currency: USD o la moneda local del negocio.
    """
    return await execute_command(ctx, "set_product_cost", _args(product_name=product_name, unit_cost=unit_cost, currency=currency),
                                 lambda: SetProductCost(product_name=product_name, unit_cost=to_decimal(unit_cost), currency=currency))


@tool("Borrar un producto del catálogo (deja de aparecer; las ventas y compras ya registradas no se tocan)", kind="write")
async def delete_product(ctx: RunContext[TurnDeps], product_name: str) -> dict[str, Any]:
    """Saca un producto del catálogo. Úsala cuando el dueño pide borrar, quitar o eliminar un producto.

    El historial se conserva: las ventas y compras donde aparece siguen contando en las cifras. Solo deja de
    estar en el catálogo (precio, costo y sugerencias). Si el dueño solo quiere dejar de venderlo por ahora,
    igual se borra del catálogo y puede volver a crearlo cuando quiera.

    Args:
        product_name: Producto del catálogo.
    """
    return await execute_command(ctx, "delete_product", _args(product_name=product_name),
                                 lambda: DeleteProduct(product_name=product_name))


@tool("Fijar una tasa que dice el dueño: la que usa para cobrar, la tasa real de sus bolívares, o ambas", kind="write")
async def set_rate(ctx: RunContext[TurnDeps], rate: float, purpose: RateScope | None = None,
                   label: str | None = None) -> dict[str, Any]:
    """Guarda una tasa (moneda local por dólar) que da el dueño. Cambiarla nunca modifica los movimientos anteriores.

    - purpose=charge: la tasa con la que COBRA ("desde ahora cobro a 820", "uso 800 para cobrar"). El negocio pasa a
      cobrar con su tasa propia.
    - purpose=funds: la tasa REAL de sus bolívares, a la que los consiguió ("usa 800 como la tasa real de mis bolívares",
      "los bolívares me salieron a 800"). No cambia lo que cobra.
    - purpose=both: si dice explícitamente que es para las dos cosas.
    - Si solo dice "pon la tasa en 800" sin decir para qué, OMITE purpose: el sistema le pregunta con botones.

    Args:
        rate: Unidades de moneda local por dólar.
        purpose: charge, funds o both. Omítelo si el dueño no dijo para qué es.
        label: De dónde sale, si lo dijo (ej. "Binance").
    """
    return await execute_command(ctx, "set_rate", _args(rate=rate, purpose=purpose, label=label),
                                 lambda: SetRate(rate=to_decimal(rate), purpose=purpose, label=label))


@tool("Cambiar con qué tasa cobra el negocio: la oficial, la de mercado o su tasa propia", kind="write")
async def set_charge_rate_mode(ctx: RunContext[TurnDeps], mode: ChargeRateMode) -> dict[str, Any]:
    """Cambia la tasa para cobrar del negocio ("desde ahora cobro a BCV", "vuelve a la de mercado"). Para cobrar con una
    tasa propia NUEVA usa set_rate con purpose=charge. No modifica los movimientos anteriores.

    Args:
        mode: official (la oficial, p. ej. BCV), market (la de mercado) o custom (su tasa propia ya guardada).
    """
    return await execute_command(ctx, "set_charge_rate_mode", _args(mode=mode), lambda: SetChargeRateMode(mode=mode))


@tool("Registrar un cambio de moneda (dólares por bolívares o al revés): no es venta, gasto ni ingreso", kind="write")
async def record_exchange(
    ctx: RunContext[TurnDeps],
    out_amount: float,
    out_currency: Currency,
    in_amount: float,
    in_currency: Currency,
    out_method: PaymentMethod | None = None,
    in_method: PaymentMethod | None = None,
    business_date: date | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Registra un CAMBIO de moneda: salió dinero en una moneda y entró en la otra ("cambié 100$ y me dieron 80.000 Bs",
    "vendí $200 y recibí 160.000 Bs por Binance", "compré 50$ con 42.000 Bs"). NO es una venta del negocio aunque diga
    "vendí dólares", ni un gasto. Si recibió moneda local, su tasa pasa a ser la tasa real de sus bolívares.
    Si falta cuánto salió o cuánto entró, pregúntalo antes de registrar.

    Args:
        out_amount: Cuánto salió.
        out_currency: Moneda de lo que salió (USD o la local).
        in_amount: Cuánto entró.
        in_currency: Moneda de lo que entró.
        out_method: Por dónde salió (efectivo, zelle, binance…), si lo dijo.
        in_method: Por dónde entró (efectivo, pago móvil, binance…), si lo dijo. Si dijo uno solo para todo, repítelo.
        business_date: Fecha si NO fue hoy (YYYY-MM-DD).
        description: Nota corta, si aporta algo.
    """
    args = _args(out_amount=out_amount, out_currency=out_currency, in_amount=in_amount, in_currency=in_currency,
                 out_method=out_method, in_method=in_method, business_date=business_date, description=description)
    return await execute_command(ctx, "record_exchange", args, lambda: RecordExchange(
        out_amount=to_decimal(out_amount), out_currency=out_currency, in_amount=to_decimal(in_amount), in_currency=in_currency,
        out_method=out_method, in_method=in_method, business_date=business_date, description=description))


@tool("Cerrar la caja del día (resumen congelado de ventas, cobros, gastos, compras y fiado)", kind="write")
async def close_cash(ctx: RunContext[TurnDeps], business_date: date | None = None) -> dict[str, Any]:
    """Cierra la caja. Úsala SOLO cuando el dueño pide cerrar caja. Para ver cómo va el día sin cerrar usa get_cash_status.

    Args:
        business_date: Día a cerrar si no es hoy (YYYY-MM-DD).
    """
    return await execute_command(ctx, "close_cash", _args(business_date=business_date), lambda: CloseCash(business_date=business_date))


@tool("Recordar datos estables del negocio que el dueño cuenta (\"Moisés paga los viernes\", \"cerramos los domingos\")",
      kind="write")
async def remember_note(ctx: RunContext[TurnDeps], text: str, note_kind: NoteKind = NoteKind.GENERAL,
                        subject_name: str | None = None, replaces_previous: bool = False) -> dict[str, Any]:
    """Guarda un hecho estable del negocio en la memoria. NO la uses para ventas, gastos, deudas ni nada con montos
    (eso va en su tool). Úsala cuando el dueño cuenta algo que conviene recordar.

    Args:
        text: El hecho, en una frase clara en tercera persona.
        note_kind: customer, supplier, product, policy (reglas/horarios del negocio) o general.
        subject_name: Cliente, proveedor o producto al que se refiere, si aplica.
        replaces_previous: true si corrige/reemplaza lo que se sabía antes de ese mismo sujeto.
    """
    return await execute_command(ctx, "remember_note", _args(text=text, note_kind=note_kind, subject_name=subject_name,
                                                             replaces_previous=replaces_previous or None),
                                 lambda: RememberNote(text=text, note_kind=note_kind, subject_name=subject_name,
                                                      replaces_previous=replaces_previous))


@tool("Cambiar el apodo o completar el nombre de un cliente existente", kind="write", lanes=FAST_AND_AGENT,
      verticals=frozenset({BusinessVertical.RETAIL, BusinessVertical.BROKERAGE}))
async def edit_customer_profile(ctx: RunContext[TurnDeps], customer_name: str,
                                name: str | None = None, nickname: str | None = None) -> dict[str, Any]:
    """Edita un único cliente por nombre o apodo, con permisos y resolución de ambigüedad.
    Requiere un pedido explícito de editar el PERFIL del cliente en su conjunto.
    «Es Diego Example» al responder a una venta citada identifica al cliente de ESA venta, aunque el recibo
    anterior anuncie «Cliente creado»: NO autoriza cambiar este perfil ni todos sus movimientos.
    Usa edit_broker_movement con changes.customer_name para esa aclaración, conservando importes/tasas.
    No renombres este perfil al nombre de otro cliente existente.

    Args:
        customer_name: Referencia actual al cliente, nombre o apodo. Nunca un ID.
        name: Nombre completo NUEVO, solo cuando pide editar el perfil del cliente; no una identidad aclarada en una operación.
        nickname: Apodo NUEVO solicitado. No cambia el nombre ni crea clientes.
    """
    changes = {key: value for key, value in {"name": name, "nickname": nickname}.items() if value is not None}
    args = {"customer_name": customer_name, **changes}
    return await execute_command(ctx, "edit_customer_profile", args, lambda: EditCustomerProfile(**args))
