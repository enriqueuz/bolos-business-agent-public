"""Persist the receiving/sending account for simultaneous WhatsApp numbers."""

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = depends_on = None


def upgrade():
    op.add_column("inbound_messages", sa.Column("business_phone_number_id", sa.String(160), nullable=True))
    op.add_column("outbound_messages", sa.Column("business_phone_number_id", sa.String(160), nullable=True))


def downgrade():
    op.drop_column("outbound_messages", "business_phone_number_id")
    op.drop_column("inbound_messages", "business_phone_number_id")
