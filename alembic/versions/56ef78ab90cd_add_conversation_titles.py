"""Encrypted manual conversation names; AI names stay with governed replies."""

from alembic import op

revision = "56ef78ab90cd"
down_revision = "45de67fa89bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS title_ciphertext BYTEA")
    op.execute(
        "ALTER TABLE conversation ADD COLUMN IF NOT EXISTS "
        "title_revision INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.drop_column("conversation", "title_revision")
    op.drop_column("conversation", "title_ciphertext")
