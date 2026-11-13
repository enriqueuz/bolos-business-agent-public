"""Backfill de la migración 0008: los reportes de cada negocio dan las mismas cifras que antes.

Baja el esquema a 0007, siembra filas con SQL crudo (como estaban en producción), vuelve a subir a head y revisa.
"""

from __future__ import annotations

import os
import subprocess
from decimal import Decimal

from sqlalchemy import text

BIZ_OFFICIAL = "00000000-0000-0000-0000-00000000000a"
BIZ_CUSTOM = "00000000-0000-0000-0000-00000000000b"
SALE_OFFICIAL = "00000000-0000-0000-0000-0000000000a1"
SALE_CUSTOM = "00000000-0000-0000-0000-0000000000b1"
ITEM_CUSTOM = "00000000-0000-0000-0000-0000000000b2"
EXPENSE_CUSTOM = "00000000-0000-0000-0000-0000000000b3"


def _alembic(*args: str) -> None:
    subprocess.run(["alembic", *args], check=True, env={**os.environ}, capture_output=True)


SEED = f"""
INSERT INTO exchange_rates (currency, kind, provider, rate, fetched_at) VALUES
  ('VES', 'official', 'test', 800, '2026-09-01T10:00:00Z'), ('VES', 'market', 'test', 900, '2026-09-01T10:00:00Z');
INSERT INTO businesses (id, name, display_currency, payment_methods, onboarding_draft) VALUES
  ('{BIZ_OFFICIAL}', 'BCV', 'usd_official', '["cash_usd", "mobile_payment", "cash_local"]', '{{}}'),
  ('{BIZ_CUSTOM}', 'Propia', 'usd_market', '["cash_local"]', '{{"payment_methods": ["cash_usd", "zelle"]}}');
INSERT INTO custom_rates (id, business_id, rate, set_at) VALUES
  (gen_random_uuid(), '{BIZ_CUSTOM}', 950, '2026-09-01T09:00:00Z');
-- venta a negocio BCV: $10 cobrados completos, en efectivo $
INSERT INTO sales (id, business_id, business_date, occurred_at, payment_status, payment_method, total_amount, total_currency,
                   total_local, total_usd_official, total_usd_market, paid_amount, rate_official, rate_market, rate_market_source,
                   created_at)
VALUES ('{SALE_OFFICIAL}', '{BIZ_OFFICIAL}', '2026-09-02', '2026-09-02T15:00:00Z', 'paid', 'cash_usd', 10, 'USD',
        9000, 10, 10, 10, 800, 900, 'auto', '2026-09-02T15:00:00Z');
-- venta en Bs a negocio con tasa propia 950: 9.500 Bs, pagó 4.750 (la mitad)
INSERT INTO sales (id, business_id, business_date, occurred_at, payment_status, payment_method, total_amount, total_currency,
                   total_local, total_usd_official, total_usd_market, paid_amount, rate_official, rate_market, rate_market_source,
                   created_at)
VALUES ('{SALE_CUSTOM}', '{BIZ_CUSTOM}', '2026-09-02', '2026-09-02T15:00:00Z', 'partial', 'cash_local', 9500, 'VES',
        9500, 11.875, 10, 4750, 800, 950, 'custom', '2026-09-02T15:00:00Z');
INSERT INTO sale_items (id, sale_id, position, description, quantity, unit_price_amount, unit_price_currency, price_source,
                        line_amount, line_currency, line_local, line_usd_official, line_usd_market)
VALUES ('{ITEM_CUSTOM}', '{SALE_CUSTOM}', 1, 'Caucho', 1, 9500, 'VES', 'stated', 9500, 'VES', 9500, 11.875, 10);
INSERT INTO expenses (id, business_id, business_date, occurred_at, category, description, payment_method, amount, currency,
                      amount_local, amount_usd_official, amount_usd_market, rate_official, rate_market, rate_market_source,
                      created_at)
VALUES ('{EXPENSE_CUSTOM}', '{BIZ_CUSTOM}', '2026-09-02', '2026-09-02T15:00:00Z', 'food', 'almuerzo', 'cash_usd', 5, 'USD',
        4750, 5, 5, 800, 950, 'custom', '2026-09-02T15:00:00Z');
"""


async def test_backfill_keeps_each_business_numbers(engine):
    _alembic("downgrade", "0007")
    try:
        async with engine.begin() as conn:
            for statement in SEED.split(";\n"):
                if statement.strip():
                    await conn.execute(text(statement))
        _alembic("upgrade", "head")

        async with engine.connect() as conn:
            async def one(sql: str):
                return (await conn.execute(text(sql))).mappings().one()

            modes = dict((await conn.execute(text("SELECT name, charge_rate_mode FROM businesses"))).all())
            assert modes == {"BCV": "official", "Propia": "custom"}

            # negocio BCV: lo que veía en $ (pata oficial) es ahora su pata aplicada; métodos sin duplicar "cash"
            bcv = await one(f"SELECT * FROM sales WHERE id = '{SALE_OFFICIAL}'")
            assert (bcv["rate_applied"], bcv["rate_applied_source"], bcv["total_usd_applied"]) == (800, "official", 10)
            assert (bcv["payment_method"], bcv["paid_currency"], bcv["paid_usd_applied"]) == ("cash", "USD", 10)
            biz = await one(f"SELECT display_currency, payment_methods FROM businesses WHERE id = '{BIZ_OFFICIAL}'")
            assert biz["display_currency"] == "usd_applied" and biz["payment_methods"] == ["cash", "mobile_payment"]

            # negocio con tasa propia: la aplicada es la propia (950) y la pata de mercado vuelve a la automática (900)
            own = await one(f"SELECT * FROM sales WHERE id = '{SALE_CUSTOM}'")
            assert (own["rate_applied"], own["rate_applied_source"], own["total_usd_applied"]) == (950, "custom", 10)
            assert own["rate_market"] == 900 and own["total_usd_market"] == Decimal("10.5556")
            assert (own["paid_local"], own["paid_usd_applied"], own["settlement"]) == (4750, 5, "debt")
            item = await one(f"SELECT * FROM sale_items WHERE id = '{ITEM_CUSTOM}'")
            assert item["item_usd_applied"] == 10 and item["item_usd_market"] == Decimal("10.5556")
            expense = await one(f"SELECT * FROM expenses WHERE id = '{EXPENSE_CUSTOM}'")
            assert (expense["amount_usd_applied"], expense["amount_usd_market"], expense["rate_market"]) == (5, 5, 900)
            draft = await one(f"SELECT onboarding_draft FROM businesses WHERE id = '{BIZ_CUSTOM}'")
            assert draft["onboarding_draft"]["payment_methods"] == ["cash", "zelle"]
    finally:
        _alembic("upgrade", "head")


async def test_downgrade_and_upgrade_again_are_clean(engine):
    _alembic("downgrade", "0007")
    _alembic("upgrade", "head")
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert version == "0029"
