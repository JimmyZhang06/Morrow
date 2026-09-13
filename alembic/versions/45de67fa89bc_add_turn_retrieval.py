"""Per-turn local retrieval manifest; legacy turns keep their fixed scope."""

from alembic import op

revision = "45de67fa89bc"
down_revision = "34cd56ef78ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE conversation_turn ADD COLUMN IF NOT EXISTS retrieval JSON")


def downgrade() -> None:
    op.drop_column("conversation_turn", "retrieval")
