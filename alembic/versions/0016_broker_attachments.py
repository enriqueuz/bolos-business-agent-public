"""Persist broker movement attachments.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachment_blobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("original_key", sa.Text(), nullable=False),
        sa.Column("thumbnail_key", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(40), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="ready", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("byte_size > 0 AND width > 0 AND height > 0", name=op.f("ck_attachment_blobs_attachment_blob_dimensions")),
        sa.CheckConstraint("status IN ('pending', 'ready')", name=op.f("ck_attachment_blobs_attachment_blob_status")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE", name=op.f("fk_attachment_blobs_business_id_businesses")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachment_blobs")),
        sa.UniqueConstraint("business_id", "sha256", name="attachment_blob_business_sha256"),
        sa.UniqueConstraint("original_key", name=op.f("uq_attachment_blobs_original_key")),
        sa.UniqueConstraint("thumbnail_key", name=op.f("uq_attachment_blobs_thumbnail_key")),
    )
    op.create_table(
        "broker_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blob_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("broker_movement_id", postgresql.UUID(as_uuid=True)),
        sa.Column("uploaded_by_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("original_filename", sa.String(180)),
        sa.Column("attached_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("purge_after IS NULL OR deleted_at IS NOT NULL", name=op.f("ck_broker_attachments_attachment_purge_after_delete")),
        sa.ForeignKeyConstraint(["blob_id"], ["attachment_blobs.id"], ondelete="CASCADE", name=op.f("fk_broker_attachments_blob_id_attachment_blobs")),
        sa.ForeignKeyConstraint(["broker_movement_id"], ["broker_movements.id"], ondelete="CASCADE", name=op.f("fk_broker_attachments_broker_movement_id_broker_movements")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE", name=op.f("fk_broker_attachments_business_id_businesses")),
        sa.ForeignKeyConstraint(["source_message_id"], ["inbound_messages.id"], ondelete="SET NULL", name=op.f("fk_broker_attachments_source_message_id_inbound_messages")),
        sa.ForeignKeyConstraint(["uploaded_by_member_id"], ["business_members.id"], name=op.f("fk_broker_attachments_uploaded_by_member_id_business_members")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broker_attachments")),
    )
    op.create_index("ix_broker_attachments_business_created", "broker_attachments", ["business_id", "created_at"])
    op.create_index("uq_broker_attachments_active_movement", "broker_attachments", ["broker_movement_id"], unique=True,
                    postgresql_where=sa.text("deleted_at IS NULL AND broker_movement_id IS NOT NULL"))
    op.create_index("uq_broker_attachments_source_message", "broker_attachments", ["source_message_id"], unique=True,
                    postgresql_where=sa.text("source_message_id IS NOT NULL"))
    op.create_table(
        "attachment_object_deletions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachment_object_deletions")),
        sa.UniqueConstraint("object_key", name=op.f("uq_attachment_object_deletions_object_key")),
    )


def downgrade() -> None:
    op.drop_table("attachment_object_deletions")
    op.drop_index("uq_broker_attachments_source_message", table_name="broker_attachments")
    op.drop_index("uq_broker_attachments_active_movement", table_name="broker_attachments")
    op.drop_index("ix_broker_attachments_business_created", table_name="broker_attachments")
    op.drop_table("broker_attachments")
    op.drop_table("attachment_blobs")
