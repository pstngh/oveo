"""Split the legacy writing section into Revision and Internal communications.

Revision ID: 20260916_0002
Revises: 20260916_0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260916_0002"
down_revision: str | None = "20260916_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_thread_constraints(*, mode_sql: str, voice_sql: str) -> None:
    with op.batch_alter_table("threads") as batch:
        batch.drop_constraint("ck_threads_mode", type_="check")
        batch.drop_constraint("ck_threads_mode_voice", type_="check")
        batch.create_check_constraint("ck_threads_mode", mode_sql)
        batch.create_check_constraint("ck_threads_mode_voice", voice_sql)


def upgrade() -> None:
    _replace_thread_constraints(
        mode_sql="mode IN ('translate', 'alithyagpt', 'revision', 'internal_comms')",
        voice_sql=(
            "(mode = 'translate' AND voice_key IS NULL) OR "
            "(mode IN ('alithyagpt', 'internal_comms') AND voice_key IS NOT NULL) OR "
            "(mode IN ('revision', 'internal_comms') AND voice_key IS NULL)"
        ),
    )
    op.execute("UPDATE threads SET mode = 'internal_comms' WHERE mode = 'alithyagpt'")
    _replace_thread_constraints(
        mode_sql="mode IN ('translate', 'revision', 'internal_comms')",
        voice_sql="voice_key IS NULL OR mode = 'internal_comms'",
    )

    with op.batch_alter_table("work_items") as batch:
        batch.drop_constraint("ck_work_items_kind", type_="check")
        batch.create_check_constraint(
            "ck_work_items_kind",
            "kind IN ('translation', 'revision', 'draft')",
        )


def downgrade() -> None:
    op.execute("UPDATE work_items SET kind = 'draft' WHERE kind = 'revision'")
    with op.batch_alter_table("work_items") as batch:
        batch.drop_constraint("ck_work_items_kind", type_="check")
        batch.create_check_constraint(
            "ck_work_items_kind",
            "kind IN ('translation', 'draft')",
        )

    _replace_thread_constraints(
        mode_sql="mode IN ('translate', 'revision', 'internal_comms', 'alithyagpt')",
        voice_sql=(
            "(mode = 'translate' AND voice_key IS NULL) OR "
            "mode IN ('revision', 'internal_comms') OR "
            "(mode = 'alithyagpt' AND voice_key IS NOT NULL)"
        ),
    )
    op.execute(
        "UPDATE threads SET mode = 'alithyagpt', "
        "voice_key = COALESCE(voice_key, 'comm_internes') "
        "WHERE mode IN ('revision', 'internal_comms')"
    )
    _replace_thread_constraints(
        mode_sql="mode IN ('translate', 'alithyagpt')",
        voice_sql=(
            "(mode = 'translate' AND voice_key IS NULL) OR "
            "(mode = 'alithyagpt' AND voice_key IS NOT NULL)"
        ),
    )
