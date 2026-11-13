"""Account migration roundtrip and protected rollback for real customer events."""
from sqlalchemy import text

from app.application.commands.brokerage import RecordBrokerOperation
from tests.integration.test_brokerage import _brokerage, _ctx
from tests.integration.test_migration_0023 import alembic


async def test_empty_unified_accounts_migration_roundtrip(engine):
    try:
        alembic("downgrade", "0023")
        alembic("upgrade", "head")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0029"
    finally:
        alembic("upgrade", "head")


async def test_rollback_refuses_to_discard_pending_sales(bus, session_factory):
    business, owner, _, _ = await _brokerage(session_factory)
    result = await bus.dispatch(RecordBrokerOperation(operation_type="sell", settlement="pending", operation_amount="100",
        exchange_rate="1000", customer_name="Hum", customer_classification="global"), _ctx(business, owner))
    async with session_factory() as db, db.begin():
        await db.execute(text("UPDATE broker_movements SET sales_balance_rule=NULL WHERE id=:id"),
                         {"id": result.affected[0].entity_id})
    attempt = alembic("downgrade", "0023", check=False)
    assert attempt.returncode != 0
    assert "Cannot discard customer payouts or pending sales" in attempt.stderr
