"""add Source API command receipts

Revision ID: 7e2c1f9a6b40
Revises: d4c8a1e7f302
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "7e2c1f9a6b40"
down_revision = "d4c8a1e7f302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the content-free immutable Source command idempotency ledger."""

    op.create_table(
        "source_command_receipt",
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("client_key_hash", sa.String(length=79), nullable=False),
        sa.Column("request_hash", sa.String(length=79), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("resource_revision_id", sa.Uuid(), nullable=True),
        sa.Column("result_revision_no", sa.Integer(), nullable=True),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("policy_epoch", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(client_key_hash) = 79 AND client_key_hash LIKE 'hmac-sha256:v1:%'",
            name=op.f("ck_source_command_receipt_source_command_receipt_client_key_hash"),
        ),
        sa.CheckConstraint(
            "length(operation) BETWEEN 1 AND 100",
            name=op.f("ck_source_command_receipt_source_command_receipt_operation_technical"),
        ),
        sa.CheckConstraint(
            "policy_epoch >= 0",
            name=op.f("ck_source_command_receipt_source_command_receipt_policy_epoch_nonnegative"),
        ),
        sa.CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name=op.f("ck_source_command_receipt_source_command_receipt_request_hash"),
        ),
        sa.CheckConstraint(
            "result_revision_no IS NULL OR result_revision_no >= 1",
            name=op.f("ck_source_command_receipt_source_command_receipt_revision_positive"),
        ),
        sa.CheckConstraint(
            "source_generation >= 0",
            name=op.f(
                "ck_source_command_receipt_source_command_receipt_source_generation_nonnegative"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "resource_id"],
            ["source_document.vault_id", "source_document.id"],
            name="fk_source_command_receipt_vault_document",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "resource_id", "resource_revision_id"],
            [
                "source_revision.vault_id",
                "source_revision.document_id",
                "source_revision.id",
            ],
            name="fk_source_command_receipt_vault_revision",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_command_receipt")),
        sa.UniqueConstraint(
            "vault_id",
            "id",
            name="uq_source_command_receipt_vault_id_id",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "operation",
            "client_key_hash",
            name="uq_source_command_receipt_vault_operation_key",
        ),
    )
    op.create_index(
        op.f("ix_source_command_receipt_vault_id"),
        "source_command_receipt",
        ["vault_id"],
        unique=False,
    )

    load_model_registry()
    # Migration modules import the current ORM registry, which may contain
    # tables introduced by later revisions. Security DDL must only target the
    # schema that exists at this point in the migration timeline.
    source_api_table_names = {
        "claim_version",
        "consent_record",
        "derived_object",
        "evidence_link",
        "job",
        "memory_claim",
        "memory_suppression",
        "model_run",
        "model_run_input",
        "outbound_operation",
        "outbox_event",
        "principal",
        "search_projection",
        "source_command_receipt",
        "source_document",
        "source_fragment",
        "source_revision",
        "user_verdict",
        "vault",
        "vault_membership",
    }
    source_api_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in source_api_table_names:
            table.to_metadata(source_api_metadata)
    apply_postgres_security(op.get_bind(), source_api_metadata)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_source_command_receipt_vault_id"),
        table_name="source_command_receipt",
    )
    op.drop_table("source_command_receipt")
