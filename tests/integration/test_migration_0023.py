"""Customer identity migration preserves legacy records and refuses lossy rollback."""

import os
import subprocess
from uuid import uuid4

from sqlalchemy import text


def alembic(*args, check=True):
    return subprocess.run(["alembic", *args], env=dict(os.environ), check=check, capture_output=True, text=True)


async def test_legacy_customer_keeps_identity_and_no_backfill(engine):
    alembic("downgrade", "0022")
    bid, cid = uuid4(), uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO businesses (id, name) VALUES (:bid, 'Legacy')"), {"bid": bid})
            await conn.execute(text("INSERT INTO customers (id, business_id, name, normalized_name) VALUES (:cid, :bid, 'Julia Example', 'julana miranda')"), {"bid": bid, "cid": cid})
        alembic("upgrade", "head")
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT id, name, nickname FROM customers WHERE id=:cid"), {"cid": cid})).one()
            assert tuple(row) == (cid, "Julia Example", None)
    finally:
        alembic("upgrade", "head")


async def test_rollback_refuses_to_drop_assigned_nicknames(engine):
    bid, cid = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO businesses (id, name) VALUES (:bid, 'Nicknames')"), {"bid": bid})
        await conn.execute(text("INSERT INTO customers (id, business_id, name, normalized_name, nickname) VALUES (:cid, :bid, NULL, '', 'JUL')"), {"bid": bid, "cid": cid})
    attempt = alembic("downgrade", "0022", check=False)
    assert attempt.returncode != 0 and "Cannot discard customer nicknames" in attempt.stderr
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT nickname FROM customers WHERE id=:cid"), {"cid": cid})).scalar_one() == "JUL"
