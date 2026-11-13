"""Fixtures compartidas.

Los tests corren contra Postgres y Redis REALES (servicios db-test/redis-test del compose). No se usa
`patch()` sobre módulos: las dependencias se construyen explícitamente (ver app/container.py), así que
mover un archivo nunca rompe un test.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.bus import CommandBus, CommandDeps, load_all_commands
from app.application.context import Actor, ExecContext
from app.application.rates import RateService
from app.db.models import Base, Business, BusinessMember, ExchangeRate
from app.db.session import make_engine, make_session_factory
from app.domain.enums import ActorType, ChargeRateMode, CurrencyView, OnboardingState, RateKind
from app.logging import configure_logging

load_all_commands()
configure_logging("WARNING", json=False)

OFFICIAL = Decimal("800")
MARKET = Decimal("900")
NOW = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)  # 14:00 en Caracas (lunes)


def pytest_configure(config: pytest.Config) -> None:
    assert os.environ.get("APP_ENV") == "test", "Corre los tests con: docker compose run --rm test"


@pytest.fixture(scope="session")
def migrated_db() -> str:
    url = os.environ["DATABASE_URL"]
    sync_url = url.replace("+asyncpg", "")
    subprocess.run(
        ["psql", sync_url.replace("postgresql://", "postgresql://"), "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
        check=False, capture_output=True,
    )
    subprocess.run(["alembic", "upgrade", "head"], check=True, env={**os.environ})
    return url


@pytest_asyncio.fixture(scope="session")
async def engine(migrated_db: str):
    eng = make_engine(migrated_db)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="session")
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


@pytest_asyncio.fixture(autouse=True)
async def clean_db(engine) -> AsyncIterator[None]:
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def rates(session_factory) -> RateService:
    async with session_factory() as s, s.begin():
        s.add(ExchangeRate(currency="VES", kind=RateKind.OFFICIAL.value, provider="test", rate=OFFICIAL, rate_date=NOW))
        s.add(ExchangeRate(currency="VES", kind=RateKind.MARKET.value, provider="test", rate=MARKET, rate_date=NOW))
    return RateService(session_factory)


@pytest_asyncio.fixture
async def bus(session_factory, rates) -> CommandBus:
    return CommandBus(session_factory, CommandDeps(rates=rates))


@pytest_asyncio.fixture
async def business(session_factory) -> Business:
    async with session_factory() as s, s.begin():
        b = Business(name="Repuestos El Rayo", what_sells="repuestos de moto", onboarding_state=OnboardingState.DONE.value, welcomed=True,
                     display_currency=CurrencyView.USD_APPLIED.value, charge_rate_mode=ChargeRateMode.MARKET.value,
                     payment_methods=["cash", "mobile_payment"])
        s.add(b)
        await s.flush()
        s.add(BusinessMember(business_id=b.id, phone="584140000001", name="Enrique"))
    return b


@pytest.fixture
def agent_ctx(business) -> ExecContext:
    return ExecContext(business_id=business.id, actor=Actor(ActorType.AGENT), now=NOW, turn_id=uuid.uuid4())


@pytest.fixture
def panel_ctx(business) -> ExecContext:
    return ExecContext(business_id=business.id, actor=Actor(ActorType.PANEL), now=NOW)
