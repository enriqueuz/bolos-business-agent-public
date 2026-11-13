"""Modelo de persistencia (SQLAlchemy 2.0).

Convenciones:
- Toda tabla de negocio lleva `business_id` (tenancy desde el día uno).
- Dinero: NUMERIC(18,4); tasas NUMERIC(18,6). Patas congeladas por fila (ver app/domain/money.py).
- Prefijos de las patas: `amount_*` movimiento sin productos, `total_*` venta completa, `item_*` un producto dentro de
  una venta o compra, `paid_*` lo que el cliente pagó realmente, `discount_*` el descuento.
- Soft delete con `deleted_at` en todo lo transaccional; `version` para bloqueo optimista.
- El bloqueo optimista lo hace la base de datos (`version_id_col`): cada UPDATE lleva `WHERE version = <la leída>` y, si
  otra transacción ganó primero, el UPDATE no toca ninguna fila y el CommandBus lo traduce a `Stale`. El `version`
  se sube a mano con `bump()` (por eso `version_id_generator=False`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

from app.domain.countries import DEFAULT_COUNTRY, country_profile
from app.domain.enums import (
    ActorType,
    AuditAction,
    BusinessVertical,
    ChargeRateMode,
    Currency,
    CurrencyView,
    CustomRateSource,
    EntityType,
    ExpenseCategory,
    LinkRelation,
    MemberRole,
    MessageKind,
    NoteKind,
    OnboardingState,
    PaymentMethod,
    PaymentStatus,
    RateAppliedSource,
    RateKind,
    RatePurpose,
    SaleSettlement,
    ToolOutcome,
    TurnStatus,
)
from app.domain.money import MoneyLegs

_DEFAULT_PROFILE = country_profile(DEFAULT_COUNTRY)

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

AMOUNT = Numeric(18, 4)
RATE = Numeric(18, 6)


def enum_col(enum_cls: type, **kw: Any) -> Any:
    # Guardamos enums como texto: migrar valores es trivial y no hay ALTER TYPE.
    return mapped_column(String(32), **kw)


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RatesMixin:
    """Tasas congeladas de la escritura (compartidas por todas las patas de la fila).

    `rate_applied` es la tasa para cobrar que se usó; `rate_official` y `rate_market` son las automáticas del momento,
    guardadas solo como referencia.
    """

    rate_official: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_market: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_applied: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_applied_source: Mapped[str] = enum_col(RateAppliedSource, nullable=False)

    def set_rates(self, legs: MoneyLegs) -> None:
        self.rate_official = legs.rate_official
        self.rate_market = legs.rate_market
        self.rate_applied = legs.rate_applied
        self.rate_applied_source = legs.rate_applied_source.value


class AmountMixin(RatesMixin):
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = enum_col(Currency, nullable=False)
    amount_local: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    amount_usd_official: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    amount_usd_market: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    amount_usd_applied: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)

    def set_amount_legs(self, legs: MoneyLegs) -> None:
        self.amount = legs.amount
        self.currency = str(legs.currency)
        self.amount_local = legs.local
        self.amount_usd_official = legs.usd_official
        self.amount_usd_market = legs.usd_market
        self.amount_usd_applied = legs.usd_applied
        self.set_rates(legs)


class RealCostMixin:
    """Salidas de dinero: cuánto le costaron realmente al negocio, según la tasa a la que obtuvo sus bolívares.

    NULL si se pagó en moneda local y el negocio no tiene tasa real conocida (no se inventa).
    """

    rate_funds: Mapped[Decimal | None] = mapped_column(RATE)
    amount_usd_real: Mapped[Decimal | None] = mapped_column(AMOUNT)

    def set_real_cost(self, legs: MoneyLegs, funds: Decimal | None) -> None:
        """Costo real en $: en dólares es el mismo monto; en moneda local, a la tasa real de sus bolívares (si la hay)."""
        from app.domain.money import real_cost_usd

        local_paid = legs.currency == legs.local_currency
        self.rate_funds = funds if local_paid and funds else None
        self.amount_usd_real = real_cost_usd(legs.amount, legs.currency, legs.local_currency, self.rate_funds)


class PortfolioAmountMixin(AmountMixin):
    """USD-native brokerage portfolio; retail retains complete reference legs."""

    valuation_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="reference", default="reference")
    amount_local: Mapped[Decimal | None] = mapped_column(AMOUNT)
    rate_official: Mapped[Decimal | None] = mapped_column(RATE)
    rate_market: Mapped[Decimal | None] = mapped_column(RATE)
    rate_applied: Mapped[Decimal | None] = mapped_column(RATE)
    rate_applied_source: Mapped[str | None] = enum_col(RateAppliedSource)

    def set_native_usd(self, amount: Decimal) -> None:
        self.valuation_mode = "brokerage_usd"
        self.amount = amount
        self.currency = "USD"
        self.amount_usd_official = self.amount_usd_market = self.amount_usd_applied = amount
        self.amount_local = self.rate_official = self.rate_market = self.rate_applied = None
        self.rate_applied_source = None


PORTFOLIO_VALUATION_CHECK = (
    "(valuation_mode = 'reference' AND amount_local IS NOT NULL AND rate_official IS NOT NULL "
    "AND rate_market IS NOT NULL AND rate_applied IS NOT NULL AND rate_applied_source IS NOT NULL) OR "
    "(valuation_mode = 'brokerage_usd' AND currency = 'USD' AND amount_local IS NULL "
    "AND rate_official IS NULL AND rate_market IS NULL AND rate_applied IS NULL AND rate_applied_source IS NULL "
    "AND amount_usd_official = amount AND amount_usd_market = amount AND amount_usd_applied = amount)"
)



class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.version, "version_id_generator": False}


# --------------------------------------------------------------------------- negocio e identidad


class Business(TimestampMixin, Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = uuid_pk()
    vertical: Mapped[str] = enum_col(BusinessVertical, nullable=False, server_default=BusinessVertical.RETAIL.value)
    name: Mapped[str | None] = mapped_column(String(160))
    what_sells: Mapped[str | None] = mapped_column(Text)
    # País y moneda local: todo lo específico del país sale de su CountryProfile (app/domain/countries.py)
    # (los defaults solo existen para filas creadas sin país; IdentityService siempre los fija desde el perfil)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default=DEFAULT_COUNTRY)
    local_currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=_DEFAULT_PROFILE.local_currency)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=_DEFAULT_PROFILE.timezone)
    # País que el dueño CONFIRMÓ en el onboarding (texto libre). Si no es soportado, country_code queda en el default
    # y registra en USD; sirve para saber a quién avisar cuando se agregue su país. NULL = negocios previos a la pregunta.
    declared_country: Mapped[str | None] = mapped_column(String(80))
    display_currency: Mapped[str] = enum_col(CurrencyView, nullable=False, server_default=CurrencyView.USD_APPLIED.value)
    # Tasa para cobrar: oficial, mercado o la propia (CustomRate purpose=charge). Define `rate_applied` de cada movimiento.
    charge_rate_mode: Mapped[str] = enum_col(ChargeRateMode, nullable=False, server_default=ChargeRateMode.OFFICIAL.value)
    # Último día (del negocio) en que el dueño confirmó su tasa propia; se le vuelve a preguntar al día siguiente.
    charge_rate_confirmed_on: Mapped[date | None] = mapped_column(Date)
    payment_methods: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    onboarding_state: Mapped[str] = enum_col(
        OnboardingState, nullable=False, server_default=OnboardingState.IN_PROGRESS.value
    )
    # Respuestas del onboarding aún sin confirmar: las columnas reales se escriben solo cuando el dueño aprueba el resumen.
    onboarding_draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # El saludo de presentación va una sola vez, aunque el negocio se haya creado antes desde el panel.
    welcomed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    @property
    def onboarded(self) -> bool:
        return self.onboarding_state == OnboardingState.DONE.value


class BusinessMember(TimestampMixin, Base):
    __tablename__ = "business_members"

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # E.164 sin '+'
    name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = enum_col(MemberRole, nullable=False, server_default=MemberRole.OWNER.value)
    abbreviation: Mapped[str | None] = mapped_column(String(12))
    # Revoca acceso sin borrar la cuenta ni su historia. El dueño puede seguir
    # liquidando movimientos y cartera de un corredor inactivo.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    business: Mapped[Business] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("business_id", "phone", name="business_phone"),
        Index(
            "uq_business_members_business_abbreviation_ci",
            "business_id",
            func.lower(abbreviation),
            unique=True,
            postgresql_where=abbreviation.is_not(None),
        ),
    )


class BrokerMovement(TimestampMixin, SoftDeleteMixin, Base):
    """Diario USD de una cuenta: movimientos de saldo y operaciones comerciales explícitas."""

    __tablename__ = "broker_movements"
    __table_args__ = (
        CheckConstraint(
            "(operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL AND monetary_basis IS NULL) OR "
            "(kind = 'operation' AND operation_amount_bs IS NOT NULL AND exchange_rate IS NOT NULL "
            "AND operation_amount_bs > 0 AND exchange_rate > 0 AND exchange_rate_source IS NOT NULL AND monetary_basis IS NOT NULL "
            "AND exchange_rate_source IN ('explicit', 'calculated') AND monetary_basis IN ('usd_bs', 'usd_rate'))",
            name="broker_movement_real_money",
        ),
        CheckConstraint("kind IN ('in', 'out', 'operation', 'customer_payout', 'adjustment')", name="broker_movement_kind"),
        CheckConstraint(
            "kind = 'adjustment' OR (kind = 'out' AND broker_member_id IS NOT NULL AND balance_delta < 0) OR (kind = 'customer_payout' AND balance_delta = 0) OR (broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))",
            name="broker_movement_sign",
        ),
        CheckConstraint(
            "deleted_at IS NOT NULL OR (kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
            "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL "
            "AND related_receivable_id IS NULL) OR "
            "(kind = 'operation' AND operation_type IN ('buy', 'sell') "
            "AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL "
            "AND operation_amount > 0 "
            "AND ((operation_type = 'buy' "
            "AND ((settlement_status = 'settled' AND delivered_amount = operation_amount "
            "AND related_receivable_id IS NULL "
            "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission + excess END) "
            "OR (related_receivable_id IS NOT NULL "
            "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 "
            "WHEN portfolio_broker_member_id IS NULL THEN commission + excess ELSE commission + excess - operation_amount END "
            "AND ((settlement_status = 'pending' AND delivered_amount = 0) "
            "OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) "
            "OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) "
            "OR (settlement_status = 'account' AND delivered_amount = 0 "
            "AND broker_member_id IS NOT NULL AND related_receivable_id IS NULL "
            "AND balance_delta = commission + excess - operation_amount))) "
            "OR (operation_type = 'sell' AND excess = 0 AND related_receivable_id IS NULL "
            "AND ((settlement_status = 'settled' AND delivered_amount = operation_amount "
            "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN sales_balance_rule = 'commission_only' THEN commission ELSE commission - operation_amount END) "
            "OR (settlement_status = 'pending' AND delivered_amount = 0 AND customer_id IS NOT NULL "
            "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 "
            "WHEN sales_balance_rule = 'commission_only' OR portfolio_broker_member_id IS NULL THEN commission ELSE commission - operation_amount END)))) "
            "AND commission IS NOT NULL AND commission_rate IS NOT NULL "
            ")",
            name="broker_movement_operation_fields",
        ),
        CheckConstraint(
            "broker_member_id IS NOT NULL OR "
            "((kind = 'operation' AND settlement_status <> 'account' AND commission = 0 AND commission_rate = 0) "
            "OR (kind = 'in' AND related_payment_id IS NOT NULL) OR kind = 'customer_payout')",
            name="broker_movement_house_scope",
        ),
        CheckConstraint(
            "related_payment_id IS NULL OR (kind = 'in' AND related_receivable_id IS NULL)",
            name="broker_movement_payment_link",
        ),
        CheckConstraint(
            "kind = 'adjustment' OR ((settlement_status = 'account' OR (kind IN ('in', 'out') AND related_payment_id IS NULL)) "
            "AND customer_id IS NULL) OR "
            "((related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR "
            "(kind = 'operation' AND settlement_status <> 'account' AND related_receivable_id IS NULL) OR "
            "(kind = 'customer_payout' AND customer_id IS NOT NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL)",
            name="broker_movement_customer_scope",
        ),
        CheckConstraint(
            "deleted_at IS NOT NULL OR "
            "(kind <> 'operation' AND received_amount IS NULL) OR "
            "(kind = 'operation' AND operation_amount IS NOT NULL AND excess IS NOT NULL AND commission_rate BETWEEN 0 AND 1 "
            "AND commission = round(operation_amount * commission_rate, 2) "
            "AND ((received_amount IS NULL AND excess = 0) OR "
            "(operation_type = 'buy' AND received_amount IS NOT NULL AND received_amount >= operation_amount "
            "AND excess = CASE WHEN broker_member_id IS NULL THEN 0 ELSE received_amount - operation_amount END)))",
            name="broker_movement_received",
        ),
        CheckConstraint(
            "(kind = 'customer_payout' AND customer_payout_amount IS NOT NULL AND customer_payout_amount > 0 AND currency = 'USD' AND balance_delta = 0) OR "
            "(kind <> 'customer_payout' AND customer_payout_amount IS NULL)",
            name="broker_movement_customer_payout",
        ),
        CheckConstraint(
            "sales_balance_rule IS NULL OR (sales_balance_rule = 'commission_only' AND kind = 'operation' AND operation_type = 'sell')",
            name="broker_movement_sales_balance_rule",
        ),
        Index("ix_broker_movements_account_date", "business_id", "broker_member_id", "business_date"),
        CheckConstraint(
            "kind <> 'out' OR (broker_member_id IS NOT NULL AND balance_delta < 0 AND currency = 'USD' "
            "AND customer_id IS NULL AND portfolio_broker_member_id IS NULL "
            "AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
            "AND received_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL "
            "AND settlement_status IS NULL AND sales_balance_rule IS NULL AND customer_payout_amount IS NULL "
            "AND related_receivable_id IS NULL AND related_payment_id IS NULL "
            "AND operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL "
            "AND monetary_basis IS NULL AND monetary_provenance IS NULL)",
            name="broker_movement_withdrawal_fields",
        ),
        CheckConstraint(
            "kind <> 'adjustment' OR (broker_member_id IS NOT NULL AND balance_delta <> 0 AND currency = 'USD' "
            "AND description IS NOT NULL AND description = 'Reconciliación de saldo' "
            "AND customer_id IS NULL AND portfolio_broker_member_id IS NULL "
            "AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
            "AND received_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL "
            "AND settlement_status IS NULL AND sales_balance_rule IS NULL AND customer_payout_amount IS NULL "
            "AND related_receivable_id IS NULL AND related_payment_id IS NULL "
            "AND operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL "
            "AND monetary_basis IS NULL AND monetary_provenance IS NULL)",
            name="broker_movement_adjustment_fields",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    broker_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"))
    portfolio_broker_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"))
    recorded_by_member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_members.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    balance_delta: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    description: Mapped[str | None] = mapped_column(String(300))
    operation_type: Mapped[str | None] = mapped_column(String(16))
    settlement_status: Mapped[str | None] = mapped_column(String(16))
    # NULL preserves the frozen accounting of sales written before ADR 0034.
    sales_balance_rule: Mapped[str | None] = mapped_column(String(24))
    customer_payout_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    operation_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    operation_amount_bs: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(28, 2))
    exchange_rate_source: Mapped[str | None] = mapped_column(String(16))
    monetary_basis: Mapped[str | None] = mapped_column(String(16))
    monetary_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    received_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    delivered_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    excess: Mapped[Decimal | None] = mapped_column(AMOUNT)
    commission: Mapped[Decimal | None] = mapped_column(AMOUNT)
    commission_rate: Mapped[Decimal | None] = mapped_column(RATE)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))
    related_receivable_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("receivables.id"), unique=True
    )
    related_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer_payments.id"), unique=True
    )

    broker: Mapped[BusinessMember | None] = relationship(foreign_keys=[broker_member_id], lazy="joined")
    recorded_by: Mapped[BusinessMember] = relationship(foreign_keys=[recorded_by_member_id], lazy="joined")
    related_payment: Mapped[CustomerPayment | None] = relationship(
        foreign_keys=[related_payment_id], lazy="joined"
    )
    customer: Mapped[Customer | None] = relationship(foreign_keys=[customer_id], lazy="joined")


class AttachmentBlob(TimestampMixin, Base):
    """Contenido deduplicado de comprobantes. El bucket continúa privado."""

    __tablename__ = "attachment_blobs"
    __table_args__ = (
        UniqueConstraint("business_id", "sha256", name="attachment_blob_business_sha256"),
        CheckConstraint("byte_size > 0 AND width > 0 AND height > 0", name="attachment_blob_dimensions"),
        CheckConstraint("status IN ('pending', 'ready')", name="attachment_blob_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    thumbnail_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(40), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")


class BrokerAttachment(TimestampMixin, SoftDeleteMixin, Base):
    """Una carga de comprobante; puede estar pendiente hasta que un comando cree/asocie el movimiento."""

    __tablename__ = "broker_attachments"
    __table_args__ = (
        CheckConstraint("purge_after IS NULL OR deleted_at IS NOT NULL", name="attachment_purge_after_delete"),
        Index(
            "uq_broker_attachments_active_movement",
            "broker_movement_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND broker_movement_id IS NOT NULL"),
        ),
        Index(
            "uq_broker_attachments_source_message",
            "source_message_id",
            unique=True,
            postgresql_where=text("source_message_id IS NOT NULL"),
        ),
        Index("ix_broker_attachments_business_created", "business_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    blob_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attachment_blobs.id", ondelete="CASCADE"), nullable=False)
    broker_movement_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("broker_movements.id", ondelete="CASCADE"))
    uploaded_by_member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_members.id"), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))
    original_filename: Mapped[str | None] = mapped_column(String(180))
    extracted_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    blob: Mapped[AttachmentBlob] = relationship(lazy="joined")
    movement: Mapped[BrokerMovement | None] = relationship(lazy="joined")
    uploaded_by: Mapped[BusinessMember] = relationship(lazy="joined")


class AttachmentObjectDeletion(Base):
    """Outbox durable para borrar objetos aunque el negocio ya no exista."""

    __tablename__ = "attachment_object_deletions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppIdentity(TimestampMixin, Base):
    """BSUID (id de usuario por negocio que manda Meta cuando oculta el número) → teléfono. Alias de canal: la identidad
    del dueño sigue siendo el teléfono. `source`: webhook | contact_request | kapso | backfill."""

    __tablename__ = "whatsapp_identities"

    bsuid: Mapped[str] = mapped_column(String(160), primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)


# --------------------------------------------------------------------------- catálogo


class Product(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_trgm", "normalized_name", postgresql_using="gin", postgresql_ops={"normalized_name": "gin_trgm_ops"}),
        Index("uq_products_business_name", "business_id", "normalized_name", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(64))
    price_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    price_currency: Mapped[str | None] = enum_col(Currency)
    last_unit_cost_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    last_unit_cost_currency: Mapped[str | None] = enum_col(Currency)
    last_cost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Customer(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index("uq_customers_business_nickname", "business_id", "nickname", unique=True,
              postgresql_where=text("deleted_at IS NULL AND nickname IS NOT NULL")),
        CheckConstraint("nullif(btrim(name), '') IS NOT NULL OR nullif(btrim(nickname), '') IS NOT NULL", name="customer_identifier"),
        CheckConstraint("nickname IS NULL OR (nickname <> '' AND nickname = upper(regexp_replace(btrim(unaccent(nickname)), '[[:space:]]+', ' ', 'g')))", name="customer_nickname_canonical"),
        Index("ix_customers_trgm", "normalized_name", postgresql_using="gin", postgresql_ops={"normalized_name": "gin_trgm_ops"}),
        CheckConstraint(
            "(classification IS NULL AND owner_broker_member_id IS NULL) OR "
            "(classification IS NOT NULL AND classification = 'global' AND owner_broker_member_id IS NULL) OR "
            "(classification IS NOT NULL AND classification = 'personal' AND owner_broker_member_id IS NOT NULL)", name="customer_classification"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(160))
    nickname: Mapped[str | None] = mapped_column(String(160))
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    classification: Mapped[str | None] = mapped_column(String(16))
    owner_broker_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"), index=True)
    owner_broker: Mapped[BusinessMember | None] = relationship(foreign_keys=[owner_broker_member_id], lazy="joined")

    @property
    def display_name(self) -> str:
        return self.name or self.nickname or "Sin identificador"


# --------------------------------------------------------------------------- movimientos


class Sale(TimestampMixin, SoftDeleteMixin, RatesMixin, Base):
    __tablename__ = "sales"
    __table_args__ = (Index("ix_sales_business_date", "business_id", "business_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payment_status: Mapped[str] = enum_col(PaymentStatus, nullable=False)
    payment_method: Mapped[str | None] = enum_col(PaymentMethod)
    notes: Mapped[str | None] = mapped_column(Text)
    served_by_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))

    total_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    total_currency: Mapped[str] = enum_col(Currency, nullable=False)
    total_local: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    total_usd_official: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    total_usd_market: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    total_usd_applied: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    # Precio antes del descuento (en la moneda del total). total_* es la venta neta = list_total - descuento.
    list_total_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0", default=Decimal(0))
    discount_local: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0", default=Decimal(0))
    discount_usd_official: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0", default=Decimal(0))
    discount_usd_market: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0", default=Decimal(0))
    discount_usd_applied: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0", default=Decimal(0))
    # Lo que el cliente pagó REALMENTE en el momento, en su moneda real. Lo que falte, si quedó debiendo, es un fiado.
    paid_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    paid_currency: Mapped[str] = enum_col(Currency, nullable=False)
    paid_local: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    paid_usd_official: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    paid_usd_market: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    paid_usd_applied: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    settlement: Mapped[str] = enum_col(SaleSettlement, nullable=False, server_default=SaleSettlement.EXACT.value)

    items: Mapped[list[SaleItem]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="SaleItem.position", lazy="selectin"
    )
    customer: Mapped[Customer | None] = relationship(lazy="joined")

    def set_total(self, legs: MoneyLegs) -> None:
        self.total_amount = legs.amount
        self.total_currency = str(legs.currency)
        self.total_local = legs.local
        self.total_usd_official = legs.usd_official
        self.total_usd_market = legs.usd_market
        self.total_usd_applied = legs.usd_applied
        self.set_rates(legs)

    def set_discount(self, legs: MoneyLegs) -> None:
        """Descuento en la moneda del total (el precio de lista menos el descuento es `total_*`)."""
        self.discount_amount = legs.amount
        self.discount_local = legs.local
        self.discount_usd_official = legs.usd_official
        self.discount_usd_market = legs.usd_market
        self.discount_usd_applied = legs.usd_applied

    def set_paid(self, legs: MoneyLegs) -> None:
        self.paid_amount = legs.amount
        self.paid_currency = str(legs.currency)
        self.paid_local = legs.local
        self.paid_usd_official = legs.usd_official
        self.paid_usd_market = legs.usd_market
        self.paid_usd_applied = legs.usd_applied


class SaleItem(Base):
    __tablename__ = "sale_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    unit_price_currency: Mapped[str] = enum_col(Currency, nullable=False)
    price_source: Mapped[str] = mapped_column(String(16), nullable=False)  # catalog | stated | payment (tomado del pago)

    item_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_currency: Mapped[str] = enum_col(Currency, nullable=False)
    item_local: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_usd_official: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_usd_market: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_usd_applied: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)

    # Costo congelado al vender (para margen). NULL = costo desconocido.
    cost_local: Mapped[Decimal | None] = mapped_column(AMOUNT)
    cost_usd_official: Mapped[Decimal | None] = mapped_column(AMOUNT)
    cost_usd_market: Mapped[Decimal | None] = mapped_column(AMOUNT)
    cost_usd_applied: Mapped[Decimal | None] = mapped_column(AMOUNT)
    cost_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unknown")

    sale: Mapped[Sale] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship(lazy="joined")


class Expense(TimestampMixin, SoftDeleteMixin, AmountMixin, RealCostMixin, Base):
    __tablename__ = "expenses"
    __table_args__ = (Index("ix_expenses_business_date", "business_id", "business_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    category: Mapped[str] = enum_col(ExpenseCategory, nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    payment_method: Mapped[str | None] = enum_col(PaymentMethod)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))


class Purchase(TimestampMixin, SoftDeleteMixin, AmountMixin, RealCostMixin, Base):
    __tablename__ = "purchases"
    __table_args__ = (Index("ix_purchases_business_date", "business_id", "business_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    payment_method: Mapped[str | None] = enum_col(PaymentMethod)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))

    items: Mapped[list[PurchaseItem]] = relationship(
        back_populates="purchase", cascade="all, delete-orphan", order_by="PurchaseItem.position", lazy="selectin"
    )


class PurchaseItem(Base):
    __tablename__ = "purchase_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    unit_cost_currency: Mapped[str] = enum_col(Currency, nullable=False)
    item_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_local: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_usd_official: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_usd_market: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    item_usd_applied: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)

    purchase: Mapped[Purchase] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship(lazy="joined")


class Receivable(TimestampMixin, SoftDeleteMixin, PortfolioAmountMixin, Base):
    """Un fiado. Su saldo se deriva: amount - SUM(asignaciones vigentes). No hay columna `balance`."""

    __tablename__ = "receivables"
    __table_args__ = (Index("ix_receivables_customer", "business_id", "customer_id"),
                      CheckConstraint(PORTFOLIO_VALUATION_CHECK, name="receivable_valuation"))

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    broker_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"), index=True)
    sale_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sales.id"), unique=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))

    customer: Mapped[Customer] = relationship(lazy="joined")


class CustomerBalanceAdjustment(TimestampMixin, SoftDeleteMixin, Base):
    """Signed USD reconciliation, independent of commercial obligations and FIFO."""

    __tablename__ = "customer_balance_adjustments"
    __table_args__ = (
        Index("ix_customer_balance_adjustments_account_date", "business_id", "customer_id",
              "portfolio_broker_member_id", "business_date"),
        CheckConstraint("amount_delta <> 0", name="customer_adjustment_nonzero"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    portfolio_broker_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"))
    amount_delta: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    recorded_by_member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_members.id"), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))
    customer: Mapped[Customer] = relationship(lazy="joined")
    recorded_by: Mapped[BusinessMember] = relationship(foreign_keys=[recorded_by_member_id], lazy="joined")


class CustomerPayment(TimestampMixin, SoftDeleteMixin, PortfolioAmountMixin, Base):
    """Un abono de un cliente. Se reparte FIFO entre sus fiados (PaymentAllocation)."""

    __tablename__ = "customer_payments"
    __table_args__ = (Index("ix_customer_payments_business_date", "business_id", "business_date"),
                      CheckConstraint(PORTFOLIO_VALUATION_CHECK, name="payment_valuation"))

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    broker_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_members.id"), index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payment_method: Mapped[str | None] = enum_col(PaymentMethod)
    unapplied_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, server_default="0")
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))

    customer: Mapped[Customer] = relationship(lazy="joined")
    allocations: Mapped[list[PaymentAllocation]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", lazy="selectin"
    )


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"

    id: Mapped[uuid.UUID] = uuid_pk()
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_payments.id", ondelete="CASCADE"), nullable=False, index=True)
    receivable_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receivables.id"), nullable=False, index=True)
    applied_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)  # en la moneda del fiado

    payment: Mapped[CustomerPayment] = relationship(back_populates="allocations")


# --------------------------------------------------------------------------- tasas y caja


class ExchangeRate(Base):
    """Tasas automáticas por moneda local: `rate` = unidades de `currency` por 1 USD."""

    __tablename__ = "exchange_rates"
    __table_args__ = (Index("ix_exchange_rates_currency_kind_fetched", "currency", "kind", "fetched_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = enum_col(RateKind, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CustomRate(Base):
    """Tasa fijada por el negocio. `purpose`: charge = tasa para cobrar; funds = tasa real de sus bolívares.

    La vigente de cada propósito es la última con `cleared_at IS NULL`.
    """

    __tablename__ = "custom_rates"
    __table_args__ = (Index("ix_custom_rates_business_purpose_set", "business_id", "purpose", "set_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose: Mapped[str] = enum_col(RatePurpose, nullable=False, server_default=RatePurpose.CHARGE.value)
    source: Mapped[str] = enum_col(CustomRateSource, nullable=False, server_default=CustomRateSource.MANUAL.value)
    exchange_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("currency_exchanges.id", ondelete="SET NULL"))
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CurrencyExchange(TimestampMixin, SoftDeleteMixin, Base):
    """Cambio de moneda: el dinero cambia de forma (salen $, entran Bs o al revés). No es venta, gasto ni ingreso.

    `rate` es la tasa real del cambio (moneda local por 1 USD) y alimenta la tasa real de los bolívares del negocio.
    """

    __tablename__ = "currency_exchanges"
    __table_args__ = (Index("ix_currency_exchanges_business_date", "business_id", "business_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    out_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    out_currency: Mapped[str] = enum_col(Currency, nullable=False)
    out_method: Mapped[str | None] = enum_col(PaymentMethod)
    in_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    in_currency: Mapped[str] = enum_col(Currency, nullable=False)
    in_method: Mapped[str | None] = enum_col(PaymentMethod)
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_official: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    rate_market: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))


class CashSession(Base):
    __tablename__ = "cash_sessions"
    __table_args__ = (UniqueConstraint("business_id", "business_date", name="business_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_by_actor_type: Mapped[str] = enum_col(ActorType, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    times_closed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


# --------------------------------------------------------------------------- memoria


class BusinessNote(Base):
    __tablename__ = "business_notes"
    __table_args__ = (
        Index("ix_business_notes_fts", text("to_tsvector('spanish', text)"), postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = enum_col(NoteKind, nullable=False)
    subject_entity_type: Mapped[str | None] = enum_col(EntityType)
    subject_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    subject_name: Mapped[str | None] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))
    created_by_actor_type: Mapped[str] = enum_col(ActorType, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}


# --------------------------------------------------------------------------- auditoría


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type: Mapped[str] = enum_col(EntityType, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = enum_col(AuditAction, nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor_type: Mapped[str] = enum_col(ActorType, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    command: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = created_at_col()


# --------------------------------------------------------------------------- conversación y observabilidad


class BrokerDraftExecution(Base):
    """Committed result of a conversational draft; survives Redis cleanup failures."""

    __tablename__ = "broker_draft_executions"
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_members.id"), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (
        Index("ix_inbound_fts", text("to_tsvector('spanish', coalesce(body, '') || ' ' || coalesce(transcript, ''))"), postgresql_using="gin"),
        Index("ix_inbound_business_received", "business_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    business_phone_number_id: Mapped[str | None] = mapped_column(String(160))  # ID del número de Bolos receptor
    provider_message_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    # NULL = BSUID desconocido: el mensaje queda en espera hasta que el usuario comparta su contacto
    from_phone: Mapped[str | None] = mapped_column(String(32))
    from_bsuid: Mapped[str | None] = mapped_column(String(160), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(160))
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    media_mime_type: Mapped[str | None] = mapped_column(String(80))
    reply_to_provider_message_id: Mapped[str | None] = mapped_column(String(160), index=True)
    # id del botón o fila de lista que tocó (formato propio: c:/o:/u:/onb:, ver app/application/choices.py)
    interactive_reply_id: Mapped[str | None] = mapped_column(String(256))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = created_at_col()


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    to_phone: Mapped[str | None] = mapped_column(String(32))  # NULL solo en la solicitud de contacto (sale al BSUID)
    to_bsuid: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    business_phone_number_id: Mapped[str | None] = mapped_column(String(160))  # ID del número de Bolos emisor
    provider_message_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # opciones tocables que acompañaron al mensaje ({body, list_button, choices: [{id, title, description}]})
    interactive: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="sent")
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = created_at_col()


class MessageEntityLink(Base):
    """Qué registros creó, editó o mostró cada mensaje. Base de las correcciones por mensaje citado."""

    __tablename__ = "message_entity_links"
    __table_args__ = (
        Index("ix_links_message", "message_kind", "message_id"),
        UniqueConstraint("message_kind", "message_id", "entity_type", "entity_id", "relation", name="link"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    message_kind: Mapped[str] = enum_col(MessageKind, nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = enum_col(EntityType, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relation: Mapped[str] = enum_col(LinkRelation, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class AgentTurn(Base):
    __tablename__ = "agent_turns"
    __table_args__ = (Index("ix_agent_turns_business_created", "business_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inbound_messages.id", ondelete="SET NULL"))
    lane: Mapped[str | None] = mapped_column(String(16))
    route_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = enum_col(TurnStatus, nullable=False, server_default=TurnStatus.RUNNING.value)
    stages: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    reply_text: Mapped[str | None] = mapped_column(Text)
    model_text: Mapped[str | None] = mapped_column(Text)
    grounding_ok: Mapped[bool | None] = mapped_column(Boolean)
    grounding_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    provider: Mapped[str | None] = mapped_column(String(60))
    request: Mapped[Any | None] = mapped_column(JSONB)
    response: Mapped[Any | None] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    thinking_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    finish_reason: Mapped[str | None] = mapped_column(String(40))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_col()


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(120))
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    injected_args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[Any | None] = mapped_column(JSONB)
    outcome: Mapped[str] = enum_col(ToolOutcome, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = created_at_col()
