"""add Vault-scoped reversible small actions

Revision ID: a9c4e7b2d615
Revises: f3a6d8c2e901
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "a9c4e7b2d615"
down_revision = "f3a6d8c2e901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create one local action projection and immutable command stream."""

    op.create_table(
        "reversible_action",
        sa.Column("memory_claim_id", sa.Uuid(), nullable=False),
        sa.Column("source_derived_object_id", sa.Uuid(), nullable=False),
        sa.Column("source_version_no", sa.Integer(), nullable=False),
        sa.Column("template_version", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("exit_plan", sa.Text(), nullable=False),
        sa.Column("estimated_minutes", sa.Integer(), nullable=False),
        sa.Column("is_reversible", sa.Boolean(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "proposed",
                "accepted",
                "completed",
                "revoked",
                name="reversible_action_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "estimated_minutes BETWEEN 1 AND 15",
            name=op.f("ck_reversible_action_reversible_action_duration_small"),
        ),
        sa.CheckConstraint(
            "is_reversible",
            name=op.f("ck_reversible_action_reversible_action_must_be_reversible"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_reversible_action_reversible_action_revision_positive"),
        ),
        sa.CheckConstraint(
            "source_version_no > 0",
            name=op.f("ck_reversible_action_reversible_action_source_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "memory_claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_reversible_action_vault_memory_claim",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "source_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_reversible_action_vault_source_derived",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reversible_action")),
        sa.UniqueConstraint("vault_id", "id", name="uq_reversible_action_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id",
            "source_derived_object_id",
            "template_version",
            name="uq_reversible_action_source_template",
        ),
    )
    op.create_index(
        op.f("ix_reversible_action_vault_id"),
        "reversible_action",
        ["vault_id"],
        unique=False,
    )
    op.create_table(
        "action_verdict",
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column(
            "verdict",
            sa.Enum(
                "accept",
                "complete",
                "revoke",
                name="reversible_action_verdict",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "resulting_state",
            sa.Enum(
                "proposed",
                "accepted",
                "completed",
                "revoked",
                name="action_verdict_resulting_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "resulting_revision > 1",
            name=op.f("ck_action_verdict_action_verdict_revision_valid"),
        ),
        sa.CheckConstraint(
            "sequence_no > 0",
            name=op.f("ck_action_verdict_action_verdict_sequence_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "action_id"],
            ["reversible_action.vault_id", "reversible_action.id"],
            name="fk_action_verdict_vault_action",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_verdict")),
        sa.UniqueConstraint("vault_id", "id", name="uq_action_verdict_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id", "action_id", "sequence_no", name="uq_action_verdict_sequence"
        ),
    )
    op.create_index(
        "ix_action_verdict_stream",
        "action_verdict",
        ["vault_id", "action_id", "sequence_no"],
        unique=False,
    )
    op.create_index(
        op.f("ix_action_verdict_vault_id"), "action_verdict", ["vault_id"], unique=False
    )
    op.create_table(
        "action_command_receipt",
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("command_kind", sa.String(length=16), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("verdict_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(command_fingerprint) = 64",
            name=op.f("ck_action_command_receipt_action_command_receipt_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "(command_kind = 'create' AND verdict_id IS NULL) OR "
            "(command_kind = 'verdict' AND verdict_id IS NOT NULL)",
            name=op.f("ck_action_command_receipt_action_command_receipt_result_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "action_id"],
            ["reversible_action.vault_id", "reversible_action.id"],
            name="fk_action_command_receipt_vault_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "verdict_id"],
            ["action_verdict.vault_id", "action_verdict.id"],
            name="fk_action_command_receipt_vault_verdict",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_command_receipt")),
        sa.UniqueConstraint("vault_id", "id", name="uq_action_command_receipt_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id",
            "idempotency_key",
            name="uq_action_command_receipt_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_action_command_receipt_vault_id"),
        "action_command_receipt",
        ["vault_id"],
        unique=False,
    )

    load_model_registry()
    action_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in {
            "action_command_receipt",
            "action_verdict",
            "derived_object",
            "memory_claim",
            "reversible_action",
            "vault",
        }:
            table.to_metadata(action_metadata)
    apply_postgres_security(op.get_bind(), action_metadata)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_action_command_receipt_vault_id"),
        table_name="action_command_receipt",
    )
    op.drop_table("action_command_receipt")
    op.drop_index(op.f("ix_action_verdict_vault_id"), table_name="action_verdict")
    op.drop_index("ix_action_verdict_stream", table_name="action_verdict")
    op.drop_table("action_verdict")
    op.drop_index(op.f("ix_reversible_action_vault_id"), table_name="reversible_action")
    op.drop_table("reversible_action")
