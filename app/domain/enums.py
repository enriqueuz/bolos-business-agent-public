"""Fuente única de enums. El schema de las tools, la validación, la DB y los prompts salen de aquí."""

from __future__ import annotations

from enum import StrEnum

from app.domain.currencies import Currency  # noqa: F401  (re-export: el enum sale del registro de monedas)


class CurrencyView(StrEnum):
    """Pata congelada con la que se reporta. Nunca se reconvierte."""

    LOCAL = "local"  # moneda local del negocio (VES, COP…)
    USD_APPLIED = "usd_applied"  # USD a la tasa para cobrar aplicada en cada movimiento (la de los reportes)
    USD_OFFICIAL = "usd_official"  # USD a tasa oficial (BCV, TRM…): referencia
    USD_MARKET = "usd_market"  # USD a tasa de mercado automática: referencia


class ChargeRateMode(StrEnum):
    """Tasa que usa el negocio para cobrar en moneda local lo que tiene precio en USD."""

    OFFICIAL = "official"  # BCV, TRM…
    MARKET = "market"  # mercado automática
    CUSTOM = "custom"  # tasa propia del dueño (CustomRate purpose=charge)


class RateAppliedSource(StrEnum):
    """De dónde salió la tasa aplicada congelada en un movimiento."""

    OFFICIAL = "official"
    MARKET = "market"
    CUSTOM = "custom"
    INLINE = "inline"  # legado: tasa dicha en el mensaje (se elimina en la Parte 2)
    EXCHANGE = "exchange"  # solo en la vista money_movements: la tasa del propio cambio


class RatePurpose(StrEnum):
    CHARGE = "charge"  # tasa para cobrar
    FUNDS = "funds"  # tasa real de los bolívares del negocio


class RateScope(StrEnum):
    """Para qué es una tasa que da el dueño ("pon la tasa en 800"): cobrar, tasa real de sus bolívares, o ambas."""

    CHARGE = "charge"
    FUNDS = "funds"
    BOTH = "both"


class CustomRateSource(StrEnum):
    MANUAL = "manual"  # la dijo el dueño
    EXCHANGE = "exchange"  # salió de un cambio de moneda registrado


class SaleSettlement(StrEnum):
    """Cómo se resolvió la diferencia entre lo que costaba la venta y lo que se pagó."""

    EXACT = "exact"  # pagó lo que correspondía
    DEBT = "debt"  # el resto quedó debiendo (fiado)
    DISCOUNT = "discount"  # el resto se descontó


class RateKind(StrEnum):
    OFFICIAL = "official"
    MARKET = "market"


class PaymentMethod(StrEnum):
    """Valores neutros por país. Cómo se llama cada uno en cada país lo decide su perfil (countries.py)."""

    CASH = "cash"  # efectivo; la moneda va aparte (currency / paid_currency)
    MOBILE_PAYMENT = "mobile_payment"  # pago móvil (VE), Nequi/Daviplata (CO), Yape/Plin (PE)…
    TRANSFER = "transfer"
    CARD = "card"  # punto de venta / datáfono
    ZELLE = "zelle"
    CRYPTO = "crypto"
    BINANCE = "binance"
    OTHER = "other"


# Etiquetas neutras (sirven en cualquier país). Cada CountryProfile puede sobreescribirlas.
PAYMENT_METHOD_LABELS: dict[PaymentMethod, str] = {
    PaymentMethod.CASH: "Efectivo",
    PaymentMethod.MOBILE_PAYMENT: "Pago móvil",
    PaymentMethod.TRANSFER: "Transferencia",
    PaymentMethod.CARD: "Tarjeta",
    PaymentMethod.ZELLE: "Zelle",
    PaymentMethod.CRYPTO: "Cripto",
    PaymentMethod.BINANCE: "Binance",
    PaymentMethod.OTHER: "Otro",
}


class PaymentStatus(StrEnum):
    PAID = "paid"
    CREDIT = "credit"  # fiado completo
    PARTIAL = "partial"  # abonó una parte, el resto queda fiado


class SaleResolution(StrEnum):
    """Qué pasó cuando el precio o el pago no cuadran con la tasa para cobrar. Lo eligen los botones (o el dueño lo dice)."""

    LOWER_PRICE = "lower_price"  # "lo vendí más barato": falta saber si solo esta venta o cambia el precio habitual
    THIS_SALE_ONLY = "this_sale_only"  # precio más bajo solo en esta venta; el habitual no cambia
    UPDATE_PRICE_ASK = "update_price_ask"  # quiere cambiar el precio habitual: falta confirmarlo
    UPDATE_PRICE = "update_price"  # confirmado: el precio de esta venta pasa a ser el habitual
    DISCOUNT = "discount"  # la diferencia fue un descuento (precio o pago)
    DEBT = "debt"  # la diferencia quedó debiendo (fiado)


# Moneda que implica un método de pago cuando el dueño no la dice ("por pago móvil" = moneda local).
LOCAL_METHODS = frozenset({"mobile_payment", "card", "transfer"})
USD_METHODS = frozenset({"zelle"})


class ExpenseCategory(StrEnum):
    RENT = "rent"
    UTILITIES = "utilities"  # luz, agua, internet, teléfono
    PAYROLL = "payroll"  # sueldos, pagos a ayudantes
    TRANSPORT = "transport"  # gasolina, fletes, delivery
    MAINTENANCE = "maintenance"  # reparaciones del local, equipos
    SUPPLIES = "supplies"  # bolsas, papelería, limpieza (no mercancía para vender)
    TAXES_FEES = "taxes_fees"  # impuestos, alcaldía, comisiones bancarias
    FOOD = "food"  # comida del día a día del negocio
    MARKETING = "marketing"
    OTHER = "other"


EXPENSE_CATEGORY_LABELS: dict[ExpenseCategory, str] = {
    ExpenseCategory.RENT: "Alquiler",
    ExpenseCategory.UTILITIES: "Servicios",
    ExpenseCategory.PAYROLL: "Sueldos",
    ExpenseCategory.TRANSPORT: "Transporte",
    ExpenseCategory.MAINTENANCE: "Mantenimiento",
    ExpenseCategory.SUPPLIES: "Insumos",
    ExpenseCategory.TAXES_FEES: "Impuestos y comisiones",
    ExpenseCategory.FOOD: "Comida",
    ExpenseCategory.MARKETING: "Publicidad",
    ExpenseCategory.OTHER: "Otros",
}


class NoteKind(StrEnum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    PRODUCT = "product"
    POLICY = "policy"  # horarios, reglas del negocio
    GENERAL = "general"


class EntityType(StrEnum):
    SALE = "sale"
    EXPENSE = "expense"
    PURCHASE = "purchase"
    RECEIVABLE = "receivable"
    PAYMENT = "payment"
    PRODUCT = "product"
    CUSTOMER = "customer"
    NOTE = "note"
    CUSTOM_RATE = "custom_rate"
    CASH_SESSION = "cash_session"
    BUSINESS = "business"
    EXCHANGE = "exchange"
    BROKER_MOVEMENT = "broker_movement"
    BROKER_ATTACHMENT = "broker_attachment"
    CUSTOMER_BALANCE_ADJUSTMENT = "customer_balance_adjustment"


ENTITY_LABELS: dict[EntityType, str] = {
    EntityType.SALE: "venta",
    EntityType.EXPENSE: "gasto",
    EntityType.PURCHASE: "compra",
    EntityType.RECEIVABLE: "fiado",
    EntityType.PAYMENT: "abono",
    EntityType.PRODUCT: "producto",
    EntityType.CUSTOMER: "cliente",
    EntityType.NOTE: "nota",
    EntityType.CUSTOM_RATE: "tasa",
    EntityType.CASH_SESSION: "cierre de caja",
    EntityType.BUSINESS: "negocio",
    EntityType.EXCHANGE: "cambio",
    EntityType.BROKER_MOVEMENT: "movimiento de corredor",
    EntityType.BROKER_ATTACHMENT: "comprobante",
    EntityType.CUSTOMER_BALANCE_ADJUSTMENT: "ajuste de saldo",
}


class ActorType(StrEnum):
    AGENT = "agent"
    PANEL = "panel"
    SYSTEM = "system"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class LinkRelation(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    SHOWN = "shown"


class MessageKind(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class OnboardingState(StrEnum):
    IN_PROGRESS = "in_progress"
    DONE = "done"


class MemberRole(StrEnum):
    OWNER = "owner"
    BROKER = "broker"


class BusinessVertical(StrEnum):
    RETAIL = "retail"
    BROKERAGE = "brokerage"


class Capability(StrEnum):
    """Módulos conocidos por el código; se resuelven en backend y se publican en /me."""

    RETAIL_OPERATIONS = "retail.operations"
    BROKER_LEDGER = "brokerage.ledger"
    RECEIVABLES = "receivables"
    ATTACHMENTS = "attachments"
    EXPORTS = "exports"


class Lane(StrEnum):
    FAST = "fast"
    AGENT = "agent"
    ONBOARDING = "onboarding"
    DETERMINISTIC = "deterministic"  # resuelto sin LLM (menú, confirmaciones)


class TurnStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class ToolOutcome(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ERROR = "error"
