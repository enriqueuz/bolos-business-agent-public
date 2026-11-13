"""mensajes con botones y listas de WhatsApp

Revision ID: 0007
Revises: 0006

inbound_messages.interactive_reply_id: el id (propio) del botón o fila que tocó el dueño.
outbound_messages.interactive: las opciones que acompañaron a un mensaje saliente.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("inbound_messages", sa.Column("interactive_reply_id", sa.String(256), nullable=True))
    op.add_column("outbound_messages", sa.Column("interactive", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbound_messages", "interactive")
    op.drop_column("inbound_messages", "interactive_reply_id")
