"""businesses.welcomed: el saludo inicial ya no depende de si la fila se acaba de crear

Revision ID: 0004
Revises: 0003

Motivo: el login del panel ahora puede crear el negocio ANTES de que llegue el primer mensaje de chat
(app/api/panel/auth.py, request_code usa identity.resolve en vez de member_by_phone). Sin esta columna,
el primer mensaje del dueño se interpretaba como respuesta a "¿cómo se llama tu negocio?" en vez de
recibir el saludo — porque la señal antigua (`Identity.is_new`) solo era cierta en el turno donde SE
CREABA la fila, y con el negocio ya creado por el panel, esa señal llegaba siempre en False.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("welcomed", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Negocios que ya avanzaron del primer paso obviamente ya recibieron el saludo alguna vez.
    op.execute("UPDATE businesses SET welcomed = true WHERE onboarding_state <> 'ask_business_name'")


def downgrade() -> None:
    op.drop_column("businesses", "welcomed")
