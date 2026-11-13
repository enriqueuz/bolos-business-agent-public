"""país declarado por el dueño en el onboarding

Revision ID: 0006
Revises: 0005

El prefijo telefónico solo sugiere el país; el onboarding ahora pregunta. Si el país del dueño no está soportado,
country_code queda en el default y aquí se guarda lo que dijo (para avisarle cuando se agregue su país).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("declared_country", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("businesses", "declared_country")
