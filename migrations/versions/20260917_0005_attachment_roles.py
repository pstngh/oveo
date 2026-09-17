"""Distinguish working-source and reference DOCX attachments.

Revision ID: 20260917_0005
Revises: 20260917_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0005"
down_revision: str | None = "20260917_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing attachments were created under the source-only contract, so source is
    # the only safe backward-compatible classification.
    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "role",
                sa.String(16),
                nullable=False,
                server_default="source",
            )
        )
        batch.create_check_constraint(
            "ck_attachments_role",
            "role IN ('source', 'reference')",
        )


def downgrade() -> None:
    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.drop_constraint("ck_attachments_role", type_="check")
        batch.drop_column("role")
