"""add governed AI action lineage and artifact pointers

Revision ID: b8f4c2d1e706
Revises: e7a1c5d9b204
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "b8f4c2d1e706"
down_revision = "e7a1c5d9b204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reversible_action",
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reversible_action_vault_model_run",
        "reversible_action",
        "model_run",
        ["vault_id", "model_run_id"],
        ["vault_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_reversible_action_vault_model_run",
        "reversible_action",
        ["vault_id", "model_run_id"],
        unique=False,
    )

    op.add_column(
        "model_run_artifact",
        sa.Column(
            "artifact_kind",
            sa.String(length=16),
            server_default="knowledge",
            nullable=True,
        ),
    )
    op.add_column(
        "model_run_artifact",
        sa.Column("action_id", sa.Uuid(), nullable=True),
    )
    op.execute('DROP TRIGGER IF EXISTS "lc_append_only" ON model_run_artifact')
    op.execute("UPDATE model_run_artifact SET artifact_kind = 'knowledge'")
    op.alter_column("model_run_artifact", "artifact_kind", nullable=False)
    op.alter_column("model_run_artifact", "derived_object_id", nullable=True)
    op.alter_column("model_run_artifact", "memory_claim_id", nullable=True)
    op.create_foreign_key(
        "fk_model_run_artifact_vault_action",
        "model_run_artifact",
        "reversible_action",
        ["vault_id", "action_id"],
        ["vault_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_model_run_artifact_vault_action",
        "model_run_artifact",
        ["vault_id", "action_id"],
    )
    op.create_check_constraint(
        "model_run_artifact_kind_shape",
        "model_run_artifact",
        "(artifact_kind = 'knowledge' AND derived_object_id IS NOT NULL "
        "AND memory_claim_id IS NOT NULL AND action_id IS NULL) OR "
        "(artifact_kind = 'action' AND action_id IS NOT NULL "
        "AND derived_object_id IS NULL AND memory_claim_id IS NULL)",
    )

    load_model_registry()
    artifact_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in {
            "derived_object",
            "memory_claim",
            "model_run",
            "model_run_artifact",
            "reversible_action",
            "vault",
        }:
            table.to_metadata(artifact_metadata)
    apply_postgres_security(op.get_bind(), artifact_metadata)


def downgrade() -> None:
    op.execute('DROP TRIGGER IF EXISTS "lc_append_only" ON model_run_artifact')
    op.execute("DELETE FROM model_run_artifact WHERE artifact_kind = 'action'")
    op.drop_constraint(
        "model_run_artifact_kind_shape",
        "model_run_artifact",
        type_="check",
    )
    op.drop_constraint(
        "uq_model_run_artifact_vault_action",
        "model_run_artifact",
        type_="unique",
    )
    op.drop_constraint(
        "fk_model_run_artifact_vault_action",
        "model_run_artifact",
        type_="foreignkey",
    )
    op.alter_column("model_run_artifact", "memory_claim_id", nullable=False)
    op.alter_column("model_run_artifact", "derived_object_id", nullable=False)
    op.drop_column("model_run_artifact", "action_id")
    op.drop_column("model_run_artifact", "artifact_kind")
    op.execute(
        'CREATE TRIGGER "lc_append_only" BEFORE UPDATE OR DELETE '
        "ON model_run_artifact FOR EACH ROW EXECUTE FUNCTION "
        "life_coach_private.reject_immutable_mutation()"
    )

    op.drop_index(
        "ix_reversible_action_vault_model_run",
        table_name="reversible_action",
    )
    op.drop_constraint(
        "fk_reversible_action_vault_model_run",
        "reversible_action",
        type_="foreignkey",
    )
    op.drop_column("reversible_action", "model_run_id")
