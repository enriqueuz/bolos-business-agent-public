"""Definiciones de solo lectura de las vistas SQL (money_movements: migración 0008). No se crean desde aquí."""

from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, Integer, MetaData, Numeric, String, Table
from sqlalchemy.dialects.postgresql import UUID

view_metadata = MetaData()

money_movements = Table(
    "money_movements",
    view_metadata,
    Column("kind", String),
    Column("id", UUID(as_uuid=True)),
    Column("business_id", UUID(as_uuid=True)),
    Column("business_date", Date),
    Column("occurred_at", DateTime(timezone=True)),
    Column("description", String),
    Column("customer_id", UUID(as_uuid=True)),
    Column("customer_name", String),
    Column("payment_method", String),
    Column("payment_status", String),
    Column("category", String),
    Column("direction", Integer),
    Column("amount", Numeric),
    Column("currency", String),
    Column("amount_local", Numeric),
    Column("amount_usd_official", Numeric),
    Column("amount_usd_market", Numeric),
    Column("amount_usd_applied", Numeric),
    # cashflow_*: el dinero que REALMENTE entró o salió (por cualquier método; no es "efectivo"), en su moneda real
    Column("cashflow_amount", Numeric),
    Column("cashflow_currency", String),
    Column("cashflow_local", Numeric),
    Column("cashflow_usd_official", Numeric),
    Column("cashflow_usd_market", Numeric),
    Column("cashflow_usd_applied", Numeric),
    Column("rate_official", Numeric),
    Column("rate_market", Numeric),
    Column("rate_applied", Numeric),
    Column("rate_applied_source", String),
    Column("version", Integer),
    Column("created_at", DateTime(timezone=True)),
)

receivable_balances = Table(
    "receivable_balances",
    view_metadata,
    Column("receivable_id", UUID(as_uuid=True)),
    Column("business_id", UUID(as_uuid=True)),
    Column("customer_id", UUID(as_uuid=True)),
    Column("sale_id", UUID(as_uuid=True)),
    Column("business_date", Date),
    Column("currency", String),
    Column("original_amount", Numeric),
    Column("paid_amount", Numeric),
    Column("remaining_amount", Numeric),
)
