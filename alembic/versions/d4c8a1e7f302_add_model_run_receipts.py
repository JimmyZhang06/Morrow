"""add governed model run receipts

Revision ID: d4c8a1e7f302
Revises: 8b1f0d7e4a21
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "d4c8a1e7f302"
down_revision = "8b1f0d7e4a21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create content-free model receipts and their technical input references."""

    op.create_table(
        "model_run",
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=79), nullable=False),
        sa.Column("task_definition_hash", sa.String(length=79), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("model_revision", sa.String(length=64), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column("consent_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("policy_epoch", sa.Integer(), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column(
            "actual_sensitivity",
            sa.Enum(
                "normal",
                "sensitive",
                "highly_sensitive",
                name="model_run_sensitivity",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("data_residency", sa.String(length=128), nullable=False),
        sa.Column(
            "retention_policy",
            sa.Enum(
                "zero_retention",
                "transient",
                "short_term",
                "provider_managed",
                name="model_run_retention_policy",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "authorized",
                "dispatching",
                "succeeded",
                "failed",
                "unknown",
                "denied",
                "canceled",
                name="model_run_state",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default=sa.text("'authorized'"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "authorized_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("io_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "attempt >= 0",
            name=op.f("ck_model_run_model_run_attempt_nonnegative"),
        ),
        sa.CheckConstraint(
            "state <> 'authorized' OR "
            "(attempt = 0 AND dispatch_generation = 0 "
            "AND dispatch_started_at IS NULL AND dispatch_expires_at IS NULL "
            "AND io_finished_at IS NULL AND completed_at IS NULL)",
            name=op.f("ck_model_run_model_run_authorized_shape"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR authorized_at <= completed_at",
            name=op.f("ck_model_run_model_run_completion_after_authorization"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR io_finished_at IS NULL OR io_finished_at <= completed_at",
            name=op.f("ck_model_run_model_run_completion_after_io"),
        ),
        sa.CheckConstraint(
            "length(consent_snapshot_id) = 72 AND consent_snapshot_id LIKE 'consent:%'",
            name=op.f("ck_model_run_model_run_consent_snapshot_format"),
        ),
        sa.CheckConstraint(
            "consent_snapshot_id ~ '^consent:[0-9a-f]{64}$'",
            name=op.f("ck_model_run_model_run_consent_snapshot_hex"),
        ),
        sa.CheckConstraint(
            "data_residency ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'",
            name=op.f("ck_model_run_model_run_data_residency_technical"),
        ),
        sa.CheckConstraint(
            "dispatch_expires_at IS NULL OR "
            "(dispatch_started_at IS NOT NULL AND dispatch_started_at < dispatch_expires_at)",
            name=op.f("ck_model_run_model_run_dispatch_interval_nonempty"),
        ),
        sa.CheckConstraint(
            "dispatch_generation >= 0",
            name=op.f("ck_model_run_model_run_dispatch_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "dispatch_started_at IS NULL OR authorized_at <= dispatch_started_at",
            name=op.f("ck_model_run_model_run_dispatch_after_authorization"),
        ),
        sa.CheckConstraint(
            "state <> 'dispatching' OR "
            "(attempt > 0 AND dispatch_generation > 0 "
            "AND dispatch_started_at IS NOT NULL AND dispatch_expires_at IS NOT NULL "
            "AND io_finished_at IS NULL AND completed_at IS NULL)",
            name=op.f("ck_model_run_model_run_dispatching_shape"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ "
            "'^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
            "[a-z][a-z0-9_.-]{0,31}:([0-9]{1,20}|[0-9a-f]{16,64}|"
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))$'",
            name=op.f("ck_model_run_model_run_idempotency_key_technical"),
        ),
        sa.CheckConstraint(
            "io_finished_at IS NULL OR "
            "(dispatch_started_at IS NOT NULL AND dispatch_started_at <= io_finished_at)",
            name=op.f("ck_model_run_model_run_io_after_dispatch"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'unknown') OR "
            "(attempt > 0 AND dispatch_generation > 0 "
            "AND dispatch_started_at IS NOT NULL AND dispatch_expires_at IS NOT NULL "
            "AND io_finished_at IS NOT NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_model_run_model_run_io_terminal_shape"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('failed', 'unknown', 'denied', 'canceled') "
            "OR safe_error_code IS NOT NULL",
            name=op.f("ck_model_run_model_run_failure_has_error_code"),
        ),
        sa.CheckConstraint(
            "model ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'",
            name=op.f("ck_model_run_model_run_model_technical"),
        ),
        sa.CheckConstraint(
            "model_revision ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_model_run_model_run_model_revision_technical"),
        ),
        sa.CheckConstraint(
            "state IN ('succeeded', 'failed', 'unknown', 'denied', 'canceled') "
            "OR completed_at IS NULL",
            name=op.f("ck_model_run_model_run_nonterminal_without_completion"),
        ),
        sa.CheckConstraint(
            "state IN ('succeeded', 'failed', 'unknown', 'denied', 'canceled') "
            "OR (safe_error_code IS NULL AND provider_request_id IS NULL)",
            name=op.f("ck_model_run_model_run_nonterminal_without_outcome"),
        ),
        sa.CheckConstraint(
            "pipeline_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_model_run_model_run_pipeline_version_technical"),
        ),
        sa.CheckConstraint(
            "policy_epoch >= 0",
            name=op.f("ck_model_run_model_run_policy_epoch_nonnegative"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('denied', 'canceled') OR "
            "(attempt = 0 AND dispatch_generation = 0 "
            "AND dispatch_started_at IS NULL AND dispatch_expires_at IS NULL "
            "AND io_finished_at IS NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_model_run_model_run_pre_dispatch_terminal_shape"),
        ),
        sa.CheckConstraint(
            "prompt_template_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_model_run_model_run_prompt_template_version_technical"),
        ),
        sa.CheckConstraint(
            "provider ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'",
            name=op.f("ck_model_run_model_run_provider_technical"),
        ),
        sa.CheckConstraint(
            "provider_request_id IS NULL OR length(provider_request_id) BETWEEN 1 AND 255",
            name=op.f("ck_model_run_model_run_provider_request_id_length"),
        ),
        sa.CheckConstraint(
            "provider_request_id ~ '^$|^[A-Za-z0-9][A-Za-z0-9._~:/+=@-]{0,254}$'",
            name=op.f("ck_model_run_model_run_provider_request_id_technical"),
        ),
        sa.CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name=op.f("ck_model_run_model_run_request_hash_vault_hmac"),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^hmac-sha256:v1:[0-9a-f]{64}$'",
            name=op.f("ck_model_run_model_run_request_hash_hex"),
        ),
        sa.CheckConstraint(
            "safe_error_code IS NULL OR length(safe_error_code) BETWEEN 1 AND 100",
            name=op.f("ck_model_run_model_run_safe_error_code_length"),
        ),
        sa.CheckConstraint(
            "safe_error_code ~ '^$|^[a-z][a-z0-9_.:-]{0,99}$'",
            name=op.f("ck_model_run_model_run_safe_error_code_technical"),
        ),
        sa.CheckConstraint(
            "schema_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name=op.f("ck_model_run_model_run_schema_version_technical"),
        ),
        sa.CheckConstraint(
            "source_generation >= 0",
            name=op.f("ck_model_run_model_run_source_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "state != 'succeeded' OR safe_error_code IS NULL",
            name=op.f("ck_model_run_model_run_success_without_error"),
        ),
        sa.CheckConstraint(
            "length(task_definition_hash) = 79 AND task_definition_hash LIKE 'hmac-sha256:v1:%'",
            name=op.f("ck_model_run_model_run_task_definition_hash_vault_hmac"),
        ),
        sa.CheckConstraint(
            "task_definition_hash ~ '^hmac-sha256:v1:[0-9a-f]{64}$'",
            name=op.f("ck_model_run_model_run_task_definition_hash_hex"),
        ),
        sa.CheckConstraint(
            "task_type ~ '^[a-z][a-z0-9_.:-]{0,99}$'",
            name=op.f("ck_model_run_model_run_task_type_technical"),
        ),
        sa.ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_model_run_vault"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_run")),
        sa.UniqueConstraint("vault_id", "id", name="uq_model_run_vault_id_id"),
        sa.UniqueConstraint(
            "vault_id",
            "task_type",
            "idempotency_key",
            name="uq_model_run_vault_task_idempotency",
        ),
    )
    op.create_index(op.f("ix_model_run_vault_id"), "model_run", ["vault_id"], unique=False)
    op.create_index(
        "ix_model_run_vault_state_expiry",
        "model_run",
        ["vault_id", "state", "dispatch_expires_at"],
        unique=False,
    )

    op.create_table(
        "model_run_input",
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "source_revision",
                "source_fragment",
                "derived_object",
                name="model_run_input_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=79), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "content_fingerprint ~ '^hmac-sha256:v1:[0-9a-f]{64}$'",
            name=op.f("ck_model_run_input_model_run_input_content_fingerprint_hex"),
        ),
        sa.CheckConstraint(
            "length(content_fingerprint) = 79 AND content_fingerprint LIKE 'hmac-sha256:v1:%'",
            name=op.f("ck_model_run_input_model_run_input_content_fingerprint_vault_hmac"),
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name=op.f("ck_model_run_input_model_run_input_ordinal_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_model_run_input_vault_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_run_input")),
        sa.UniqueConstraint(
            "vault_id",
            "model_run_id",
            "kind",
            "object_id",
            name="uq_model_run_input_object",
        ),
        sa.UniqueConstraint(
            "vault_id",
            "model_run_id",
            "ordinal",
            name="uq_model_run_input_ordinal",
        ),
    )
    op.create_index(
        op.f("ix_model_run_input_vault_id"),
        "model_run_input",
        ["vault_id"],
        unique=False,
    )

    load_model_registry()
    receipt_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in {"model_run", "model_run_input", "vault"}:
            table.to_metadata(receipt_metadata)
    apply_postgres_security(op.get_bind(), receipt_metadata)


def downgrade() -> None:
    """Remove model receipts while retaining all earlier security boundaries."""

    op.drop_index(op.f("ix_model_run_input_vault_id"), table_name="model_run_input")
    op.drop_table("model_run_input")
    op.drop_index("ix_model_run_vault_state_expiry", table_name="model_run")
    op.drop_index(op.f("ix_model_run_vault_id"), table_name="model_run")
    op.drop_table("model_run")
