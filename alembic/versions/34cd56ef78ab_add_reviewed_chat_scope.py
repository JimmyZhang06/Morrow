"""Opt-in reviewed-memory scope; existing conversations remain unchanged."""

from alembic import op

revision = "34cd56ef78ab"
down_revision = "23bc45de67fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier migrations use the current registry; support both fresh and existing databases.
    op.execute(
        "ALTER TABLE conversation ADD COLUMN IF NOT EXISTS "
        "include_reviewed_memories BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.drop_column("conversation", "include_reviewed_memories")
