"""identidades de WhatsApp por BSUID (usernames de WhatsApp)

Revision ID: 0009
Revises: 0008

Meta oculta el número de quien adoptó un username y no tuvo contacto con el negocio en 30 días; en su lugar manda
un BSUID (id de usuario por negocio). El teléfono sigue siendo la identidad del dueño; el BSUID es un alias de canal.

- Nueva tabla `whatsapp_identities` (BSUID → teléfono).
- `inbound_messages.from_bsuid` y `from_phone` nullable: un mensaje de un BSUID desconocido queda en espera hasta que
  el usuario comparta su contacto.
- `outbound_messages.to_bsuid` y `to_phone` nullable: la solicitud de contacto sale al BSUID.

Backfill: los mensajes de Kapso ya guardados que traían teléfono y BSUID juntos.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_identities",
        sa.Column("bsuid", sa.String(160), primary_key=True),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_whatsapp_identities_phone", "whatsapp_identities", ["phone"])
    op.add_column("inbound_messages", sa.Column("from_bsuid", sa.String(160), nullable=True))
    op.create_index("ix_inbound_messages_from_bsuid", "inbound_messages", ["from_bsuid"])
    op.alter_column("inbound_messages", "from_phone", existing_type=sa.String(32), nullable=True)
    op.add_column("outbound_messages", sa.Column("to_bsuid", sa.String(160), nullable=True))
    op.alter_column("outbound_messages", "to_phone", existing_type=sa.String(32), nullable=True)

    # Kapso v2: message.from_user_id o conversation.business_scoped_user_id. El más reciente por BSUID.
    op.execute("""
        INSERT INTO whatsapp_identities (bsuid, phone, source)
        SELECT DISTINCT ON (bsuid) bsuid, from_phone, 'backfill'
        FROM (
            SELECT coalesce(raw->'message'->>'from_user_id', raw->'conversation'->>'business_scoped_user_id') AS bsuid,
                   from_phone, received_at
            FROM inbound_messages
            WHERE provider = 'kapso' AND from_phone IS NOT NULL AND from_phone <> ''
        ) found
        WHERE bsuid IS NOT NULL AND bsuid <> ''
        ORDER BY bsuid, received_at DESC
        ON CONFLICT (bsuid) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("UPDATE outbound_messages SET to_phone = '' WHERE to_phone IS NULL")
    op.alter_column("outbound_messages", "to_phone", existing_type=sa.String(32), nullable=False)
    op.drop_column("outbound_messages", "to_bsuid")
    op.execute("UPDATE inbound_messages SET from_phone = '' WHERE from_phone IS NULL")
    op.alter_column("inbound_messages", "from_phone", existing_type=sa.String(32), nullable=False)
    op.drop_index("ix_inbound_messages_from_bsuid", table_name="inbound_messages")
    op.drop_column("inbound_messages", "from_bsuid")
    op.drop_table("whatsapp_identities")
