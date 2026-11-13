"""Messaging-account migration preserves legacy messages and requires no backfill."""

import uuid

from sqlalchemy import text

from tests.integration.test_migration_0023 import alembic


async def test_messaging_accounts_preserve_legacy_rows(engine):
    inbound_id, outbound_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        alembic("downgrade", "0025")
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO inbound_messages (id, provider, provider_message_id, from_phone, type, body) "
                "VALUES (:id, 'kapso', 'legacy-in', '584140000001', 'text', 'mensaje histórico')"
            ), {"id": inbound_id})
            await connection.execute(text(
                "INSERT INTO outbound_messages (id, provider, provider_message_id, to_phone, body) "
                "VALUES (:id, 'kapso', 'legacy-out', '584140000001', 'respuesta histórica')"
            ), {"id": outbound_id})
        alembic("upgrade", "head")
        async with engine.connect() as connection:
            for table, row_id in (("inbound_messages", inbound_id), ("outbound_messages", outbound_id)):
                row = (await connection.execute(text(f"SELECT business_phone_number_id, body FROM {table} WHERE id=:id"),
                                                {"id": row_id})).one()
                assert row.business_phone_number_id is None and "históric" in row.body
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0029"
    finally:
        alembic("upgrade", "head")
