"""Opt-in care preferences and private background conversation scope."""

from alembic import op

revision = "67fa89bc01de"
down_revision = "56ef78ab90cd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS care_settings JSON")
    op.execute(
        "ALTER TABLE conversation ADD COLUMN IF NOT EXISTS "
        "care_origin BOOLEAN NOT NULL DEFAULT false"
    )

    op.execute("GRANT UPDATE (care_settings) ON TABLE vault TO life_coach_app")


def downgrade() -> None:
    op.drop_column("conversation", "care_origin")
    op.drop_column("vault", "care_settings")
