"""add evidence-backed narratives and calendar candidates

Revision ID: c9a7e3f1b204
Revises: b8f4c2d1e706
Create Date: 2026-08-26
"""
# ruff: noqa: E501

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "c9a7e3f1b204"
down_revision = "b8f4c2d1e706"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "narrative_project",
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("scope_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vault_id", "id", name="uq_narrative_project_vault_id_id"),
        sa.CheckConstraint(
            "scope_to IS NULL OR scope_from IS NULL OR scope_from < scope_to",
            name="narrative_project_scope_valid",
        ),
    )
    op.create_index("ix_narrative_project_vault_id", "narrative_project", ["vault_id"])
    op.create_index(
        "uq_narrative_project_default",
        "narrative_project",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )

    op.create_table(
        "narrative_generation",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("uncertainty", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["vault_id", "project_id"],
            ["narrative_project.vault_id", "narrative_project.id"],
            name="fk_narrative_generation_vault_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_narrative_generation_vault_model_run",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("vault_id", "id", name="uq_narrative_generation_vault_id_id"),
        sa.UniqueConstraint("vault_id", "model_run_id", name="uq_narrative_generation_model_run"),
        sa.CheckConstraint(
            "kind IN ('life_line', 'memoir_chapter')", name="narrative_generation_kind"
        ),
    )
    op.create_index("ix_narrative_generation_vault_id", "narrative_generation", ["vault_id"])
    op.create_index(
        "ix_narrative_generation_project_created",
        "narrative_generation",
        ["vault_id", "project_id", "created_at"],
    )

    op.create_table(
        "narrative_theme",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column("counterpoint", sa.Text(), nullable=False),
        sa.Column("uncovered_period", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["vault_id", "generation_id"],
            ["narrative_generation.vault_id", "narrative_generation.id"],
            name="fk_narrative_theme_vault_generation",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("vault_id", "id", name="uq_narrative_theme_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id", "generation_id", "position", name="uq_narrative_theme_position"
        ),
        sa.CheckConstraint("position > 0", name="narrative_theme_position_positive"),
    )
    op.create_index("ix_narrative_theme_vault_id", "narrative_theme", ["vault_id"])

    op.create_table(
        "narrative_citation",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("theme_id", sa.Uuid(), nullable=True),
        sa.Column("memory_claim_id", sa.Uuid(), nullable=False),
        sa.Column("derived_object_id", sa.Uuid(), nullable=False),
        sa.Column("material_ordinal", sa.Integer(), nullable=False),
        sa.Column("relation", sa.String(24), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["vault_id", "generation_id"],
            ["narrative_generation.vault_id", "narrative_generation.id"],
            name="fk_narrative_citation_vault_generation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "theme_id"],
            ["narrative_theme.vault_id", "narrative_theme.id"],
            name="fk_narrative_citation_vault_theme",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "memory_claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_narrative_citation_vault_memory",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_narrative_citation_vault_derived",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("vault_id", "id", name="uq_narrative_citation_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id",
            "generation_id",
            "theme_id",
            "memory_claim_id",
            "relation",
            name="uq_narrative_citation_anchor",
        ),
        sa.CheckConstraint("material_ordinal > 0", name="narrative_citation_ordinal_positive"),
    )
    op.create_index("ix_narrative_citation_vault_id", "narrative_citation", ["vault_id"])

    op.create_table(
        "calendar_candidate",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("notes", sa.String(500), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_provider", sa.String(32), nullable=True),
        sa.Column("external_event_id", sa.String(256), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["vault_id", "generation_id"],
            ["narrative_generation.vault_id", "narrative_generation.id"],
            name="fk_calendar_candidate_vault_generation",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("vault_id", "id", name="uq_calendar_candidate_vault_id_id"),
        sa.CheckConstraint("starts_at < ends_at", name="calendar_candidate_interval_valid"),
        sa.CheckConstraint("revision > 0", name="calendar_candidate_revision_positive"),
        sa.CheckConstraint("length(payload_hash) = 64", name="calendar_candidate_payload_hash"),
        sa.CheckConstraint(
            "state IN ('proposed', 'confirmed', 'revoked')", name="calendar_candidate_state"
        ),
    )
    op.create_index("ix_calendar_candidate_vault_id", "calendar_candidate", ["vault_id"])

    op.create_table(
        "calendar_command_receipt",
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("command_kind", sa.String(16), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["vault_id", "candidate_id"],
            ["calendar_candidate.vault_id", "calendar_candidate.id"],
            name="fk_calendar_command_receipt_vault_candidate",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("vault_id", "id", name="uq_calendar_command_receipt_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id", "idempotency_key", name="uq_calendar_command_receipt_idempotency"
        ),
        sa.CheckConstraint("length(command_hash) = 64", name="calendar_command_receipt_hash"),
    )
    op.create_index(
        "ix_calendar_command_receipt_vault_id", "calendar_command_receipt", ["vault_id"]
    )

    op.execute('DROP TRIGGER IF EXISTS "lc_append_only" ON model_run_artifact')
    op.drop_constraint("model_run_artifact_kind_shape", "model_run_artifact", type_="check")
    op.add_column(
        "model_run_artifact", sa.Column("narrative_generation_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_model_run_artifact_vault_narrative",
        "model_run_artifact",
        "narrative_generation",
        ["vault_id", "narrative_generation_id"],
        ["vault_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_model_run_artifact_vault_narrative",
        "model_run_artifact",
        ["vault_id", "narrative_generation_id"],
    )
    op.create_check_constraint(
        "model_run_artifact_kind_shape",
        "model_run_artifact",
        "(artifact_kind = 'knowledge' AND derived_object_id IS NOT NULL AND memory_claim_id IS NOT NULL AND action_id IS NULL AND narrative_generation_id IS NULL) OR "
        "(artifact_kind = 'action' AND action_id IS NOT NULL AND derived_object_id IS NULL AND memory_claim_id IS NULL AND narrative_generation_id IS NULL) OR "
        "(artifact_kind = 'narrative' AND narrative_generation_id IS NOT NULL AND derived_object_id IS NULL AND memory_claim_id IS NULL AND action_id IS NULL)",
    )

    load_model_registry()
    apply_postgres_security(op.get_bind(), Base.metadata)


def downgrade() -> None:
    op.execute('DROP TRIGGER IF EXISTS "lc_append_only" ON model_run_artifact')
    op.execute("DELETE FROM model_run_artifact WHERE artifact_kind = 'narrative'")
    op.drop_constraint("model_run_artifact_kind_shape", "model_run_artifact", type_="check")
    op.drop_constraint(
        "uq_model_run_artifact_vault_narrative", "model_run_artifact", type_="unique"
    )
    op.drop_constraint(
        "fk_model_run_artifact_vault_narrative", "model_run_artifact", type_="foreignkey"
    )
    op.drop_column("model_run_artifact", "narrative_generation_id")
    op.create_check_constraint(
        "model_run_artifact_kind_shape",
        "model_run_artifact",
        "(artifact_kind = 'knowledge' AND derived_object_id IS NOT NULL AND memory_claim_id IS NOT NULL AND action_id IS NULL) OR (artifact_kind = 'action' AND action_id IS NOT NULL AND derived_object_id IS NULL AND memory_claim_id IS NULL)",
    )
    op.execute(
        'CREATE TRIGGER "lc_append_only" BEFORE UPDATE OR DELETE ON model_run_artifact FOR EACH ROW EXECUTE FUNCTION life_coach_private.reject_immutable_mutation()'
    )
    op.drop_table("calendar_command_receipt")
    op.drop_table("calendar_candidate")
    op.drop_table("narrative_citation")
    op.drop_table("narrative_theme")
    op.drop_table("narrative_generation")
    op.drop_table("narrative_project")
