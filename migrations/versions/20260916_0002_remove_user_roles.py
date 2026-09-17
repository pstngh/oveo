"""Remove the unused account role distinction.

Revision ID: 20260916_0002
Revises: 20260916_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0002"
down_revision: str | None = "20260916_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", recreate="always") as batch:
        batch.drop_constraint("ck_users_role", type_="check")
        batch.drop_column("role")


def downgrade() -> None:
    with op.batch_alter_table("users", recreate="always") as batch:
        batch.add_column(sa.Column("role", sa.String(16), nullable=False, server_default="user"))
        batch.create_check_constraint("ck_users_role", "role IN ('owner', 'user')")
