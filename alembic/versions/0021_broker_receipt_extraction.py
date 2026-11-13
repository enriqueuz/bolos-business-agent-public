"""Persist structured receipt extraction evidence."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = depends_on = None


def upgrade():
    op.add_column("broker_attachments", sa.Column("extracted_data", JSONB()))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM broker_attachments WHERE extracted_data IS NOT NULL)")).scalar():
        raise RuntimeError("Cannot discard receipt extraction evidence.")
    op.drop_column("broker_attachments", "extracted_data")
