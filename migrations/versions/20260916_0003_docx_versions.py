"""Persist DOCX template references and deterministic block replacements.

Revision ID: 20260916_0003
Revises: 20260916_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0003"
down_revision: str | None = "20260916_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # DOCX support intentionally replaces the pre-release text-attachment contract.
    # Clear all conversation state so no legacy attachment or canonical-work shape can
    # survive into the DOCX-only schema. Accounts, sessions, and the usage ledger remain.
    for statement in (
        "DELETE FROM work_versions",
        "DELETE FROM work_items",
        "DELETE FROM generations",
        "DELETE FROM attachments",
        "DELETE FROM messages",
        "DELETE FROM threads",
    ):
        op.execute(sa.text(statement))

    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.create_check_constraint(
            "ck_attachments_docx_media_type",
            "media_type = "
            "'application/vnd.openxmlformats-officedocument.wordprocessingml.document'",
        )

    with op.batch_alter_table("work_versions", recreate="always") as batch:
        batch.add_column(sa.Column("docx_template_attachment_id", sa.String(36)))
        batch.add_column(sa.Column("docx_blocks", sa.JSON(none_as_null=True)))
        batch.create_foreign_key(
            "fk_work_versions_docx_template_attachment_id_attachments",
            "attachments",
            ["docx_template_attachment_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_work_versions_docx_pair",
            "(docx_template_attachment_id IS NULL AND docx_blocks IS NULL) OR "
            "(docx_template_attachment_id IS NOT NULL AND docx_blocks IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("work_versions", recreate="always") as batch:
        batch.drop_constraint("ck_work_versions_docx_pair", type_="check")
        batch.drop_constraint(
            "fk_work_versions_docx_template_attachment_id_attachments",
            type_="foreignkey",
        )
        batch.drop_column("docx_blocks")
        batch.drop_column("docx_template_attachment_id")
    with op.batch_alter_table("attachments", recreate="always") as batch:
        batch.drop_constraint("ck_attachments_docx_media_type", type_="check")
