"""Persist extracted DOCX blocks with each attachment.

Revision ID: 20260917_0004
Revises: 20260916_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0004"
down_revision: str | None = "20260916_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.add_column(sa.Column("document_blocks", sa.JSON(none_as_null=True)))
    # The DOCX-only migration cleared pre-release conversations. This fallback keeps
    # upgrades deterministic if an attachment was created between these two releases;
    # the application rejects an empty block map instead of re-parsing legacy state.
    op.execute(sa.text("UPDATE attachments SET document_blocks = '[]'"))
    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.alter_column("document_blocks", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.drop_column("document_blocks")
