"""Protecciones de reversión para la cuenta Casa implícita."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db.models import BrokerMovement, Business, BusinessMember
from app.domain.enums import BusinessVertical, MemberRole, OnboardingState


def _alembic(*args: str) -> None:
    subprocess.run(
        ["alembic", *args],
        check=True,
        env={**os.environ},
        capture_output=True,
        text=True,
    )


async def test_downgrade_refuses_to_drop_house_movements(engine, session_factory) -> None:
    async with session_factory() as session, session.begin():
        business = Business(
            name="Casa con historia",
            vertical=BusinessVertical.BROKERAGE.value,
            onboarding_state=OnboardingState.DONE.value,
        )
        session.add(business)
        await session.flush()
        owner = BusinessMember(
            business_id=business.id,
            phone="584140000099",
            name="Owner",
            role=MemberRole.OWNER.value,
        )
        session.add(owner)
        await session.flush()
        session.add(BrokerMovement(
            business_id=business.id,
            broker_member_id=None,
            recorded_by_member_id=owner.id,
            kind="operation",
            balance_delta=Decimal("0"),
            currency="USD",
            operation_type="buy",
            settlement_status="settled",
            operation_amount=Decimal("10"),
            delivered_amount=Decimal("10"),
            excess=Decimal("0"),
            commission=Decimal("0"),
            commission_rate=Decimal("0"),
            business_date=date(2026, 10, 9),
            occurred_at=datetime(2026, 10, 9, 16, tzinfo=UTC),
        ))

    with pytest.raises(subprocess.CalledProcessError) as error:
        _alembic("downgrade", "0016")
    assert "existen movimientos de Casa" in error.value.stderr
    async with engine.connect() as connection:
        version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert version == "0029"


async def test_upgrade_from_0016_makes_broker_member_nullable(engine) -> None:
    _alembic("downgrade", "0016")
    async with engine.connect() as connection:
        version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        nullable = (await connection.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'broker_movements' AND column_name = 'broker_member_id'"
        ))).scalar_one()
    assert version == "0016"
    assert nullable == "NO"

    _alembic("upgrade", "head")
    async with engine.connect() as connection:
        version = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        nullable = (await connection.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'broker_movements' AND column_name = 'broker_member_id'"
        ))).scalar_one()
    assert version == "0029"
    assert nullable == "YES"
