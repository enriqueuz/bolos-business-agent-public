"""Schema-only migration preserves financial history and refuses a lossy downgrade."""

from sqlalchemy import text

from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountWithdrawal,
)
from tests.integration.test_brokerage import _brokerage, _ctx
from tests.integration.test_migration_0023 import alembic


async def test_direct_movement_migration_preserves_existing_rows(bus, session_factory, engine):
    business, _, broker, _ = await _brokerage(session_factory)
    result = await bus.dispatch(RecordBrokerAccountPayment(amount="500"), _ctx(business, broker))
    async with engine.connect() as connection:
        before = await connection.scalar(text("SELECT to_jsonb(m) FROM broker_movements m WHERE id=:id"),
                                         {"id": result.affected[0].entity_id})
    try:
        alembic("downgrade", "0028")
        alembic("upgrade", "head")
        async with engine.connect() as connection:
            after = await connection.scalar(text("SELECT to_jsonb(m) FROM broker_movements m WHERE id=:id"),
                                            {"id": result.affected[0].entity_id})
            assert before == after
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0029"
    finally:
        alembic("upgrade", "head")


async def test_downgrade_preserves_deleted_withdrawal_history(bus, session_factory, engine):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    result = await bus.dispatch(RecordBrokerAccountWithdrawal(amount="500"), ctx)
    await bus.dispatch(DeleteBrokerMovement(movement_id=result.affected[0].entity_id, expected_version=1), ctx, confirmed=True)
    attempt = alembic("downgrade", "0028", check=False)
    assert attempt.returncode != 0 and "direct withdrawals exist" in attempt.stderr
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0029"
        assert await connection.scalar(text("SELECT balance_delta FROM broker_movements WHERE id=:id"),
                                       {"id": result.affected[0].entity_id}) == -500
