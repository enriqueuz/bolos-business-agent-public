"""onboarding como formulario: borrador en JSON y dos estados (in_progress / done)

Revision ID: 0005
Revises: 0004

El onboarding dejó de ser una secuencia fija de preguntas: un LLM interpreta cada mensaje y puede llenar o corregir
cualquier campo. Las respuestas viven en businesses.onboarding_draft hasta que el dueño confirma el resumen.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("onboarding_draft", postgresql.JSONB(), nullable=False,
                                          server_default=sa.text("'{}'::jsonb")))
    op.execute("""
        UPDATE businesses
        SET onboarding_draft = jsonb_strip_nulls(jsonb_build_object('name', name, 'what_sells', what_sells)),
            onboarding_state = 'in_progress'
        WHERE onboarding_state <> 'done'
    """)
    op.alter_column("businesses", "onboarding_state", server_default="in_progress")


def downgrade() -> None:
    op.alter_column("businesses", "onboarding_state", server_default="ask_business_name")
    op.execute("UPDATE businesses SET onboarding_state = 'ask_business_name' WHERE onboarding_state = 'in_progress'")
    op.drop_column("businesses", "onboarding_draft")
