"""Backfill de la migración 0009: los mensajes de Kapso ya guardados que traían teléfono y BSUID enlazan el BSUID.

Baja el esquema a 0008, siembra mensajes con SQL crudo (como estaban en producción), vuelve a subir a head y revisa.
"""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import text


def _alembic(*args: str) -> None:
    subprocess.run(["alembic", *args], check=True, env={**os.environ}, capture_output=True)


SEED = """
INSERT INTO inbound_messages (id, provider, provider_message_id, from_phone, type, raw, received_at) VALUES
  -- el BSUID en el mensaje (Kapso v2): gana el más reciente
  (gen_random_uuid(), 'kapso', 'wamid.1', '584140000001', 'text', '{"message": {"from_user_id": "VE.111"}}', '2026-09-01T10:00:00Z'),
  (gen_random_uuid(), 'kapso', 'wamid.2', '584140000002', 'text', '{"message": {"from_user_id": "VE.111"}}', '2026-09-02T10:00:00Z'),
  -- solo en la conversación
  (gen_random_uuid(), 'kapso', 'wamid.3', '584140000003', 'text', '{"conversation": {"business_scoped_user_id": "VE.333"}}', '2026-09-01T10:00:00Z'),
  -- sin BSUID, o de otro proveedor: no enlazan nada
  (gen_random_uuid(), 'kapso', 'wamid.4', '584140000004', 'text', '{"message": {"from": "584140000004"}}', '2026-09-01T10:00:00Z'),
  (gen_random_uuid(), 'console', 'in-5', '584140000005', 'text', '{"message": {"from_user_id": "VE.555"}}', '2026-09-01T10:00:00Z')
"""


async def test_backfill_links_bsuids_already_seen(engine):
    _alembic("downgrade", "0008")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(SEED))
        _alembic("upgrade", "head")
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT bsuid, phone, source FROM whatsapp_identities ORDER BY bsuid"))).all()
        assert [tuple(r) for r in rows] == [("VE.111", "584140000002", "backfill"), ("VE.333", "584140000003", "backfill")]
    finally:
        _alembic("upgrade", "head")
