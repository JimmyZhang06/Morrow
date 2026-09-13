"""Saved conversations and typed governed reply artifacts."""

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "23bc45de67fa"
down_revision = "12ab34cd56ef"
branch_labels = None
depends_on = None

OLD_SHAPE = "(artifact_kind = 'knowledge' AND derived_object_id IS NOT NULL AND memory_claim_id IS NOT NULL AND action_id IS NULL AND narrative_generation_id IS NULL) OR (artifact_kind = 'action' AND action_id IS NOT NULL AND derived_object_id IS NULL AND memory_claim_id IS NULL AND narrative_generation_id IS NULL) OR (artifact_kind = 'narrative' AND narrative_generation_id IS NOT NULL AND derived_object_id IS NULL AND memory_claim_id IS NULL AND action_id IS NULL)"  # noqa: E501


def upgrade() -> None:
    load_model_registry()
    for name in ("conversation", "conversation_material", "conversation_turn"):
        Base.metadata.tables[name].create(bind=op.get_bind())
    op.add_column("model_run_artifact", sa.Column("conversation_turn_id", sa.Uuid(), nullable=True))
    op.drop_constraint("model_run_artifact_kind_shape", "model_run_artifact", type_="check")
    op.create_check_constraint(
        "model_run_artifact_kind_shape",
        "model_run_artifact",
        f"(({OLD_SHAPE}) AND conversation_turn_id IS NULL) OR (artifact_kind='conversation' AND conversation_turn_id IS NOT NULL AND derived_object_id IS NULL AND memory_claim_id IS NULL AND action_id IS NULL AND narrative_generation_id IS NULL)",  # noqa: E501
    )
    op.create_foreign_key(
        "fk_model_run_artifact_vault_conversation",
        "model_run_artifact",
        "conversation_turn",
        ["vault_id", "conversation_turn_id"],
        ["vault_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_model_run_artifact_vault_conversation",
        "model_run_artifact",
        ["vault_id", "conversation_turn_id"],
    )
    apply_postgres_security(op.get_bind(), Base.metadata)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS life_coach_private.claim_conversation_turn()")
    op.execute('DROP TRIGGER IF EXISTS "lc_append_only" ON model_run_artifact')
    op.execute("DELETE FROM model_run_artifact WHERE artifact_kind='conversation'")
    op.drop_constraint(
        "fk_model_run_artifact_vault_conversation", "model_run_artifact", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_model_run_artifact_vault_conversation", "model_run_artifact", type_="unique"
    )
    op.drop_constraint("model_run_artifact_kind_shape", "model_run_artifact", type_="check")
    op.drop_column("model_run_artifact", "conversation_turn_id")
    op.create_check_constraint("model_run_artifact_kind_shape", "model_run_artifact", OLD_SHAPE)
    for name in ("conversation_material", "conversation_turn", "conversation"):
        op.drop_table(name)
    apply_postgres_security(op.get_bind(), Base.metadata)
