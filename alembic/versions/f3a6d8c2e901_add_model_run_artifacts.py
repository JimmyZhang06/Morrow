"""add durable model run artifact projections

Revision ID: f3a6d8c2e901
Revises: 7e2c1f9a6b40
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "f3a6d8c2e901"
down_revision = "7e2c1f9a6b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create one content-free Knowledge artifact pointer per model run."""

    op.create_table(
        "model_run_artifact",
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("derived_object_id", sa.Uuid(), nullable=False),
        sa.Column("memory_claim_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["vault_id", "derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_model_run_artifact_vault_derived",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "memory_claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_model_run_artifact_vault_memory_claim",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_model_run_artifact_vault_run",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_run_artifact")),
        sa.UniqueConstraint(
            "vault_id",
            "derived_object_id",
            name="uq_model_run_artifact_vault_derived",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "id",
            name="uq_model_run_artifact_vault_id_id",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "model_run_id",
            name="uq_model_run_artifact_vault_run",
        ),
    )
    op.create_index(
        op.f("ix_model_run_artifact_vault_id"),
        "model_run_artifact",
        ["vault_id"],
        unique=False,
    )
    op.create_index(
        "ix_claim_version_vault_model_run",
        "claim_version",
        ["vault_id", "model_run_id"],
        unique=False,
    )
    # These constraints protect every new candidate immediately.  They remain
    # NOT VALID until historical nullable/orphan values have been inventoried;
    # migration code must never synthesize receipts or silently erase lineage.
    op.create_foreign_key(
        "fk_claim_version_vault_model_run",
        "claim_version",
        "model_run",
        ["vault_id", "model_run_id"],
        ["vault_id", "id"],
        ondelete="RESTRICT",
        postgresql_not_valid=True,
    )
    op.create_foreign_key(
        "fk_evidence_link_vault_model_run",
        "evidence_link",
        "model_run",
        ["vault_id", "model_run_id"],
        ["vault_id", "id"],
        ondelete="RESTRICT",
        postgresql_not_valid=True,
    )

    load_model_registry()
    artifact_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in {
            "derived_object",
            "memory_claim",
            "model_run",
            "model_run_artifact",
            "vault",
        }:
            table.to_metadata(artifact_metadata)
    apply_postgres_security(op.get_bind(), artifact_metadata)


def downgrade() -> None:
    op.drop_constraint(
        "fk_evidence_link_vault_model_run",
        "evidence_link",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_claim_version_vault_model_run",
        "claim_version",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_claim_version_vault_model_run",
        table_name="claim_version",
    )
    op.drop_index(
        op.f("ix_model_run_artifact_vault_id"),
        table_name="model_run_artifact",
    )
    op.drop_table("model_run_artifact")
