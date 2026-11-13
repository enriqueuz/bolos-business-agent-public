"""Persist draft execution results atomically with the financial command."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "broker_draft_executions",
        sa.Column("business_id", sa.UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation_id", sa.UUID(), primary_key=True),
        sa.Column("actor_id", sa.UUID(), sa.ForeignKey("business_members.id"), nullable=False),
        sa.Column("command_kind", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table("broker_draft_executions")
