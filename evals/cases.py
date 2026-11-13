"""Casos de evaluación con conversaciones realistas del dominio.

Los casos operativos verifican tools y estado persistido. Los casos de conversación también verifican
la relevancia del texto por turno, sin exigir una redacción literal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.application.commands.brokerage import (
    CreateBrokerMember,
    RecordBrokerAccountPayment,
    RecordBrokerAccountWithdrawal,
    RecordBrokerOperation,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from app.application.commands.catalog import SetProductCost, SetProductPrice
from app.application.commands.customers import UpsertCustomer
from app.application.commands.expenses import RecordExpense
from app.application.commands.receivables import CreateReceivable
from app.application.commands.sales import RecordSale, SaleItemInput
from app.db.models import BrokerMovement, Business, Receivable, Sale
from app.domain.enums import (
    BusinessVertical,
    Currency,
    ExpenseCategory,
    MemberRole,
    PaymentMethod,
    PaymentStatus,
)
from app.domain.text import extract_numbers
from evals.conversation import (
    business_reply,
    operational_reply,
    repeated_greeting_reply,
    uncertain_receipt_reply,
)

Args = dict[str, Any]


@dataclass
class Case:
    id: str
    category: str
    messages: list[str]
    expect_tools: list[str] = field(default_factory=list)  # deben aparecer (en cualquier orden) con outcome ok/needs_confirmation
    forbid_tools: list[str] = field(default_factory=list)
    no_writes: bool = False  # ninguna tool de escritura debe ejecutarse con éxito
    arg_checks: dict[str, Callable[[Args], bool]] = field(default_factory=dict)
    seed: list[Any] = field(default_factory=list)  # comandos a ejecutar antes (actor panel)
    quote_last_reply_on_final: bool = False
    prior_assistant_reply: str | None = None  # seed a historical explanation (including a known incorrect one)
    onboarding: bool = False  # negocio recién creado, sin configurar ni saludo
    vertical: BusinessVertical = BusinessVertical.RETAIL
    member_role: MemberRole = MemberRole.OWNER
    member_name: str = "Eval"
    member_abbreviation: str | None = None
    expect_lane: str | None = None
    db_check: Callable[[Any, Any], Awaitable[bool]] | None = None
    notes: str = ""
    image_inputs: dict[int, str] = field(default_factory=dict)
    reply_check: Callable[[list[str]], bool] | None = None  # un texto compuesto por turno; solo para UX conversacional
    tools_turn: int = -1  # elección determinista final: comprobar las tools del turno que propuso la compra
    expect_attempted_tools: list[str] = field(default_factory=list)  # valid proposals rejected for missing required data
    forbid_attempted_tools: list[str] = field(default_factory=list)  # no mutation proposal, including rejected attempts


def money_arg(args: dict, key: str, expected: str) -> bool:
    try:
        return Decimal(str(args.get(key))) == Decimal(expected)
    except Exception:
        return False


def _item(product, qty, price, cur=Currency.USD):
    return SaleItemInput(product=product, quantity=Decimal(qty), unit_price=Decimal(price), currency=cur)


def business_ok(pred: Callable[[Business], bool]) -> Callable[[Any, Any], Awaitable[bool]]:
    async def check(container, business_id) -> bool:
        async with container.session_factory() as s:
            return bool(pred(await s.get(Business, business_id)))
    return check


def draft(b: Business) -> dict[str, Any]:
    return b.onboarding_draft or {}


def methods(values: list[str] | None) -> set[str]:
    return set(values or [])


async def _no_sales(container, business_id) -> bool:
    from sqlalchemy import func, select
    async with container.session_factory() as s:
        return (await s.execute(select(func.count()).select_from(Sale).where(Sale.business_id == business_id))).scalar_one() == 0


async def _pending_purchase_corrected_to_1000(container, business_id) -> bool:
    from sqlalchemy import select

    async with container.session_factory() as s:
        movements = (await s.execute(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id,
            BrokerMovement.deleted_at.is_(None),
        ))).scalars().all()
        receivables = (await s.execute(select(Receivable).where(
            Receivable.business_id == business_id,
            Receivable.deleted_at.is_(None),
        ))).scalars().all()
        return (
            len(movements) == 1
            and movements[0].operation_amount == Decimal("1000.00")
            and movements[0].commission == Decimal("10.00")
            and movements[0].balance_delta == Decimal("-990.00")
            and len(receivables) == 1
            and receivables[0].amount == Decimal("1000.00")
        )


_TO_SUMMARY = ["hola", "Scalea", "desarrollo de software", "sí", "2", "zelle"]


def items_ok(pred: Callable[[list[Args]], bool]) -> Callable[[Args], bool]:
    return lambda a: pred(a.get("items") or [])


CASES: list[Case] = [
    # ------------------------------------------------------------------ onboarding (formulario interpretado por LLM)
    Case("onb_happy_path", "onboarding", ["hola", "Repuestos El Rayo", "repuestos de moto", "sí", "1",
                                          "efectivo en dólares y pago móvil", "sí"],
         onboarding=True, db_check=business_ok(lambda b: b.onboarding_state == "done" and b.name == "Repuestos El Rayo"
                                                and b.charge_rate_mode == "official" and b.display_currency == "usd_applied"
                                                and b.declared_country == "Venezuela"
                                                and {"cash", "mobile_payment"} <= methods(b.payment_methods))),
    Case("onb_greeting_twice_then_name_correction", "onboarding", ["hola", "hola", "no no, mi negocio se llama scalea"], onboarding=True,
         db_check=business_ok(lambda b: (draft(b).get("name") or "").lower() == "scalea" and not draft(b).get("what_sells")),
         notes="conversación real del 15-09-2026: el segundo 'hola' se guardaba como nombre"),
    Case("onb_correction_while_asked_other_field", "onboarding", ["hola", "Mi Tienda", "no no, mi negocio se llama scalea"], onboarding=True,
         db_check=business_ok(lambda b: (draft(b).get("name") or "").lower() == "scalea" and not draft(b).get("what_sells"))),
    Case("onb_everything_in_one_message", "onboarding",
         ["hola, tengo una tienda llamada Bodegón Luisa, vendo víveres, cobro a tasa BCV y me pagan por pago móvil y punto"],
         onboarding=True, db_check=business_ok(lambda b: "luisa" in (draft(b).get("name") or "").lower() and draft(b).get("what_sells")
                                                and draft(b).get("charge_rate_mode") == "official"
                                                and methods(draft(b).get("payment_methods")) == {"mobile_payment", "card"})),
    Case("onb_charge_rate_by_number", "onboarding", ["hola", "Scalea", "software", "sí", "3"], onboarding=True,
         db_check=business_ok(lambda b: draft(b).get("charge_rate_mode") == "custom" and not draft(b).get("custom_charge_rate")),
         notes="3 = uso mi propia tasa: queda pendiente cuál"),
    Case("onb_own_rate_in_words", "onboarding", ["hola", "Scalea", "software", "sí", "uso la mía, hoy estoy cobrando a 820"],
         onboarding=True, db_check=business_ok(lambda b: draft(b).get("charge_rate_mode") == "custom"
                                                and draft(b).get("custom_charge_rate") in ("820", "820.0"))),
    Case("onb_change_at_summary", "onboarding", [*_TO_SUMMARY, "no, mejor cobro a tasa BCV"], onboarding=True,
         db_check=business_ok(lambda b: b.onboarding_state == "in_progress" and draft(b).get("charge_rate_mode") == "official"
                              and draft(b).get("name") == "Scalea")),
    Case("onb_add_payment_method_at_summary", "onboarding", [*_TO_SUMMARY, "agrega también pago móvil"], onboarding=True,
         db_check=business_ok(lambda b: methods(draft(b).get("payment_methods")) == {"zelle", "mobile_payment"})),
    Case("onb_free_confirmation", "onboarding", [*_TO_SUMMARY, "así está perfecto, guárdalo"], onboarding=True,
         db_check=business_ok(lambda b: b.onboarding_state == "done" and b.name == "Scalea" and b.charge_rate_mode == "market")),
    Case("onb_plain_no_at_summary_changes_nothing", "onboarding", [*_TO_SUMMARY, "no"], onboarding=True,
         db_check=business_ok(lambda b: b.onboarding_state == "in_progress" and draft(b).get("charge_rate_mode") == "market"
                              and methods(draft(b).get("payment_methods")) == {"zelle"})),
    Case("onb_country_elsewhere_in_one_message", "onboarding", ["hola", "Scalea", "software", "no, estoy en Colombia"],
         onboarding=True, db_check=business_ok(lambda b: draft(b).get("other_country") == "Colombia"
                                                and draft(b).get("charge_rate_mode") == "official"),
         notes="país no soportado: se guarda lo que dijo y registra en dólares"),
    Case("onb_country_no_then_venezuela", "onboarding", ["hola", "Scalea", "software", "no", "ah no, sí estoy en Venezuela"],
         onboarding=True, db_check=business_ok(lambda b: draft(b).get("country_code") == "VE" and not draft(b).get("other_country"))),
    Case("onb_question_is_not_an_answer", "onboarding", ["hola", "¿y para qué necesitas saber eso?"], onboarding=True,
         db_check=business_ok(lambda b: not draft(b).get("name"))),
    Case("onb_sale_attempt_is_deferred", "onboarding", ["hola", "vendí 2 bujías a 3$ en efectivo"], onboarding=True,
         db_check=_no_sales, notes="no registra ni toma la venta como nombre; basta con que no guarde la venta"),
    # ------------------------------------------------------------------ ventas
    Case("sale_simple_usd", "ventas", ["vendí 2 bujías NGK a 3$ cada una en efectivo"], ["record_sale"],
         arg_checks={"record_sale": lambda a: a["items"][0]["quantity"] == 2 and a["items"][0]["unit_price"] == 3
                     and a.get("payment_method") == "cash"}),
    Case("sale_total_divides_unit_price", "ventas", ["vendí 4 filtros de aceite en 20 dólares"], ["record_sale"],
         arg_checks={"record_sale": items_ok(lambda it: it[0]["quantity"] == 4 and abs(it[0]["unit_price"] - 5) < 0.01)}),
    Case("sale_lucas_ves", "ventas", ["vendí un casco por 45 lucas por pago móvil"], ["record_sale"],
         arg_checks={"record_sale": items_ok(lambda it: it[0].get("currency") in ("VES", None) and it[0]["unit_price"] in (45000, 45))},
         notes="45 lucas = 45.000 Bs"),
    Case("sale_multi_items", "ventas", ["vendí 2 cauchos a 30$ y un tripa a 6$, todo en efectivo"], ["record_sale"],
         arg_checks={"record_sale": items_ok(lambda it: len(it) == 2)}),
    Case("sale_catalog_price", "ventas", ["vendí 3 cadenas 428"], ["record_sale"],
         seed=[SetProductPrice(product_name="Cadena 428", price=Decimal(14), currency=Currency.USD)],
         arg_checks={"record_sale": items_ok(lambda it: it[0].get("unit_price") in (None, 14))}),
    Case("sale_yesterday", "ventas", ["ayer vendí un kit de arrastre en 35$"], ["record_sale"],
         arg_checks={"record_sale": lambda a: bool(a.get("business_date"))}),
    Case("sale_missing_price_asks", "ventas", ["vendí un espejo retrovisor"], no_writes=True,
         notes="sin precio ni catálogo: debe preguntar"),
    Case("sale_usd_price_paid_bs", "precio_pago", ["vendí un espejo en $10 y me pagó 8.000 Bs por pago móvil"], ["record_sale"],
         arg_checks={"record_sale": lambda a: a["items"][0]["unit_price"] == 10 and a["items"][0].get("currency") in ("USD", None)
                     and a.get("amount_paid") == 8000 and a.get("paid_currency") in ("VES", None)
                     and a.get("payment_method") == "mobile_payment"},
         notes="precio en $ y pago en Bs son datos distintos"),
    Case("sale_price_in_bs_is_price", "precio_pago", ["vendí el espejo en 2.000 Bs"], ["record_sale"],
         seed=[SetProductPrice(product_name="espejo", price=Decimal(10), currency=Currency.USD)],
         arg_checks={"record_sale": lambda a: a["items"][0].get("unit_price") == 2000
                     and a["items"][0].get("currency") in ("VES", None) and not a.get("amount_paid")},
         notes="'lo vendí en' es precio: el sistema pregunta si fue más barato o descuento (sin moneda, la tool toma 'Bs' del texto)"),
    Case("sale_paid_less_is_payment", "precio_pago", ["vendí un espejo de $10 y el cliente me pagó 2.000 Bs"], ["record_sale"],
         arg_checks={"record_sale": lambda a: a["items"][0]["unit_price"] == 10 and a.get("amount_paid") == 2000
                     and a.get("paid_currency") in ("VES", None)},
         notes="'me pagó' es dinero recibido: el sistema pregunta si quedó debiendo o fue descuento"),
    Case("sale_price_from_payment", "precio_pago",
         ["vendí un aceite 20W50 por pago móvil, me llegó el pago de 8.253,66 Bs"], ["record_sale"],
         arg_checks={"record_sale": lambda a: not a["items"][0].get("unit_price") and a.get("amount_paid") == 8253.66
                     and a.get("payment_method") == "mobile_payment"},
         notes="como con una captura de pago: sin precio dicho, el precio sale del pago (Bs ÷ tasa para cobrar); no se pregunta"),
    Case("sale_mentioned_rate_is_not_applied", "precio_pago", ["a este cliente le vendí un caucho de $10 y se lo calculé a 800"],
         ["record_sale"], forbid_tools=["set_rate"],
         arg_checks={"record_sale": lambda a: a.get("mentioned_rate") == 800}),
    Case("sale_accepted_as_full_payment", "precio_pago", ["vendí un espejo de $10 y acepté 3.000 Bs como pago completo"],
         ["record_sale"],
         arg_checks={"record_sale": lambda a: a.get("amount_paid") == 3000 and a.get("payment_resolution") == "discount"}),
    Case("fx_loss_query", "bi", ["¿cuánto he perdido por diferencia cambiaria este mes?"], ["query_metrics"], no_writes=True,
         arg_checks={"query_metrics": lambda a: "fx_loss_usd" in a["metrics"]}),
    Case("sale_credit", "fiado", ["Moisés se llevó un caucho de 40$ fiado"], ["record_sale"],
         arg_checks={"record_sale": lambda a: a.get("payment_status") == "credit" and "mois" in (a.get("customer_name") or "").lower()}),
    Case("sale_partial", "fiado", ["Pedro compró una batería de 60$, me dio 20 y el resto me lo debe"], ["record_sale"],
         arg_checks={"record_sale": lambda a: a.get("payment_status") == "partial" and a.get("amount_paid") == 20}),
    # ------------------------------------------------------------------ gastos vs compras
    Case("expense_utilities", "gastos", ["pagué 25$ de luz"], ["record_expense"],
         arg_checks={"record_expense": lambda a: a.get("category") == "utilities"}),
    Case("expense_payroll", "gastos", ["le pagué 50$ al ayudante por la semana"], ["record_expense"],
         arg_checks={"record_expense": lambda a: a.get("category") == "payroll"}),
    Case("expense_food_bs", "gastos", ["almuerzo 1800 bs"], ["record_expense"],
         arg_checks={"record_expense": lambda a: a.get("currency") in ("VES", None) and a["amount"] == 1800}),
    Case("purchase_items", "compras", ["compré 10 cadenas 428 a 8$ cada una a Repuestos El Gocho"], ["record_purchase"],
         arg_checks={"record_purchase": lambda a: bool(a.get("items")) and a["items"][0]["unit_cost"] == 8}),
    Case("purchase_total_only", "compras", ["compré mercancía por 300$ al proveedor de Valencia"], ["record_purchase"],
         forbid_tools=["record_expense"]),
    Case("purchase_not_expense", "compras", ["me llegó el pedido de aceites, pagué 120$"], ["record_purchase"],
         forbid_tools=["record_expense"]),
    Case("exchange_without_amount_asks", "trampas", ["cambié 100$ a bolívares en el banco"], no_writes=True,
         forbid_tools=["record_expense", "record_sale"],
         notes="es un cambio, no un gasto (lección de Personal); falta cuántos Bs entraron: pregunta"),
    Case("cash_withdrawal_not_expense", "trampas", ["saqué 50$ del punto para tener sencillo"], no_writes=True),
    # ------------------------------------------------------------------ fiado / abonos
    Case("receivable_simple", "fiado", ["anota que Luis me debe 15$"], ["create_receivable"],
         seed=[CreateReceivable(customer_name="Luis", amount=Decimal(1), currency=Currency.USD)]),
    Case("payment_simple", "fiado", ["Moisés me abonó 10$ en efectivo"], ["record_payment"],
         seed=[CreateReceivable(customer_name="Moisés", amount=Decimal(30), currency=Currency.USD)],
         arg_checks={"record_payment": lambda a: a["amount"] == 10}),
    Case("balance_query", "consultas", ["¿cuánto me debe Moisés?"], ["get_customer_balance"], no_writes=True,
         seed=[CreateReceivable(customer_name="Moisés", amount=Decimal(30), currency=Currency.USD)]),
    Case("who_owes", "consultas", ["¿quiénes me deben?"], ["list_receivables"], no_writes=True,
         seed=[CreateReceivable(customer_name="Ana", amount=Decimal(5), currency=Currency.USD)]),
    # ------------------------------------------------------------------ consultas / BI
    Case("sales_today", "bi", ["¿cuánto he vendido hoy?"], ["query_metrics"], no_writes=True,
         seed=[RecordSale(items=[_item("bujía", 2, 3)])]),
    Case("profit_month", "bi", ["¿cuánto he ganado este mes?"], ["query_metrics"], no_writes=True,
         seed=[SetProductCost(product_name="Bujía", unit_cost=Decimal("1.5"), currency=Currency.USD),
               RecordSale(items=[_item("bujía", 2, 3)])],
         arg_checks={"query_metrics": lambda a: any(m in a.get("metrics", []) for m in ("gross_margin", "margin_pct", "net_cash_flow"))}),
    Case("top_products", "bi", ["¿qué es lo que más vendo?"], ["query_metrics"], no_writes=True,
         seed=[RecordSale(items=[_item("bujía", 5, 3), _item("cadena", 1, 14)])],
         arg_checks={"query_metrics": lambda a: "product" in (a.get("group_by") or [])}),
    Case("compare_weeks", "bi", ["¿vendí más esta semana que la pasada?"], ["query_metrics"], no_writes=True,
         seed=[RecordSale(items=[_item("bujía", 2, 3)])]),
    Case("expenses_by_category", "bi", ["¿en qué se me va la plata en gastos este mes?"], ["query_metrics"], no_writes=True,
         seed=[RecordExpense(description="luz", amount=Decimal(20), currency=Currency.USD, category=ExpenseCategory.UTILITIES)],
         arg_checks={"query_metrics": lambda a: "expense_category" in (a.get("group_by") or []) or "expenses_total" in a.get("metrics", [])}),
    Case("weekday_sales", "bi", ["¿qué día de la semana vendo más?"], ["query_metrics"], no_writes=True,
         seed=[RecordSale(items=[_item("bujía", 2, 3)])],
         arg_checks={"query_metrics": lambda a: "weekday" in (a.get("group_by") or [])}),
    Case("cash_status_not_close", "caja", ["¿cómo va la caja hoy?"], no_writes=True, forbid_tools=["close_cash"]),
    Case("close_cash", "caja", ["cierra caja"], ["close_cash"]),
    # ------------------------------------------------------------------ correcciones
    Case("correction_quoted_quantity", "correcciones", ["vendí 2 bujías NGK a 3$ en efectivo", "eran 3, no 2"], ["edit_record"],
         quote_last_reply_on_final=True, arg_checks={"edit_record": lambda a: a["changes"].get("quantity") == 3}),
    Case("correction_unquoted_customer", "correcciones", ["no, lo del caucho no era Moisés sino Pedro"], ["edit_record"],
         seed=[RecordSale(items=[_item("caucho", 1, 40)], customer_name="Moisés", payment_status=PaymentStatus.CREDIT)],
         arg_checks={"edit_record": lambda a: "pedro" in (a["changes"].get("customer_name") or "").lower()}),
    Case("delete_asks_confirmation", "correcciones", ["borra el gasto de la luz"], ["delete_record"],
         seed=[RecordExpense(description="luz", amount=Decimal(20), currency=Currency.USD, category=ExpenseCategory.UTILITIES)]),
    Case("delete_product_from_catalog", "correcciones", ["elimina las bujías del catálogo"], ["delete_product"],
         seed=[SetProductPrice(product_name="Bujías", price=Decimal(3), currency=Currency.USD)],
         arg_checks={"delete_product": lambda a: "buj" in a["product_name"].lower()}),
    Case("delete_sale_is_not_delete_product", "correcciones", ["borra la venta de las bujías"], ["delete_record"],
         forbid_tools=["delete_product"], seed=[RecordSale(items=[_item("bujía", 2, 3)])],
         notes="borrar un movimiento no es sacar el producto del catálogo"),
    # ------------------------------------------------------------------ tasas y memoria
    Case("set_rate_bare_asks_purpose", "tasas", ["pon la tasa en 950, es la de binance"], ["set_rate"],
         arg_checks={"set_rate": lambda a: a["rate"] == 950 and a.get("purpose") is None},
         notes="no dice para qué es: el sistema pregunta con botones (cobrar / tasa real / ambas)"),
    Case("set_charge_rate", "tasas", ["desde ahora cobro a 820"], ["set_rate"],
         arg_checks={"set_rate": lambda a: a["rate"] == 820 and a.get("purpose") == "charge"}),
    Case("set_funds_rate", "tasas", ["usa 800 como la tasa real de mis bolívares"], ["set_rate"],
         arg_checks={"set_rate": lambda a: a["rate"] == 800 and a.get("purpose") == "funds"}),
    Case("set_charge_mode_official", "tasas", ["desde hoy cobro a tasa BCV"], ["set_charge_rate_mode"],
         arg_checks={"set_charge_rate_mode": lambda a: a["mode"] == "official"}, forbid_tools=["set_rate"]),
    Case("exchange_binance", "cambios", ["Vendí $200 y recibí 160.000 Bs por Binance"], ["record_exchange"],
         forbid_tools=["record_sale", "record_expense"],
         arg_checks={"record_exchange": lambda a: a["out_amount"] == 200 and a["out_currency"] == "USD"
                     and a["in_amount"] == 160000 and a["in_currency"] == "VES"}),
    Case("exchange_bought_dollars", "cambios", ["compré 50$ en efectivo con 42.000 Bs de pago móvil"], ["record_exchange"],
         forbid_tools=["record_purchase", "record_expense"],
         arg_checks={"record_exchange": lambda a: a["out_currency"] == "VES" and a["out_amount"] == 42000
                     and a["in_currency"] == "USD" and a["in_amount"] == 50}),
    Case("rate_query", "tasas", ["¿a cómo está el dólar?"], no_writes=True,
         notes="las tasas ya están en el contexto: puede responder sin tool, pero con cifras de origen"),
    Case("remember_note", "memoria", ["Moisés siempre paga los viernes"], ["remember_note"]),
    Case("recall_note", "memoria", ["recuerda que cerramos los domingos", "¿qué días cerramos?"], [], notes="debe responder desde notas"),
    # ------------------------------------------------------------------ límites
    Case("inventory_not_promised", "limites", ["¿cuántas cadenas me quedan en el local?"], no_writes=True,
         notes="no manejamos inventario: no debe inventar"),
    Case("off_topic", "limites", ["¿quién ganó el juego de los Leones anoche?"], no_writes=True),
    # ------------------------------------------------------------------ vertical de corredores
    Case("broker_physical_sale", "corredores", ["Vendí 2 bujías a 5 dólares cada una en efectivo"],
         ["reject_physical_sale"], forbid_tools=["record_broker_operation", "record_broker_account_payment"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="agent"),
    Case("broker_operation_legacy_requires_clarification", "corredores", ["Vendí 1000 y entregué 990"],
         ["need_more_info"], forbid_tools=["record_broker_operation"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER),
    Case("broker_settled_sale", "corredores", ['Vendí 200 a tasa 800'], ["record_broker_operation"],
         arg_checks={"record_broker_operation": lambda a: a.get("operation_type") == "sell"
                     and money_arg(a, "operation_amount", "200") and a.get("delivered_amount") is None},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="fast"),
    Case("broker_settled_sale_null_regression", "corredores", ['vendi 1000 a tasa 800'], ["record_broker_operation"],
         arg_checks={"record_broker_operation": lambda a: a.get("operation_type") == "sell"
                     and money_arg(a, "operation_amount", "1000") and a.get("delivered_amount") is None
                     and a.get("broker_name") is None},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="fast",
         notes="regresión: un corredor propio no debe recibir broker_name como la cadena 'null'"),
    Case("broker_settled_buy", "corredores", ['Compré 450 a Lissy de contado a tasa 800', 'Liquidada'], ["record_broker_purchase"], tools_turn=0,
         seed=[UpsertCustomer(name="Lissy", customer_classification="personal")],
         arg_checks={"record_broker_purchase": lambda a: a.get("settlement") in (None, "settled")
                     and money_arg(a, "amount", "450") and a.get("customer_name") == "Lissy"
                     and a.get("description") is None},
         db_check=lambda c, b: _v2_money(c, b, bs="360000", rate="800", delta="4.5"),
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER),
    Case("broker_pending_buy", "corredores",
         ['Entregamos los fondos a Orlando y quedó debiendo 197.77 dólares, tasa 800', 'Pendiente'],
         ["record_broker_purchase"], tools_turn=0,
         arg_checks={"record_broker_purchase": lambda a: a.get("customer_name") == "Orlando"
                     and money_arg(a, "amount", "197.77")},
         seed=[UpsertCustomer(name="Orlando", customer_classification="personal")],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER),
    Case("broker_pending_buy_shortcut", "corredores", ['Orlando 100 a tasa 800', 'Pendiente'],
         ["record_broker_purchase"], tools_turn=0,
         arg_checks={"record_broker_purchase": lambda a: a.get("customer_name") == "Orlando"
                     and money_arg(a, "amount", "100")},
         seed=[UpsertCustomer(name="Orlando", customer_classification="personal")],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER),
    Case("broker_pending_correction", "corredores", ['Pedro 1200 a tasa 800', 'Pendiente', 'Personal', 'aquí eran 1000'],
         ["edit_broker_movement"], forbid_tools=["delete_broker_movement"],
         quote_last_reply_on_final=True,
         arg_checks={"edit_broker_movement": lambda a: money_arg(a.get("changes", {}), "amount", "1000")},
         db_check=_pending_purchase_corrected_to_1000,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="agent",
         notes="una aclaración de monto corrige movimiento y deuda; no borra ni crea otro"),
    Case("broker_customer_payment", "corredores", ["Carlos pagó 50"],
         ["record_broker_receivable_payment"],
         arg_checks={"record_broker_receivable_payment": lambda a: a.get("customer_name") == "Carlos"
                     and money_arg(a, "amount", "50")},
         seed=[UpsertCustomer(name="Carlos", customer_classification="personal"), RecordPendingBrokerPurchase(exchange_rate="800",
             customer_classification="personal", customer_name="Carlos", amount=Decimal("100"),
         )],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         notes="el pago reduce la deuda del cliente y acredita el saldo del corredor sin preguntar destino"),
    Case("broker_owner_pending_shortcut_uses_house", "corredores", ['Orlando 100 a tasa 800', 'Pendiente'],
         ["record_broker_purchase"], tools_turn=0,
         arg_checks={"record_broker_purchase": lambda a: a.get("customer_name") == "Orlando"
                     and money_arg(a, "amount", "100") and a.get("broker_name") is None},
         seed=[UpsertCustomer(name="Orlando", customer_classification="global")],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.OWNER),
    Case("broker_account_purchase_abbreviation", "corredores", ['SB 40 a tasa 800'],
         ["record_broker_account_purchase"],
         arg_checks={"record_broker_account_purchase": lambda a: money_arg(a, "amount", "40")
                     and a.get("description") is None},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         member_name="Alex Example", member_abbreviation="SB", expect_lane="fast"),
    Case("broker_account_purchase_bare_amount", "corredores", ["40"],
         no_writes=True, reply_check=lambda replies: "Dime la cantidad de bolívares" in replies[-1],
         arg_checks={"record_broker_account_purchase": lambda a: money_arg(a, "amount", "40")},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         member_name="Alex Example", member_abbreviation="SB", expect_lane="fast"),
    Case("broker_account_purchase_description", "corredores", ['SB 45.66 Farmatodo, tasa 800'],
         ["record_broker_account_purchase"],
         arg_checks={"record_broker_account_purchase": lambda a: money_arg(a, "amount", "45.66")
                     and a.get("description") == "Farmatodo"},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         member_name="Alex Example", member_abbreviation="SB", expect_lane="fast"),
    Case("broker_owner_records_account_purchase", "corredores", ['Alex sacó 100 a tasa 800'],
         ["record_broker_account_purchase"],
         arg_checks={"record_broker_account_purchase": lambda a: money_arg(a, "amount", "100")
                     and "samanta" in (a.get("broker_name") or "").lower()},
         seed=[CreateBrokerMember(name="Alex Example", phone="584140000099")],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.OWNER),
    Case("broker_account_repayment", "corredores", ["Pagué 1000 al negocio"], ["record_broker_account_payment"],
         arg_checks={"record_broker_account_payment": lambda a: money_arg(a, "amount", "1000") and "direction" not in a},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="fast"),
    Case("broker_owner_bare_amount_needs_context", "corredores", ["40"],
         ["need_more_info"], forbid_tools=["record_broker_account_purchase"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.OWNER, expect_lane="agent"),
    Case("broker_direct_return", "corredores", ["Entregué 200 al negocio"], ["record_broker_account_payment"],
         arg_checks={"record_broker_account_payment": lambda a: money_arg(a, "amount", "200") and "direction" not in a},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="fast"),
    Case("broker_future_date", "corredores", ["Pagué 12 el 3 de octubre de 2099"],
         ["need_more_info"], forbid_tools=["record_broker_account_payment", "record_broker_account_purchase"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER),
    Case("broker_ambiguous_received", "corredores", ["Recibí 50 por una venta"], ["need_more_info"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="agent"),
    Case("broker_ambiguous_out", "corredores", ["Salieron 100"], ["need_more_info"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, expect_lane="agent"),
]


_CUSTOMER_HISTORY_SCOPE_SEED = [
    CreateBrokerMember(name="Alex", phone="584140000099"),
    UpsertCustomer(name="CAP", nickname=None, customer_classification="global"),
    UpsertCustomer(name="Carolina Example", nickname="CAP", customer_classification="global"),
    RecordPendingBrokerPurchase(amount="600", exchange_rate="950", customer_name="Carolina Example", broker_name="Alex"),
    RecordBrokerReceivablePayment(amount="600", customer_name="Carolina Example", broker_name="Alex"),
    RecordBrokerOperation(operation_type="buy", operation_amount="160", exchange_rate="950", customer_name="Carolina Example"),
    RecordPendingBrokerPurchase(amount="240", exchange_rate="950", customer_name="Carolina Example"),
    RecordBrokerReceivablePayment(amount="240", customer_name="Carolina Example"),
]

for _history_label, _history_request, _history_explicit, _history_amounts in (
    ("all", "Qué operaciones se han registrado hoy de CAP?", False, (600, 240, 160)),
    ("house", "Operaciones de CAP hoy solo de Casa", True, (240, 160)),
):
    CASES.append(Case(
        f"customer_history_scope_{_history_label}", "customer_history_scope", [_history_request],
        expect_tools=["find_broker_movements"], no_writes=True, vertical=BusinessVertical.BROKERAGE,
        seed=_CUSTOMER_HISTORY_SCOPE_SEED,
        prior_assistant_reply="Antes estabas registrando operaciones en la cuenta Casa.",
        arg_checks={"find_broker_movements": lambda args, explicit=_history_explicit:
                    (args.get("customer_name") or "").casefold() in {"cap", "carolina parra"}
                    and args.get("account_filter_explicitly_requested", False) is explicit
                    and (not explicit or args.get("account_scope") == "house")},
        reply_check=lambda replies, amounts=_history_amounts:
                    all(any(abs(number - amount) < .01 for number in extract_numbers(replies[-1])) for amount in amounts),
        notes="CAP named customer collides with Carolina's nickname; prior Casa context must not restrict shared history.",
    ))

for _vertical in BusinessVertical:
    for _name, _messages in (
        ("greeting", ["Hola"]),
        ("help", ["Ayúdame"]),
        ("question", ["Tengo una pregunta"]),
        ("vague", ["Necesito hacer algo"]),
        ("capabilities", ["¿Qué puedes hacer?"]),
    ):
        CASES.append(Case(
            f"conversation_{_vertical.value}_{_name}", "conversacion", _messages,
            no_writes=True, vertical=_vertical, expect_lane="agent",
            reply_check=lambda replies, vertical=_vertical: all(business_reply(r, vertical) for r in replies),
        ))
    CASES.extend([
        Case(
            f"conversation_{_vertical.value}_repeated_greeting", "conversacion", ["Hola", "Hola otra vez"],
            no_writes=True, vertical=_vertical, expect_lane="agent",
            reply_check=lambda replies, vertical=_vertical: (
                business_reply(replies[0], vertical) and repeated_greeting_reply(replies[1])
            ),
        ),
        Case(
            f"conversation_{_vertical.value}_unrelated", "conversacion", ["¿Cuál es la capital de Japón?"],
            no_writes=True, vertical=_vertical, expect_lane="agent",
            reply_check=lambda replies, vertical=_vertical: (
                business_reply(replies[-1], vertical)
                and not any(word in replies[-1].casefold() for word in ("tokio", "tokyo"))
            ),
        ),
    ])

CASES.extend([
    Case("conversation_retail_sale_with_greeting", "conversacion", ["Hola, vendí un casco en 100$ en efectivo"],
         ["record_sale"], reply_check=lambda replies: (
             "✅ *Venta registrada*" in replies[-1] and operational_reply(replies[-1])
         )),
    Case("conversation_retail_missing_price", "conversacion", ["Vendí un espejo"],
         ["need_more_info"], no_writes=True,
         reply_check=lambda replies: operational_reply(replies[-1]) and "?" in replies[-1] and len(replies[-1]) < 200),
    Case("conversation_retail_business_question", "conversacion", ["Tengo una pregunta: ¿cuánto vendí hoy?"],
         ["query_metrics"], no_writes=True, reply_check=lambda replies: operational_reply(replies[-1])),
    Case("conversation_brokerage_sale_with_greeting", "conversacion", ['Hola, vendí 100 a tasa 800'],
         ["record_broker_operation"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         reply_check=lambda replies: "✅ *Venta de divisas registrada*" in replies[-1] and operational_reply(replies[-1])),
    Case("conversation_brokerage_missing_amount", "conversacion", ["Pagué al negocio"],
         ["need_more_info"], no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         reply_check=lambda replies: operational_reply(replies[-1]) and "?" in replies[-1] and len(replies[-1]) < 200),
    Case("conversation_brokerage_broker_capabilities", "conversacion", ["¿Qué puedes hacer?"],
         no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         reply_check=lambda replies: business_reply(replies[-1], BusinessVertical.BROKERAGE)
         and "todos los corredores" not in replies[-1].casefold()),
    Case("conversation_onboarding_scope", "conversacion", ["Hola", "¿Cuál es la capital de Japón?"],
         onboarding=True, no_writes=True,
         reply_check=lambda replies: "Soy *Bolos*" in replies[0] and "negocio" in replies[-1]
         and not any(word in replies[-1].casefold() for word in ("tokio", "tokyo"))),
])

_ = PaymentMethod


# Alcance y presentación: variaciones naturales se evalúan con LLM real, nunca se enrutan por texto.
_QUERY_SEED = [
    CreateBrokerMember(name="Enrique", phone="584140000099"),
    RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="global", customer_name="Cliente Casa", amount=Decimal("800")),
    RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal", customer_name="Cliente corredor", amount=Decimal("100"), broker_name="Enrique",
                                   force_new_customer=True),
]


def _descriptive_query(replies: list[str]) -> bool:
    text = replies[-1].casefold()
    return not any(p in text for p in (
        "va bien", "rendimiento positivo", "deberías", "deberias", "mejorar las ventas", "ganancia", "utilidad",
    ))


for _index, (_question, _scope, _summary) in enumerate([
    ("¿Cómo va el negocio?", "all", True),
    ("¿Cómo vamos hoy?", "all", True),
    ("Dame una vista de lo que llevamos hoy entre todos", "all", True),
    ("¿Cómo va Casa?", "house", True),
    ("¿Cómo va mi cuenta?", "house", True),
    ("Muéstrame lo mío hoy", "house", True),
    ("¿Cómo va Enrique?", "broker", True),
    ("¿Cuánto compramos?", "all", False),
    ("¿Cuánto compré?", "house", False),
    ("¿Cuánto nos deben?", "all", False),
]):
    CASES.append(Case(
        f"query_scope_owner_{_index}", "consultas", [_question],
        ["get_broker_balances"] if _index != 9 else [],
        no_writes=True, vertical=BusinessVertical.BROKERAGE, member_name="Enrique", seed=_QUERY_SEED,
        arg_checks={"get_broker_balances": lambda a, scope=_scope, summary=_summary: (
            (a.get("broker_name") == "Enrique" if scope == "broker" else
             not a.get("broker_name") and a.get("account_scope", "default") in
             ({"all", "default"} if scope == "all" else {"house"}))
            and a.get("presentation", "data") == ("summary" if summary else "data")
        )},
        reply_check=_descriptive_query,
    ))
    if _index == 9:
        CASES[-1].arg_checks["find_broker_receivables"] = lambda a: (
            not a.get("broker_name") and a.get("account_scope", "default") in {"all", "default"}
        )
        CASES[-1].reply_check = lambda replies: "900" in replies[-1] and _descriptive_query(replies)

for _role in (MemberRole.OWNER, MemberRole.BROKER):
    for _index, _question in enumerate(("¿Quién soy?", "¿Qué rol tengo?", "¿Con qué nombre y permiso entré?")):
        CASES.append(Case(
            f"query_identity_{_role.value}_{_index}", "consultas", [_question],
            no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=_role, member_name="Enrique",
            seed=_QUERY_SEED if _role == MemberRole.OWNER else [],
            forbid_tools=["get_broker_balances", "find_broker_movements", "find_broker_receivables"],
            reply_check=lambda replies, role=_role: (
                any(word in replies[-1].casefold() for word in
                    (("dueño", "propietario", "owner") if role == MemberRole.OWNER else ("corredor", "broker")))
                and "compra" not in replies[-1].casefold() and "balance" not in replies[-1].casefold()
            ),
        ))

for _index, _question in enumerate(("¿Cómo voy?", "¿Cuánto debo?", "¿Cómo va mi cuenta?")):
    CASES.append(Case(
        f"query_scope_broker_{_index}", "consultas", [_question],
        ["get_broker_balances"] if _index != 1 else [],
        no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        member_name="Enrique",
        seed=[RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal", customer_name="Cliente propio", amount=Decimal("100"))],
        arg_checks={"get_broker_balances": lambda a: not a.get("broker_name")
                    and a.get("account_scope", "default") != "house"},
        reply_check=(lambda replies: "99" in replies[-1] and "Casa" not in replies[-1]
                     and _descriptive_query(replies)) if _index == 1 else _descriptive_query,
    ))

CASES.append(Case(
    "query_multiple_reads", "consultas",
    ["Dame el resumen consolidado, enumera los movimientos y dime qué clientes deben y en qué cuenta."],
    ["get_broker_balances", "find_broker_movements", "find_broker_receivables"],
    no_writes=True, vertical=BusinessVertical.BROKERAGE, seed=_QUERY_SEED,
    reply_check=lambda replies: (
        "Cliente Casa" in replies[-1] and "Cliente corredor" in replies[-1]
        and "800" in replies[-1] and "100" in replies[-1]
        and replies[-1].count("Resumen del negocio") <= 1 and _descriptive_query(replies)
        and not any("casa" in line.casefold() and "comisi" in line.casefold()
                    for line in replies[-1].splitlines())
    ),
))


for _index, _question in enumerate(("¿Quién soy?", "¿Qué rol tengo?")):
    CASES.append(Case(
        f"query_identity_retail_{_index}", "consultas", [_question], no_writes=True,
        member_name="Enrique", vertical=BusinessVertical.RETAIL,
        forbid_tools=["query_metrics", "get_sales_summary", "get_cash_status", "find_records",
                      "list_receivables", "get_customer_balance", "get_rates"],
        reply_check=lambda replies: (
            any(w in replies[-1].casefold() for w in ("dueño", "propietario", "owner"))
            and "venta" not in replies[-1].casefold() and "balance" not in replies[-1].casefold()
        ),
    ))


async def _v2_money(container, business_id, *, bs=None, rate=None, delta=None, empty=False):
    from sqlalchemy import select
    async with container.session_factory() as session:
        rows = (await session.execute(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)
        ))).scalars().all()
        if empty:
            return not rows
        return (len(rows) == 1 and rows[0].operation_amount_bs == (Decimal(bs) if bs is not None else None)
                and rows[0].exchange_rate == (Decimal(rate) if rate is not None else None)
                and (delta is None or rows[0].balance_delta == Decimal(delta)))


CASES += [
    Case("v2_sale_usd_bs", "brokerage_v2", ["Vendí 100$ 80000bs"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800", delta="1")),
    Case("v2_sale_usd_rate", "brokerage_v2", ["Vendí 100$ a 800"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800")),
    Case("v2_sale_missing_real_money", "brokerage_v2", ["Vendí 100$"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, empty=True), no_writes=True,
         reply_check=lambda replies: "Dime la cantidad de bolívares" in replies[-1]),
    Case("v2_ambiguous_purchase", "brokerage_v2", ["Compré 100$ a Juan 80000bs"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, empty=True),
         reply_check=lambda replies: "pendiente" in replies[-1].lower() and "liquidada" in replies[-1].lower()),
    Case("v2_global_shortcut", "brokerage_v2", ['1000 Juan por 800000bs', 'Pendiente', 'Global'],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="800000", rate="800", delta="10")),
    Case("v2_immediate_global", "brokerage_v2", ["Compra inmediata Juan 100$, pagué 80000bs, es cliente global", "Liquidada"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800", delta="1")),
    Case("v2_inconsistent_triple", "brokerage_v2", ["Vendí 100$, fueron 80000bs a tasa 801"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, empty=True)),
    Case("v2_only_bs_needs_usd", "brokerage_v2", ["Vendí 80000bs"], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, empty=True)),
    Case("v2_receipt_caption", "brokerage_v2", ["Vendí 100$"], image_inputs={0: "receipt_bs80000.png"},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800", delta="1")),
    Case("v2_receipt_after_text", "brokerage_v2", ["Vendí 100$", ""], image_inputs={1: "receipt_bs80000.png"},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800", delta="1")),
    Case("v2_receipt_quoted", "brokerage_v2", ["Vendí 100$", ""], image_inputs={1: "receipt_bs80000.png"},
         quote_last_reply_on_final=True, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800", delta="1")),
]


# D01: assert both the proposed monetary legs and the persisted backend calculation.
def _d01_args(args, *, rate=False, missing=False, purchase=False):
    return (
        money_arg(args, "amount" if purchase and "amount" in args else "operation_amount", "100")
        and (args.get("operation_type", "buy") == "buy" if purchase else args.get("operation_type") == "sell")
        and (args.get("operation_amount_bs") is None if rate or missing
             else money_arg(args, "operation_amount_bs", "80000"))
        and (money_arg(args, "exchange_rate", "800") if rate
             else args.get("exchange_rate") is None)
        and args.get("delivered_amount") is None
    )


for _name, _message, _rate, _missing in [
    ("bs_suffix", "Vendí 100$ 80000bs", False, False),
    ("bs_shorthand", "Vendí 100$ 80000", False, False),
    ("bs_por", "Vendí 100$ por 80000bs", False, False),
    ("bs_bolivares", "Vendí 100 dólares por 80000 bolívares", False, False),
    ("rate_a", "Vendí 100$ a 800", True, False),
    ("rate_tasa", "Vendí 100$ tasa 800", True, False),
    ("rate_a_tasa_de", "Vendí 100$ a tasa de 800", True, False),
    ("missing", "Vendí 100$", False, True),
]:
    CASES.append(Case(
        f"d01_sale_{_name}", "brokerage_d01", [_message], [] if _missing else ["record_broker_operation"], no_writes=_missing,
        forbid_tools=["need_more_info", "ask_choice", "record_broker_purchase"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        arg_checks={"record_broker_operation": lambda a, r=_rate, m=_missing: _d01_args(a, rate=r, missing=m)},
        db_check=lambda c, b, m=_missing: _v2_money(
            c, b, bs=None if m else "80000", rate=None if m else "800", delta="1", empty=m),
    ))

for _name, _message, _rate in [
    ("bs", "Compré 100$ de contado por 80000bs", False),
    ("rate", "Compré 100$ de contado a tasa de 800", True),
]:
    CASES.append(Case(
        f"d01_purchase_{_name}", "brokerage_d01", [_message, "Liquidada"], tools_turn=0,
        forbid_tools=["need_more_info", "ask_choice"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        arg_checks={"record_broker_purchase": lambda a, r=_rate: (
            _d01_args(a, rate=r, purchase=True) and a.get("settlement") in (None, "settled")),
                    "record_broker_operation": lambda a, r=_rate: _d01_args(a, rate=r, purchase=True)},
        db_check=lambda c, b: _v2_money(c, b, bs="80000", rate="800", delta="1"),
    ))

for _name, _correction, _field, _value, _bs, _rate in [
    ("bs", "Corrige el monto en bolívares: fueron 81000bs", "operation_amount_bs", "81000", "81000", "810"),
    ("rate", "Corrige la tasa a 810", "exchange_rate", "810", "81000", "810"),
]:
    CASES.append(Case(
        f"d01_correction_{_name}", "brokerage_d01", ["Vendí 100$ a 800", _correction],
        ["edit_broker_movement"],
        forbid_tools=["delete_broker_movement", "need_more_info", "ask_choice"],
        quote_last_reply_on_final=True,
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        arg_checks={"edit_broker_movement": lambda a, f=_field, v=_value: (
            money_arg(a.get("changes", {}), f, v)
            and a.get("changes", {}).get("exchange_rate" if f == "operation_amount_bs" else "operation_amount_bs") is None)},
        db_check=lambda c, b, bs=_bs, r=_rate: _v2_money(c, b, bs=bs, rate=r, delta="1"),
    ))


async def _d02_customer(container, business_id, expected_name):
    from sqlalchemy import select
    async with container.session_factory() as session:
        rows = (await session.execute(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)
        ))).scalars().all()
        return (len(rows) == 1 and rows[0].customer.name == expected_name
                and rows[0].operation_amount == Decimal("1000") and rows[0].balance_delta == Decimal("10"))


CASES += [
    Case("d02_exact_customer", "brokerage_d02", ['Compra pendiente de 1000$ a tasa 800 para el cliente "QA V2 Global Pendiente"', "Pendiente"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="QA V2 Global Pendiente", customer_classification="global")],
         db_check=lambda c, b: _d02_customer(c, b, "QA V2 Global Pendiente")),
    Case("d02_fuzzy_customer_selection", "brokerage_d02", ['Compra pendiente de 1000$ a tasa 800 para el cliente "QA V2 Global Samu"', "Pendiente", 'QA V2 Global Pendiente'],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="QA V2 Global Pendiente", customer_classification="global")],
         db_check=lambda c, b: _d02_customer(c, b, "QA V2 Global Pendiente")),
    Case("d02_fuzzy_customer_is_new", "brokerage_d02", ['Compra pendiente de 1000$ a tasa 800 para el cliente "QA V2 Global Samu"', "Pendiente", 'Es un cliente nuevo', 'Global'],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="QA V2 Global Pendiente", customer_classification="global")],
         db_check=lambda c, b: _d02_customer(c, b, "QA V2 Global Samu")),
]


async def _d03_sale(container, business_id, *, complete=False):
    from sqlalchemy import select
    async with container.session_factory() as session:
        rows = (await session.execute(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)
        ))).scalars().all()
        return (len(rows) == 1 and rows[0].operation_type == "sell" and rows[0].settlement_status == "settled"
                and rows[0].operation_amount == Decimal("100") and rows[0].commission == Decimal("1")
                and rows[0].balance_delta == Decimal("1")
                and rows[0].operation_amount_bs == (Decimal("80000") if complete else None)
                and rows[0].exchange_rate == (Decimal("800") if complete else None))


for _name, _message, _rate, _missing in [
    ("plain", "Vendí 100$", False, True),
    ("rate", "Vendí 100$ a 800", True, False),
    ("bs", "Vendí 100$ por 80000bs", False, False),
]:
    CASES.append(Case(
        f"d03_sale_{_name}", "brokerage_followups", [_message], [] if _missing else ["record_broker_operation"], no_writes=_missing,
        forbid_tools=["record_broker_purchase", "record_pending_broker_purchase", "need_more_info", "ask_choice"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        arg_checks={"record_broker_operation": lambda a, r=_rate, m=_missing: _d01_args(a, rate=r, missing=m)},
        db_check=lambda c, b, m=_missing: _v2_money(c, b, empty=True) if m else _d03_sale(c, b, complete=True),
    ))
CASES += [
    Case("d03_purchase_stays_ambiguous", "brokerage_followups", ['Compré 100$ a tasa 800'], no_writes=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _v2_money(c, b, empty=True),
         reply_check=lambda replies: "pendiente" in replies[-1].lower() and "liquidada" in replies[-1].lower()),
    Case("d03_receipt_after_sale", "brokerage_followups", ["Vendí 100$", ""],
         ["resume_broker_operation"], image_inputs={1: "receipt_bs80000.png"},
         forbid_tools=["record_broker_purchase", "record_broker_operation"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _d03_sale(c, b, complete=True)),
    Case("d09_unreadable_receipt", "brokerage_followups", ['Vendí 100$ a tasa 800', ''],
         ["complete_broker_receipt"], image_inputs={1: "receipt_unreadable.png"}, quote_last_reply_on_final=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         arg_checks={"complete_broker_receipt": lambda a: a.get("extraction", {}).get("confidence") == "unreadable"
                     and a.get("extraction", {}).get("amount_bs") is None},
         db_check=lambda c, b: _d03_sale(c, b, complete=True),
         reply_check=lambda replies: "no pude leer el monto" in replies[-1].lower()),
    Case("d09_ambiguous_receipt", "brokerage_followups", ['Vendí 100$ a tasa 800', 'No sé cuál de las dos transferencias corresponde a esta venta'],
         ["complete_broker_receipt"], image_inputs={1: "receipt_ambiguous.png"}, quote_last_reply_on_final=True,
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         arg_checks={"complete_broker_receipt": lambda a: a.get("extraction", {}).get("confidence") == "ambiguous"},
         db_check=lambda c, b: _d03_sale(c, b, complete=True),
         reply_check=lambda replies: "identificar con seguridad" in replies[-1].lower()),
]


async def _definitive_check(container, business_id, *, house=False, purchase=False, complete=False):
    from sqlalchemy import select
    async with container.session_factory() as session:
        rows = (await session.execute(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)
        ))).scalars().all()
        if len(rows) != 1:
            return False
        row = rows[0]
        return (row.operation_type == ("buy" if purchase else "sell")
                and row.operation_amount == (987 if purchase else 200)
                and row.received_amount == (1000 if purchase else None)
                and row.excess == (13 if purchase and not house else 0)
                and row.commission == (0 if house else Decimal("9.87") if purchase else 2)
                and row.balance_delta == (0 if house else Decimal("22.87") if purchase else 2)
                and (row.broker_member_id is None) == house
                and row.operation_amount_bs == (Decimal("789600") if purchase and complete else 160000 if complete else None)
                and row.exchange_rate == (800 if complete else None))


for _role in (MemberRole.BROKER, MemberRole.OWNER):
    _house = _role == MemberRole.OWNER
    for _suffix, _message, _complete in [
        ("usd", "Vendí 200", False), ("bs", "Vendí 200 por 160000bs", True),
        ("rate", "Vendí 200 a 800", True), ("tasa", "Venta 200, tasa 800", True),
    ]:
        CASES.append(Case(
            f"definitive_{_role.value}_sale_{_suffix}", "brokerage_definitive", [_message],
            [] if not _complete else ["record_broker_operation"], no_writes=not _complete,
            vertical=BusinessVertical.BROKERAGE, member_role=_role,
            db_check=lambda c, b, h=_house, complete=_complete: (_definitive_check(c, b, house=h, complete=True)
                if complete else _v2_money(c, b, empty=True)),
        ))
    CASES.append(Case(
        f"definitive_{_role.value}_legacy", "brokerage_definitive", ["Vendí 200, entregué 195"],
        ["need_more_info"], no_writes=True, forbid_tools=["record_broker_operation"],
        vertical=BusinessVertical.BROKERAGE, member_role=_role,
        db_check=lambda c, b: _v2_money(c, b, empty=True),
    ))
for _suffix, _message, _complete in [
    ("usd", "Compré 987 a Gamal, recibí 1000", False),
    ("bs", "Compré 987 a Gamal por 789600 Bs, recibí 1000$", True),
]:
    CASES.append(Case(
        f"definitive_purchase_{_suffix}", "brokerage_definitive", [_message, "Liquidada", "Personal"] if _complete else [_message], no_writes=not _complete,
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        db_check=lambda c, b, complete=_complete: (_definitive_check(c, b, purchase=True, complete=True)
            if complete else _v2_money(c, b, empty=True)),
    ))


async def _required_bs_state(container, business_id, *, missing=False, amount="100", kind="sell", settlement="settled"):
    from sqlalchemy import select

    from app.application.conversation import conversation_scope
    from app.db.models import BusinessMember, Customer

    async with container.session_factory() as session:
        rows = (await session.scalars(select(BrokerMovement).where(BrokerMovement.business_id == business_id))).all()
        debts = (await session.scalars(select(Receivable).where(Receivable.business_id == business_id))).all()
        member = (await session.scalars(select(BusinessMember).where(BusinessMember.business_id == business_id))).one()
        draft = await container.store.get_broker_money_draft(conversation_scope(business_id, member.phone))
        if missing:
            customers = (await session.scalars(select(Customer).where(Customer.business_id == business_id))).all()
            return not rows and not debts and not customers and draft is not None
        return (len(rows) == 1 and rows[0].operation_amount == Decimal(amount)
                and rows[0].operation_amount_bs == Decimal(amount) * 800 and rows[0].exchange_rate == 800
                and rows[0].operation_type == kind and rows[0].settlement_status == settlement
                and len(debts) == (1 if settlement == "pending" else 0) and draft is None)


for _role in (MemberRole.OWNER, MemberRole.BROKER):
    for _name, _message in [("sale", "Vendí 100$"), ("purchase", "Compré 100$ de contado"),
                            ("pending", "Pedro 100")]:
        CASES.append(Case(
            f"required_bs_{_role.value}_{_name}_missing", "brokerage_required_bs", [_message],
            no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=_role,
            db_check=lambda c, b: _required_bs_state(c, b, missing=True),
            reply_check=lambda replies: replies[-1] == "Dime la cantidad de bolívares o envíame el capture del pago.",
        ))

for _name, _message in [("bare", "40"), ("withdrawal", "Saqué 40"), ("abbreviation", "SB 40")]:
    CASES.append(Case(
        f"required_bs_own_{_name}_missing", "brokerage_required_bs", [_message],
        no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        member_name="Alex Example", member_abbreviation="SB",
        db_check=lambda c, b: _required_bs_state(c, b, missing=True),
        reply_check=lambda replies: "Dime la cantidad de bolívares" in replies[-1],
    ))

for _name, _response, _images in [("bs", "80000", {}), ("rate", "La tasa fue 800", {}),
                                  ("receipt", "", {1: "receipt_bs80000.png"})]:
    CASES.append(Case(
        f"required_bs_sale_resume_{_name}", "brokerage_required_bs", ["Vendí 100$", _response],
        ["resume_broker_operation"], forbid_tools=["record_broker_account_purchase"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, image_inputs=_images,
        db_check=lambda c, b: _required_bs_state(c, b),
        reply_check=lambda replies: "Dime la cantidad de bolívares" in replies[0]
            and "Venta de divisas registrada" in replies[-1],
    ))

CASES += [
    Case("required_bs_pending_resume", "brokerage_required_bs", ["Pedro 100", "80000", "Pendiente", "Personal"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _required_bs_state(c, b, kind="buy", settlement="pending")),
    Case("required_bs_own_resume", "brokerage_required_bs", ["Saqué 40", "32000"], ["resume_broker_operation"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _required_bs_state(c, b, amount="40", kind="buy", settlement="account")),
    Case("required_bs_receipt_caption", "brokerage_required_bs", ["Vendí 100$"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         image_inputs={0: "receipt_bs80000.png"}, db_check=lambda c, b: _required_bs_state(c, b)),
]
for _name, _file in [("unreadable", "receipt_unreadable.png"), ("ambiguous", "receipt_ambiguous.png")]:
    CASES.append(Case(
        f"required_bs_receipt_{_name}", "brokerage_required_bs", ["Vendí 100$", ""],
        no_writes=True, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        image_inputs={1: _file}, db_check=lambda c, b: _required_bs_state(c, b, missing=True),
        reply_check=lambda replies: uncertain_receipt_reply(replies[-1]),
    ))


async def _no_broker_movements(container, business_id):
    from sqlalchemy import func, select
    async with container.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(BrokerMovement).where(
            BrokerMovement.business_id == business_id)) == 0


async def _immediate_receipt_state(container, business_id, *, bs="95000", attached=True, settlement="settled"):
    from sqlalchemy import select

    from app.db.models import BrokerAttachment
    async with container.session_factory() as session:
        rows = (await session.scalars(select(BrokerMovement).where(BrokerMovement.business_id == business_id))).all()
        debts = (await session.scalars(select(Receivable).where(Receivable.business_id == business_id))).all()
        attachments = (await session.scalars(select(BrokerAttachment).where(
            BrokerAttachment.business_id == business_id, BrokerAttachment.broker_movement_id.is_not(None)))).all()
        return (len(rows) == 1 and rows[0].operation_type == "buy" and rows[0].operation_amount == 100
                and rows[0].operation_amount_bs == Decimal(bs) and rows[0].exchange_rate == Decimal(bs) / 100
                and rows[0].settlement_status == settlement and rows[0].customer.name == "Jessica"
                and rows[0].commission == 1 and rows[0].excess == 0
                and rows[0].balance_delta == 1 and len(debts) == (1 if settlement == "pending" else 0)
                and len(attachments) == int(attached))


for _name, _messages, _images, _attached in [
    ("original", ["", "100$ Jessica zelle recibido"], {0: "receipt_bs95000.png"}, True),
    ("caption", ["100$ Jessica zelle recibido"], {0: "receipt_bs95000.png"}, True),
    ("after", ["Compré 100$ a Jessica, recibido", ""], {1: "receipt_bs95000.png"}, True),
    ("bs", ["Compré 100$ a Jessica, recibido, 95000 Bs"], {}, False),
    ("rate", ["Jessica 100$ recibido, ya le pagué, tasa 950"], {}, False),
    ("pending", ["Compré 100$ a Jessica, pendiente"], {0: "receipt_bs95000.png"}, True),
]:
    CASES.append(Case(
        f"immediate_receipt_{_name}", "brokerage_immediate_receipts", [*_messages, "Pendiente" if _name == "pending" else "Liquidada"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Jessica", customer_classification="global")], image_inputs=_images,
        forbid_tools=["record_broker_receivable_payment", "record_broker_account_payment"],
        db_check=lambda c, b, attached=_attached, state="pending" if _name == "pending" else "settled":
            _immediate_receipt_state(c, b, attached=attached, settlement=state),
    ))

for _name, _messages, _images in [
    ("alone", [""], {0: "receipt_bs95000.png"}),
    ("unrelated", ["", "¿Cuál es mi saldo?"], {0: "receipt_bs95000.png"}),
    ("several", ["", "", "100$ Jessica zelle recibido"],
     {0: "receipt_bs95000.png", 1: "receipt_bs80000.png"}),
    ("unreadable", ["", "100$ Jessica zelle recibido"], {0: "receipt_unreadable.png"}),
    ("ambiguous", ["", "100$ Jessica zelle recibido"], {0: "receipt_ambiguous.png"}),
    ("debt_received", ["Recibí 100$ de Jessica como abono de su deuda"], {}),
    ("unspecified", ["Compré 100$ a Jessica por 95000 Bs"], {}),
]:
    CASES.append(Case(
        f"immediate_receipt_safe_{_name}", "brokerage_immediate_receipts", _messages,
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Jessica", customer_classification="global")], image_inputs=_images,
        no_writes=True,
        db_check=_no_broker_movements,
    ))

CASES.extend([
    Case("immediate_receipt_classification", "brokerage_immediate_receipts",
         ["", "100$ Jessica zelle recibido", "Liquidada", "Global"], image_inputs={0: "receipt_bs95000.png"},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=_immediate_receipt_state),
    Case("immediate_receipt_pending_before", "brokerage_immediate_receipts",
         ["", "Compré 100$ a Jessica, pendiente", "Pendiente"], image_inputs={0: "receipt_bs95000.png"},
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="Jessica", customer_classification="global")],
         db_check=lambda c, b: _immediate_receipt_state(c, b, settlement="pending")),
    Case("immediate_receipt_unrelated_sale", "brokerage_immediate_receipts",
         ["", "Otra operación distinta: vendí 100$ a tasa 800, sin comprobante"],
         image_inputs={0: "receipt_bs95000.png"}, vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=lambda c, b: _required_bs_state(c, b)),
    Case("immediate_receipt_bare_shortcut", "brokerage_immediate_receipts",
         ["Jessica 100$ a tasa 950", "Pendiente"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="Jessica", customer_classification="global")],
         db_check=lambda c, b: _immediate_receipt_state(c, b, attached=False, settlement="pending")),
])


# Nicknames use real persistent resolution, never a model-generated customer identity.
def nickname_state(name: str | None, nickname: str | None, *, movements: int = 0):
    async def check(container, business_id):
        from sqlalchemy import select

        from app.db.models import Customer
        async with container.session_factory() as session:
            customers = (await session.execute(select(Customer).where(Customer.business_id == business_id))).scalars().all()
            rows = (await session.execute(select(BrokerMovement).where(BrokerMovement.business_id == business_id))).scalars().all()
            return (len(customers) == 1 and customers[0].name == name and customers[0].nickname == nickname
                    and len(rows) == movements and all(row.customer_id == customers[0].id for row in rows))
    return check


CASES += [
    Case("nickname_change_by_full_name", "customer_nicknames", ["Cambia el apodo de Julia Example por JEX"],
         ["edit_customer_profile"], seed=[UpsertCustomer(name="Julia Example", customer_classification="global")],
         vertical=BusinessVertical.BROKERAGE, db_check=nickname_state("Julia Example", "JEX")),
    Case("nickname_change_by_alias", "customer_nicknames", ["El apodo de jul ahora es JEX"],
         ["edit_customer_profile"], seed=[UpsertCustomer(name="Julia Example", customer_classification="global")],
         vertical=BusinessVertical.BROKERAGE, db_check=nickname_state("Julia Example", "JEX")),
    Case("nickname_complete_name", "customer_nicknames", ["El nombre de JUL es Julia Example"],
         ["edit_customer_profile"], seed=[UpsertCustomer(nickname="JUL", customer_classification="global")],
         vertical=BusinessVertical.BROKERAGE, db_check=nickname_state("Julia Example", "JUL")),
    Case("nickname_existing_operation", "customer_nicknames", ["Compré 100$ a jul, recibido, tasa 960", "Liquidada"],
         ["record_broker_purchase"], tools_turn=0,
         seed=[UpsertCustomer(name="Julia Example", customer_classification="global")],
         vertical=BusinessVertical.BROKERAGE, db_check=nickname_state("Julia Example", "JUL", movements=1)),
    Case("nickname_create_without_name", "customer_nicknames", ["JUL 100$ tasa 960", "Liquidada", "Personal"],
         vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         db_check=nickname_state(None, "JUL", movements=1)),
    Case("nickname_currency_is_not_customer", "customer_nicknames", ["Vendí 100 USD a tasa 960"],
         ["record_broker_operation"], vertical=BusinessVertical.BROKERAGE,
         db_check=lambda c, b: _nickname_no_customers(c, b)),
    Case("nickname_retail_profile", "customer_nicknames", ["Cambia el apodo de JUL por JEX"],
         ["edit_customer_profile"], seed=[UpsertCustomer(name="Julia Example")],
         db_check=nickname_state("Julia Example", "JEX")),
]


async def _nickname_no_customers(container, business_id):
    from sqlalchemy import func, select

    from app.db.models import Customer
    async with container.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(Customer).where(Customer.business_id == business_id))).scalar_one() == 0


def unified_account_state(expected: str, *, broker_delta: str | None = None):
    async def check(container, business_id):
        from sqlalchemy import select

        from app.application.customer_accounts import account_balances
        async with container.session_factory() as session:
            balances = await account_balances(session, business_id)
            if len(balances) > 1 or sum(balances.values(), Decimal("0")) != Decimal(expected):
                return False
            if broker_delta is not None:
                sales = (await session.scalars(select(BrokerMovement).where(
                    BrokerMovement.business_id == business_id, BrokerMovement.operation_type == "sell",
                    BrokerMovement.deleted_at.is_(None)))).unique().all()
                return len(sales) == 1 and sales[0].balance_delta == Decimal(broker_delta)
            return True
    return check


# Real-model checks use test services, with identities seeded before interpreting financial intent.

CASES += [
    *[Case(f"unified_pending_global_{i}", "unified_customer_accounts", [message],
        ["record_broker_operation"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Hum", customer_classification="global")],
        arg_checks={"record_broker_operation": lambda a: a.get("operation_type") == "sell" and a.get("settlement") == "pending"},
        db_check=unified_account_state("1000", broker_delta="10"))
      for i, message in enumerate(["Venta 1000$ tasa 1000 Hum, pendiente",
                                  "Vendí 1000 dólares a Hum a tasa 1000, no le he pagado",
                                  "Venta 1000$ tasa 1000 Hum, todavía no envié los dólares",
                                  "Venta 1000$ tasa 1000 Hum, queda pendiente de pago"])],
    Case("unified_pending_personal", "unified_customer_accounts", ["Venta pendiente 1000$ tasa 1000 Hum"],
         ["record_broker_operation"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="Hum", customer_classification="personal")],
         db_check=unified_account_state("1000", broker_delta="10")),
    Case("unified_settled_global_unchanged", "unified_customer_accounts", ["Venta 1000$ tasa 1000 Hum"],
         ["record_broker_operation"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
         seed=[UpsertCustomer(name="Hum", customer_classification="global")],
         arg_checks={"record_broker_operation": lambda a: a.get("settlement", "settled") == "settled"},
         db_check=unified_account_state("0", broker_delta="10")),
    Case("unified_customer_payout", "unified_customer_accounts", ["Pagué 100$ a Hum"],
         ["record_broker_customer_payout"], vertical=BusinessVertical.BROKERAGE,
         seed=[UpsertCustomer(name="Hum", customer_classification="global"),
               RecordBrokerOperation(operation_type="sell", settlement="pending", operation_amount="1000",
                                     exchange_rate="1000", customer_name="Hum")],
         forbid_tools=["record_broker_receivable_payment", "record_broker_account_payment"],
         db_check=unified_account_state("900")),
    Case("unified_received_payment", "unified_customer_accounts", ["Hum pagó 100$"],
         ["record_broker_receivable_payment"], vertical=BusinessVertical.BROKERAGE,
         seed=[UpsertCustomer(name="Hum", customer_classification="global"),
               RecordPendingBrokerPurchase(amount="200", exchange_rate="1000", customer_name="Hum")],
         forbid_tools=["record_broker_customer_payout"], db_check=unified_account_state("-100")),
    Case("unified_hum_full_cycle", "unified_customer_accounts", ["Venta 1524$ tasa 1000 Hum, pendiente",
         "Compra pendiente 200$ tasa 1000 Hum", "Pendiente", "Pagué 1324$ a Hum"], ["record_broker_customer_payout"],
         vertical=BusinessVertical.BROKERAGE, seed=[UpsertCustomer(name="Hum", customer_classification="global")],
         forbid_tools=["record_broker_receivable_payment"], db_check=unified_account_state("0")),
]


# QA: identical pending-sale semantics, different opening balances. No production data.
_EXPLANATION_SEED = [
    RecordPendingBrokerPurchase(customer_name="Juan", amount="1000", exchange_rate="900", customer_classification="global"),
    RecordPendingBrokerPurchase(customer_name="Juan", amount="20", exchange_rate="900"),
    RecordBrokerOperation(customer_name="Juan", operation_type="sell", settlement="pending", operation_amount="500", exchange_rate="900"),
    RecordBrokerOperation(customer_name="María Pérez", operation_type="sell", settlement="pending", operation_amount="100", exchange_rate="950", customer_classification="global"),
]


def account_explanation_reply(customer: str, *amounts: int, correction: bool = False):
    def check(replies):
        text = replies[-1].casefold()
        numbers = extract_numbers(text)
        def contains(amount):
            return any(abs(n - amount) < .01 for n in numbers)
        return (customer.casefold() in text
                and all(contains(amount) or (amount == 1020 and contains(1000) and contains(20)) for amount in amounts)
                and not any(term in text for term in ("dos significados", "dos interpretaciones", "pagado los bolívares",
                                                       "no pude registrar", "aclarar qué significa"))
                and (not correction or any(term in text for term in ("equivo", "incorrecta", "error", "malinterpret",
                                                                     "corrijo lo que", "corrijo mi", "rectifico", "dije mal"))))
    return check


CASES += [
    Case("account_explanation_juan", "customer_account_explanations",
         ["¿Por qué Juan debe 520 si hice una venta pendiente de 500?"], ["find_broker_receivables"],
         vertical=BusinessVertical.BROKERAGE, seed=_EXPLANATION_SEED, no_writes=True, expect_lane="agent",
         arg_checks={"find_broker_receivables": lambda a: a.get("include_movements") is True and a.get("customer_name", "").casefold() == "juan"},
         reply_check=account_explanation_reply("Juan", 1020, 500, 520)),
    Case("account_explanation_maria", "customer_account_explanations", ["¿Por qué María tiene 100 a favor?"],
         ["find_broker_receivables"], vertical=BusinessVertical.BROKERAGE, seed=_EXPLANATION_SEED,
         no_writes=True, expect_lane="agent", reply_check=account_explanation_reply("", 100),
         arg_checks={"find_broker_receivables": lambda a: "mar" in a.get("customer_name", "").casefold()}),
    Case("account_explanation_conceptual", "customer_account_explanations",
         ["Si es venta pendiente se supone que nosotros le debemos a Juan, ¿no?"], ["find_broker_receivables"],
         vertical=BusinessVertical.BROKERAGE, seed=_EXPLANATION_SEED, no_writes=True, expect_lane="agent",
         reply_check=account_explanation_reply("Juan", 1020, 500, 520)),
    Case("account_explanation_correct_prior", "customer_account_explanations", ["¿Por qué está mal María? Revisa también a Juan."],
         ["find_broker_receivables"], vertical=BusinessVertical.BROKERAGE, seed=_EXPLANATION_SEED,
         prior_assistant_reply="La venta de María está mal porque tiene 100 a favor. En Juan pendiente significó que no pagó los bolívares. Son dos interpretaciones distintas.",
         no_writes=True, expect_lane="agent", reply_check=account_explanation_reply("María", 100, 1020, 500, 520, correction=True)),
    Case("account_explanation_quote_other_customer", "customer_account_explanations",
         ["¿Por qué Juan debe 520 si hice una venta pendiente de 500?", "¿Por qué está mal María?"],
         ["find_broker_receivables"], vertical=BusinessVertical.BROKERAGE, seed=_EXPLANATION_SEED,
         quote_last_reply_on_final=True, no_writes=True, expect_lane="agent",
         arg_checks={"find_broker_receivables": lambda a: "mar" in a.get("customer_name", "").casefold()},
         reply_check=account_explanation_reply("María", 100)),
    Case("account_explanation_quote_receipt", "customer_account_explanations",
         ["Venta pendiente a María Pérez 100 a 950", "Explícame esta venta, ¿por qué quedó a favor?"],
         ["find_broker_receivables"], vertical=BusinessVertical.BROKERAGE,
         seed=[UpsertCustomer(name="María Pérez", customer_classification="global")],
         quote_last_reply_on_final=True, no_writes=True, expect_lane="agent",
         reply_check=account_explanation_reply("María", 100)),
    Case("account_explanation_missing", "customer_account_explanations",
         ["¿Cómo llegaste al saldo de Roberto Ausente?"], ["find_broker_receivables"],
         vertical=BusinessVertical.BROKERAGE, seed=_EXPLANATION_SEED, no_writes=True, expect_lane="agent",
         arg_checks={"find_broker_receivables": lambda a: "roberto" in a.get("customer_name", "").casefold()},
         reply_check=lambda r: any(t in r[-1].casefold() for t in ("no encontr", "no encuentr", "no tengo", "no aparece", "no hay", "no existe"))
                             and not any(t in r[-1].casefold() for t in ("saldo es cero", "está saldado", "todo saldado"))),
]


async def _commission_sale_state(container, business_id, settlement="settled", scope=None):
    from sqlalchemy import select

    async with container.session_factory() as session:
        rows = (await session.scalars(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)))).unique().all()
        return (len(rows) == 1 and rows[0].operation_type == "sell" and rows[0].operation_amount == 900
                and rows[0].settlement_status == settlement and rows[0].commission == 9
                and rows[0].balance_delta == 9 and rows[0].sales_balance_rule == "commission_only"
                and rows[0].operation_amount_bs == 990000 and rows[0].exchange_rate == 1100
                and (rows[0].customer.classification if rows[0].customer else None) == scope)


# ADR 0034: full-message intent; lexical "pending" must never become a sales router.
for _scope in (None, "personal", "global"):
    for _i, _phrase in enumerate([
        "Venta 900 tasa 1100", "Vendí 900 tasa 1100", "Venta 900, recibí 990000 Bs",
        "Vendí 900, ya enviamos el Zelle, tasa 1100",
    ]):
        CASES.append(Case(
            f"sale_commission_settled_{_scope}_{_i}", "commission_only_sales",
            [_phrase + (" a Carlos" if _scope else "")], ["record_broker_operation"],
            vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
            seed=[UpsertCustomer(name="Carlos", customer_classification=_scope)] if _scope else [],
            forbid_tools=["record_broker_purchase", "record_pending_broker_purchase", "ask_choice", "need_more_info"],
            arg_checks={"record_broker_operation": lambda a: a.get("operation_type") == "sell"
                        and a.get("settlement", "settled") == "settled"},
            db_check=lambda c, b, scope=_scope: _commission_sale_state(c, b, scope=scope),
        ))
for _scope in ("personal", "global"):
    for _i, _phrase in enumerate([
        "Venta 900 pendiente a Carlos tasa 1100", "Vendí 900 a Carlos, falta pagar el Zelle, tasa 1100",
        "Venta 900 a Carlos, todavía no le pagamos, tasa 1100",
    ]):
        CASES.append(Case(
            f"sale_commission_pending_{_scope}_{_i}", "commission_only_sales", [_phrase], ["record_broker_operation"],
            vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
            seed=[UpsertCustomer(name="Carlos", customer_classification=_scope)],
            forbid_tools=["record_broker_purchase", "record_pending_broker_purchase"],
            arg_checks={"record_broker_operation": lambda a: a.get("operation_type") == "sell" and a.get("settlement") == "pending"},
            db_check=lambda c, b, scope=_scope: _commission_sale_state(c, b, "pending", scope),
        ))
for _id, _messages, _tool, _seed in [
    ("buy_pending", ["Compra 900 pendiente a Carlos tasa 1100"], "record_broker_purchase", True),
    ("buy_shortcut", ["Carlos 900 tasa 1100"], "record_broker_purchase", True),
    ("buy_ambiguous_mode", ["Compré 900 a Carlos tasa 1100"], "record_broker_purchase", True),
    ("pending_ambiguous", ["Pendiente 900"], "need_more_info", False),
    ("debt_ambiguous", ["Carlos me debe 900"], "need_more_info", True),
    ("received_payment", ["Carlos pagó 900"], "record_broker_receivable_payment", True),
    ("payout", ["Pagué 900 a Carlos por Zelle"], "record_broker_customer_payout", True),
]:
    CASES.append(Case(
        f"sale_commission_intent_{_id}", "commission_only_sales", _messages, [_tool],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Carlos", customer_classification="personal")] if _seed else [],
        forbid_tools=["record_broker_operation"], no_writes=_id in {"pending_ambiguous", "debt_ambiguous", "buy_ambiguous_mode", "buy_pending", "buy_shortcut"},
    ))
CASES.append(Case(
    "sale_commission_missing_bs_keeps_sale", "commission_only_sales", ["Venta 900", "La tasa fue 1100"],
    ["resume_broker_operation"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
    forbid_tools=["record_broker_purchase", "record_pending_broker_purchase"], db_check=_commission_sale_state,
))


async def _purchase_choice_state(container, business_id, settlement=None, role=MemberRole.BROKER):
    from sqlalchemy import select

    async with container.session_factory() as session:
        rows = (await session.scalars(select(BrokerMovement).where(BrokerMovement.business_id == business_id))).all()
        debts = (await session.scalars(select(Receivable).where(Receivable.business_id == business_id))).all()
        if settlement is None:
            return not rows and not debts
        return (len(rows) == 1 and rows[0].settlement_status == settlement
                and rows[0].operation_type == "buy" and rows[0].operation_amount == 100
                and rows[0].operation_amount_bs == 95000 and rows[0].exchange_rate == 950
                and rows[0].commission == rows[0].balance_delta == (0 if role == MemberRole.OWNER else 1)
                and len(debts) == (1 if settlement == "pending" else 0))


for _role in (MemberRole.OWNER, MemberRole.BROKER):
    for _name, _message, _images in [
        ("shortcut", "Jessica 100$ a tasa 950", {}),
        ("settled", "Compré 100$ a Jessica de contado a tasa 950", {}),
        ("pending", "Compra 100$ a Jessica pendiente a tasa 950", {}),
        ("received", "100$ Jessica zelle recibido a tasa 950", {}),
        ("receipt", "Karol 500$", {0: "receipt_bs95000.png"}),
    ]:
        CASES.append(Case(
            f"purchase_choice_{_role.value}_{_name}_waits", "purchase_choice", [_message],
            forbid_tools=["ask_choice", "record_broker_account_purchase", "record_broker_receivable_payment"],
            vertical=BusinessVertical.BROKERAGE, member_role=_role, image_inputs=_images, no_writes=True,
            seed=[UpsertCustomer(name="Jessica", customer_classification="global")],
            db_check=_purchase_choice_state,
            reply_check=lambda replies: "¿La compra está pendiente o liquidada?" in replies[-1],
        ))
    for _answer, _state in [("Pendiente", "pending"), ("Liquidada", "settled")]:
        CASES.append(Case(
            f"purchase_choice_{_role.value}_{_state}_completes", "purchase_choice",
            ["Jessica 100$ a tasa 950", _answer],
            vertical=BusinessVertical.BROKERAGE, member_role=_role,
            seed=[UpsertCustomer(name="Jessica", customer_classification="global")],
            db_check=lambda c, b, state=_state, role=_role: _purchase_choice_state(c, b, state, role),
            reply_check=lambda replies: "¿La compra está pendiente o liquidada?" in replies[0]
                and "registrada" in replies[-1],
        ))
    CASES.extend([
        Case(
            f"purchase_choice_{_role.value}_missing_customer", "purchase_choice",
            ["Compré 100$ a tasa 950", "Pendiente", "Jessica", "Global"],
            vertical=BusinessVertical.BROKERAGE, member_role=_role,
            db_check=lambda c, b, role=_role: _purchase_choice_state(c, b, "pending", role),
            reply_check=lambda replies: "¿La compra está pendiente o liquidada?" in replies[0]
                and "Qué cliente" in replies[1] and "registrada" in replies[-1],
        ),
        Case(
            f"purchase_choice_{_role.value}_missing_money", "purchase_choice",
            ["Jessica 100$", "Tasa 950", "Liquidada", "Global"],
            vertical=BusinessVertical.BROKERAGE, member_role=_role,
            db_check=lambda c, b, role=_role: _purchase_choice_state(c, b, "settled", role),
            reply_check=lambda replies: "Dime la cantidad de bolívares" in replies[0]
                and "¿La compra está pendiente o liquidada?" in replies[1] and "registrada" in replies[-1],
        ),
    ])


async def _purchase_customer_continuation_state(container, business_id, settlement, role, customer_name):
    from sqlalchemy import select

    from app.db.models import BrokerDraftExecution, Customer

    if not await _purchase_choice_state(container, business_id, settlement, role):
        return False
    async with container.session_factory() as session:
        customers = (await session.scalars(select(Customer).join(BrokerMovement, BrokerMovement.customer_id == Customer.id)
                     .where(BrokerMovement.business_id == business_id))).all()
        executions = (await session.scalars(select(BrokerDraftExecution)
                      .where(BrokerDraftExecution.business_id == business_id))).all()
        return len(customers) == len(executions) == 1 and customers[0].name == customer_name


for _role in (MemberRole.OWNER, MemberRole.BROKER):
    for _state, _answer in [("pending", "Pendiente"), ("settled", "Liquidada")]:
        for _source in ("text", "receipt"):
            for _new_customer in (False, True):
                _messages = ["Nora 100$ a tasa 950"] if _source == "text" else ["Nora 100$", ""]
                _messages += [_answer, "Es un cliente nuevo", "Global"] if _new_customer else [_answer, "Norma"]
                CASES.append(Case(
                    f"purchase_continuation_{_role.value}_{_state}_{_source}_{'new' if _new_customer else 'existing'}",
                    "purchase_continuation", _messages,
                    vertical=BusinessVertical.BROKERAGE, member_role=_role,
                    seed=[UpsertCustomer(name="Norma", customer_classification="global")],
                    image_inputs={1: "receipt_bs95000.png"} if _source == "receipt" else {},
                    db_check=lambda c, b, state=_state, role=_role, customer="Nora" if _new_customer else "Norma":
                        _purchase_customer_continuation_state(c, b, state, role, customer),
                    reply_check=lambda replies: sum("¿La compra está pendiente o liquidada?" in reply for reply in replies) == 1
                        and any("¿Quién es «Nora»?" in reply for reply in replies) and "registrada" in replies[-1],
                ))


async def _reconciliation_state(container, business_id, *, expected=None, empty=False, expected_count=1):
    from sqlalchemy import select

    from app.db.models import CustomerBalanceAdjustment
    async with container.session_factory() as session:
        brokers = (await session.scalars(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id))).unique().all()
        customers = (await session.scalars(select(CustomerBalanceAdjustment).where(
            CustomerBalanceAdjustment.business_id == business_id))).unique().all()
        if empty:
            return not brokers and not customers
        deltas = [row.balance_delta for row in brokers] + [row.amount_delta for row in customers]
        return (len(deltas) == expected_count and sum(deltas) == Decimal(expected)
                and all(row.kind == "adjustment" and row.commission is None and row.operation_type is None for row in brokers))


for _suffix, _message, _expected in [
    ("debt", "ajustar saldo Alexandra 31/10 deuda 900", "-900"),
    ("negative", "AJUSTAR SALDO Alexandra al 31/10 -700", "-700"),
    ("favor", "ajustar saldo Alexandra 31/10 favor 500", "500"),
    ("a_favor", "Ajustar saldo Alexandra al 31/10 a favor 500", "500"),
    ("positive", "AJUSTAR SALDO Alexandra 31/10 +300", "300"),
    ("natural_date", "Ajustar saldo Alexandra al 31 de octubre, debía 1200 dólares", "-1200"),
    ("explicit_zero", "ajustar saldo Alexandra al 31/10 saldo cero", None),
]:
    CASES.append(Case(f"reconciliation_{_suffix}", "reconciliation", [_message, "sí"],
        expect_tools=["reconcile_balance"], tools_turn=0,
        seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")], vertical=BusinessVertical.BROKERAGE,
        db_check=(lambda c, b: _reconciliation_state(c, b, empty=True)) if _expected is None else
                 (lambda c, b, amount=_expected: _reconciliation_state(c, b, expected=amount)),
        forbid_tools=["record_broker_operation", "record_broker_account_payment", "record_broker_purchase"],
        notes="Saldo objetivo interpretado por el modelo; confirmación y escritura deterministas."))

CASES += [
    Case("reconciliation_missing_direction", "reconciliation", ["ajustar saldo Alexandra 31/10 900"],
        vertical=BusinessVertical.BROKERAGE, seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")],
        db_check=lambda c, b: _reconciliation_state(c, b, empty=True),
        reply_check=lambda replies: "deuda" in replies[0].casefold() and "favor" in replies[0].casefold()),
    Case("reconciliation_continue_direction", "reconciliation", ["ajustar saldo Alexandra 31/10 900", "deuda", "sí"],
        expect_tools=["reconcile_balance"], tools_turn=1, vertical=BusinessVertical.BROKERAGE,
        seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")],
        db_check=lambda c, b: _reconciliation_state(c, b, expected="-900")),
    Case("reconciliation_without_prefix", "reconciliation", ["Alexandra debía 900 dólares al 31 de octubre"],
        vertical=BusinessVertical.BROKERAGE, seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")],
        forbid_tools=["reconcile_balance"], db_check=lambda c, b: _reconciliation_state(c, b, empty=True)),
    Case("reconciliation_customer_credit", "reconciliation", ["ajustar saldo María al 31/10 a favor 500", "sí"],
        expect_tools=["reconcile_balance"], tools_turn=0, vertical=BusinessVertical.BROKERAGE,
        seed=[UpsertCustomer(name="María", customer_classification="global")],
        db_check=lambda c, b: _reconciliation_state(c, b, expected="500")),
]

CASES += [
    Case("reconciliation_repeat_target", "reconciliation", ["ajustar saldo Alexandra 31/10 deuda 900", "sí",
         "ajustar saldo Alexandra 31/10 deuda 900"], expect_tools=["reconcile_balance"],
         seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")], vertical=BusinessVertical.BROKERAGE,
         db_check=lambda c, b: _reconciliation_state(c, b, expected="-900"),
         reply_check=lambda replies: "ya coincide" in replies[-1].casefold()),
    Case("reconciliation_correct_target", "reconciliation", ["ajustar saldo Alexandra 31/10 deuda 700", "sí",
         "ajustar saldo Alexandra 31/10 deuda 800", "sí"], expect_tools=["reconcile_balance"], tools_turn=2,
         seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")], vertical=BusinessVertical.BROKERAGE,
         db_check=lambda c, b: _reconciliation_state(c, b, expected="-800", expected_count=2)),
]

for _suffix, _message in [
    ("noun_adjustment", "ajuste saldo Alexandra debia 1000 al 31-10"),
    ("imperative", "ajusta el saldo de Alexandra al 31 de octubre: debía 1000"),
    ("request", "quiero ajustar el saldo de Alexandra al 31/10 deuda 1000"),
]:
    CASES.append(Case(f"reconciliation_semantic_{_suffix}", "reconciliation", [_message, "sí"],
        expect_tools=["reconcile_balance"], tools_turn=0,
        seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")], vertical=BusinessVertical.BROKERAGE,
        db_check=lambda c, b: _reconciliation_state(c, b, expected="-1000"),
        forbid_tools=["record_broker_operation", "record_broker_account_payment", "record_broker_purchase"]))

for _suffix, _message in [
    ("question", "¿Cuánto debía Alexandra al 31 de octubre?"),
    ("explain", "¿Qué significa ajustar saldo?"),
    ("payment", "Alexandra abonó 1000 dólares a su cuenta"),
]:
    CASES.append(Case(f"reconciliation_not_adjustment_{_suffix}", "reconciliation", [_message],
        seed=[CreateBrokerMember(name="Alexandra", phone="584140008888")], vertical=BusinessVertical.BROKERAGE,
        forbid_tools=["reconcile_balance", "delete_balance_adjustment"]))


async def _quoted_customer_reassigned_without_restatement(container, business_id):
    from sqlalchemy import select

    from app.db.models import Customer

    async with container.session_factory() as session:
        rows = (await session.scalars(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id))).unique().all()
        if len(rows) != 2:
            return False
        corrected = next((row for row in rows if row.operation_amount == Decimal("1000")), None)
        if corrected is None:
            return False
        customer = await session.get(Customer, corrected.customer_id)
        return (customer is not None and customer.name == "Diego Example"
                and corrected.operation_amount_bs == Decimal("1040000")
                and corrected.exchange_rate == Decimal("1040") and corrected.balance_delta == 0)


CASES.append(Case(
    "grounding_quoted_customer_clarification", "grounding",
    ["Venta 1000 dólares a tasa 1040 para DEX, cliente global", "Es Diego Example"],
    vertical=BusinessVertical.BROKERAGE, quote_last_reply_on_final=True,
    seed=[RecordBrokerOperation(customer_name="Diego Example", customer_classification="global",
                               operation_type="sell", operation_amount="1220", exchange_rate="1035")],
    expect_tools=["edit_broker_movement"],
    forbid_tools=["record_broker_operation", "delete_broker_movement", "edit_customer_profile"],
    arg_checks={"edit_broker_movement": lambda args: args.get("changes", {}).get("customer_name") == "Diego Example"
                and args.get("changes", {}).get("amount") is None
                and args.get("changes", {}).get("exchange_rate") is None},
    db_check=_quoted_customer_reassigned_without_restatement,
))


def direct_movement_state(kind: str, amount: str, *, opening_balance: str | None = None):
    """Assert ledger effects and absence of customer/commercial writes."""
    async def check(container, business_id):
        from sqlalchemy import select

        from app.application.customer_accounts import account_balances
        from app.db.models import CustomerPayment

        async with container.session_factory() as session:
            movements = (await session.scalars(select(BrokerMovement).where(
                BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None)))).unique().all()
            if len(movements) != (2 if opening_balance is not None else 1):
                return False
            row = next((movement for movement in movements if movement.kind == kind
                        and movement.balance_delta == Decimal(amount)), None)
            if row is None:
                return False
            payments = (await session.scalars(select(CustomerPayment.id).where(CustomerPayment.business_id == business_id))).all()
            debts = (await session.scalars(select(Receivable.id).where(Receivable.business_id == business_id))).all()
            balances = await account_balances(session, business_id)
            return (row.kind == kind and row.balance_delta == Decimal(amount)
                    and row.commission is None and row.operation_amount is None and row.customer_id is None
                    and row.related_payment_id is None and row.related_receivable_id is None
                    and row.operation_amount_bs is None and row.exchange_rate is None
                    and not payments and not debts and all(value == 0 for value in balances.values())
                    and all(movement.commission is None and movement.customer_id is None for movement in movements)
                    and sum((movement.balance_delta for movement in movements), Decimal("0"))
                        == Decimal(opening_balance or "0") + Decimal(amount))
    return check


_DIRECT_TOOLS = ["record_broker_account_payment", "record_broker_account_withdrawal"]
_COMMERCIAL_TOOLS = ["record_broker_operation", "record_broker_purchase", "record_broker_account_purchase",
                     "record_broker_receivable_payment", "record_broker_customer_payout", "reconcile_balance"]

for _suffix, _message, _amount, _kind in [
    ("add", "Súmale 500$ a mi balance", "500", "in"),
    ("add_variant", "sumale 1000 a mi saldo", "1000", "in"),
    ("subtract", "Réstale 500$ a mi balance", "-500", "out"),
    ("subtract_cash", "Réstale 1240$ a mi balance por una entrega en efectivo", "-1240", "out"),
    ("subtract_variant", "restale 500 a mi saldo", "-500", "out"),
    ("add_natural_variant", "Aumenta mi balance en 500$", "500", "in"),
    ("own_balance_to_customer", "Réstale 500$ a mi balance por la entrega a Sandra Contreras", "-500", "out"),
    ("account_payment", "Pagué 500$ a mi cuenta", "500", "in"),
    ("return_to_business", "Entregué 500$ al negocio", "500", "in"),
]:
    _tool = _DIRECT_TOOLS[0] if _kind == "in" else _DIRECT_TOOLS[1]
    CASES.append(Case(f"direct_balance_{_suffix}", "direct_balance", [_message], [_tool],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Sandra Contreras", customer_classification="global")]
             if _suffix == "own_balance_to_customer" else [],
        forbid_tools=[*_COMMERCIAL_TOOLS, *[t for t in _DIRECT_TOOLS if t != _tool]],
        db_check=direct_movement_state(_kind, _amount)))

for _suffix, _message, _amount in [
    ("paid_customer", "Pagué 500$ a Sandra Contreras", "-500"),
    ("delivered_customer", "Entregué 500$ a Sandra Contreras", "-500"),
    ("historical_sandra", "Entregué en cash 1.240$ a Sandra Contreras", "-1240"),
    ("historical_roldan", "Le entregué 100$ en efectivo a Roldan", "-100"),
]:
    CASES.append(Case(f"direct_balance_preserve_{_suffix}", "direct_balance", [_message],
        ["record_broker_customer_payout"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Sandra Contreras" if _suffix != "historical_roldan" else "Roldan", customer_classification="global")],
        forbid_tools=[*_DIRECT_TOOLS, "record_broker_operation", "record_broker_account_purchase"],
        db_check=unified_account_state(_amount)))

CASES += [
    Case("direct_balance_preserve_sale", "direct_balance", ["Vendí 500$ a tasa 950"],
        ["record_broker_operation"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        forbid_tools=_DIRECT_TOOLS, arg_checks={"record_broker_operation": lambda a: a.get("operation_type") == "sell"}),
    Case("direct_balance_preserve_purchase", "direct_balance", ["Compré 500$ a tasa 950"],
        ["record_broker_purchase"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        forbid_tools=_DIRECT_TOOLS, no_writes=True),
    Case("direct_balance_preserve_received_purchase", "direct_balance", ["Recibí 500$ de una compra a tasa 950"],
        ["record_broker_purchase"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        forbid_tools=_DIRECT_TOOLS, no_writes=True),
    Case("direct_balance_preserve_withdrawal_shortcut", "direct_balance", ["Saqué 500$"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, forbid_tools=_DIRECT_TOOLS,
        expect_attempted_tools=["record_broker_account_purchase"],
        arg_checks={"record_broker_account_purchase": lambda a: money_arg(a, "amount", "500")}, no_writes=True),
    Case("direct_balance_preserve_bare_amount", "direct_balance", ["500"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER, forbid_tools=_DIRECT_TOOLS,
        expect_attempted_tools=["record_broker_account_purchase"],
        arg_checks={"record_broker_account_purchase": lambda a: money_arg(a, "amount", "500")}, no_writes=True),
    Case("direct_balance_preserve_historical_received", "direct_balance", ["¿Cuánto debe CRios?", "Recibido Zelle de 400$ de CRios"],
        ["record_broker_receivable_payment"], vertical=BusinessVertical.BROKERAGE,
        seed=[UpsertCustomer(name="CRios", customer_classification="global"),
              RecordPendingBrokerPurchase(amount="400", exchange_rate="950", customer_name="CRios")],
        forbid_tools=[*_DIRECT_TOOLS, "record_broker_purchase"], db_check=unified_account_state("0")),
    Case("direct_balance_context_received_debt", "direct_balance", ["¿Cuánto debe Sandra Contreras?", "Recibí 500$"],
        ["record_broker_receivable_payment"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Sandra Contreras", customer_classification="global"),
              RecordPendingBrokerPurchase(amount="500", exchange_rate="950", customer_name="Sandra Contreras")],
        forbid_tools=_DIRECT_TOOLS, db_check=unified_account_state("0")),
    Case("direct_balance_context_delivered_obligation", "direct_balance", ["¿Cuánto le debemos a Sandra Contreras?", "Entregué 500$"],
        ["record_broker_customer_payout"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Sandra Contreras", customer_classification="global"),
              RecordBrokerOperation(operation_type="sell", settlement="pending", operation_amount="500", exchange_rate="950", customer_name="Sandra Contreras")],
        forbid_tools=_DIRECT_TOOLS, db_check=unified_account_state("0", broker_delta="5")),
    Case("direct_balance_accumulated", "direct_balance", ["Súmale 500$ a mi balance", "Réstale 1240$ a mi balance"],
        ["record_broker_account_withdrawal"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        forbid_tools=_COMMERCIAL_TOOLS, reply_check=lambda replies: "-$740,00" in replies[-1]),
    Case("direct_balance_owner_selects_broker", "direct_balance", ["Réstale 500$ al balance de Alex"],
        ["record_broker_account_withdrawal"], vertical=BusinessVertical.BROKERAGE,
        seed=[CreateBrokerMember(name="Alex", phone="584140008888")], db_check=direct_movement_state("out", "-500")),
    Case("direct_balance_ambiguous_sale_collection", "direct_balance", ["Recibí 50 por una venta"],
        ["need_more_info"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        forbid_tools=_DIRECT_TOOLS, no_writes=True),
    Case("direct_balance_existing_payment_not_duplicated", "direct_balance", ["¿Cuál es mi balance?"],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[RecordBrokerAccountPayment(amount="500")], forbid_tools=_DIRECT_TOOLS, no_writes=True,
        db_check=direct_movement_state("in", "500")),
]


async def _no_broker_financial_writes(container, business_id):
    from sqlalchemy import func, select

    from app.db.models import CustomerBalanceAdjustment, CustomerPayment

    async with container.session_factory() as session:
        for model in (BrokerMovement, Receivable, CustomerPayment, CustomerBalanceAdjustment):
            if await session.scalar(select(func.count()).select_from(model).where(model.business_id == business_id)):
                return False
        return True


for _suffix, _message in [
    ("received", "Recibí 500$"),
    ("received_house", "Recibí 1000$ de Casa"),
    ("delivered", "Entregué 500$"),
    ("delivered_cash", "Entregué en cash 1240$"),
    ("delivered_own_balance", "Entregué 500$ de mi saldo"),
    ("delivered_own_balance_customer", "Entregué 500$ de mi saldo a Sandra Contreras"),
]:
    CASES.append(Case(f"direct_balance_ambiguous_{_suffix}", "direct_balance", [_message],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        seed=[UpsertCustomer(name="Sandra Contreras", customer_classification="global")]
             if _suffix == "delivered_own_balance_customer" else [],
        no_writes=True, forbid_attempted_tools=[*_DIRECT_TOOLS, *_COMMERCIAL_TOOLS, "record_pending_broker_purchase"],
        db_check=_no_broker_financial_writes, reply_check=lambda replies: "?" in replies[-1]))

for _suffix, _opening, _message, _delta, _kind, _display in [
    ("add_to_debt", "-200", "Súmale 500$ a mi balance", "500", "in", "+$300,00"),
    ("add_to_credit", "200", "Súmale 500$ a mi balance", "500", "in", "+$700,00"),
    ("subtract_from_debt", "-200", "Réstale 500$ a mi balance", "-500", "out", "-$700,00"),
    ("subtract_from_credit", "200", "Réstale 500$ a mi balance", "-500", "out", "-$300,00"),
]:
    CASES.append(Case(f"direct_balance_sign_{_suffix}", "direct_balance", [_message],
        [_DIRECT_TOOLS[0] if _kind == "in" else _DIRECT_TOOLS[1]],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        member_name="Alex" if Decimal(_opening) < 0 else "Samu",
        seed=[RecordBrokerAccountPayment(amount="200") if Decimal(_opening) > 0 else RecordBrokerAccountWithdrawal(amount="200")],
        forbid_tools=_COMMERCIAL_TOOLS,
        db_check=direct_movement_state(_kind, _delta, opening_balance=_opening),
        reply_check=lambda replies, expected=_display: expected in replies[-1]))

for _suffix, _messages, _kind, _delta in [
    ("clarify_add", ["Recibí 500$", "Súmalo a mi balance"], "in", "500"),
    ("clarify_subtract", ["Entregué 500$", "Réstalo de mi balance"], "out", "-500"),
    ("missing_add_amount", ["Quiero sumarle a mi balance", "500"], "in", "500"),
    ("missing_subtract_amount", ["Quiero restarle a mi balance", "500"], "out", "-500"),
    ("add_delta_not_target", ["Ajusta mi balance sumándole 500$"], "in", "500"),
    ("subtract_delta_not_target", ["Ajusta mi balance restándole 500$"], "out", "-500"),
]:
    CASES.append(Case(f"direct_balance_{_suffix}", "direct_balance", _messages,
        [_DIRECT_TOOLS[0] if _kind == "in" else _DIRECT_TOOLS[1]],
        vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
        forbid_tools=_COMMERCIAL_TOOLS, db_check=direct_movement_state(_kind, _delta)))

CASES.append(Case("direct_balance_explicit_over_customer_context", "direct_balance",
    ["¿Cuánto debe Sandra Contreras?", "Súmale 500$ a mi balance, sin tocar la deuda de Sandra"],
    ["record_broker_account_payment"], vertical=BusinessVertical.BROKERAGE, member_role=MemberRole.BROKER,
    seed=[UpsertCustomer(name="Sandra Contreras", customer_classification="global")],
    forbid_tools=_COMMERCIAL_TOOLS, db_check=direct_movement_state("in", "500")))

for _suffix, _kind, _amount, _member in [
    ("debt", "out", "-200", "Alex"),
    ("credit", "in", "200", "Samu"),
]:
    CASES.append(Case(f"direct_balance_ambiguous_after_{_suffix}", "direct_balance",
        ["¿Cuál es mi balance?", "Entregué 500$"], vertical=BusinessVertical.BROKERAGE,
        member_role=MemberRole.BROKER, member_name=_member,
        seed=[RecordBrokerAccountPayment(amount="200") if _kind == "in" else RecordBrokerAccountWithdrawal(amount="200")],
        no_writes=True, forbid_attempted_tools=[*_DIRECT_TOOLS, *_COMMERCIAL_TOOLS],
        db_check=direct_movement_state(_kind, _amount), reply_check=lambda replies: "?" in replies[-1]))
