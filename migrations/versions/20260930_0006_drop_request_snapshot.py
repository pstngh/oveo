"""Drop the generations' stored request context.

Each request's context has been composed in memory since the generation task started
building it; the column was only ever written as an empty object afterwards.

Revision ID: 20260930_0006
Revises: 20260917_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0006"
down_revision: str | None = "20260917_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("generations", recreate="always") as batch:
        batch.drop_column("request_snapshot")


def downgrade() -> None:
    with op.batch_alter_table("generations", recreate="always") as batch:
        batch.add_column(
            sa.Column("request_snapshot", sa.JSON(), nullable=False, server_default="{}")
        )
