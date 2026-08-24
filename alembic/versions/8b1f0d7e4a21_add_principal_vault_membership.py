"""add principal and Vault membership authorization

Revision ID: 8b1f0d7e4a21
Revises: 302f9513f94b
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "8b1f0d7e4a21"
down_revision = "302f9513f94b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create provisioned principals and read-only runtime memberships."""

    op.create_table(
        "principal",
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(subject_fingerprint) = 64",
            name=op.f("ck_principal_principal_subject_fingerprint_sha256_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_principal")),
        sa.UniqueConstraint(
            "issuer",
            "subject_fingerprint",
            name="uq_principal_issuer_subject_fingerprint",
        ),
    )
    op.create_table(
        "vault_membership",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "member",
                name="membership_role",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vault_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_vault_membership_vault_membership_generation_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principal.id"],
            name="fk_vault_membership_principal",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vault.id"],
            name="fk_vault_membership_vault",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vault_membership")),
        sa.UniqueConstraint(
            "vault_id",
            "principal_id",
            name="uq_vault_membership_vault_principal",
        ),
    )
    op.create_index(
        op.f("ix_vault_membership_principal_id"),
        "vault_membership",
        ["principal_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vault_membership_vault_id"),
        "vault_membership",
        ["vault_id"],
        unique=False,
    )

    # Principal provisioning stays outside the runtime role. Membership is
    # tenant-governed and receives SELECT-only business privileges below.
    op.execute('REVOKE ALL ON TABLE "principal" FROM PUBLIC')
    load_model_registry()
    authorization_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in {"principal", "vault", "vault_membership"}:
            table.to_metadata(authorization_metadata)
    apply_postgres_security(op.get_bind(), authorization_metadata)


def downgrade() -> None:
    """Remove the authorization tables without weakening the prior trust boundary."""

    op.drop_index(op.f("ix_vault_membership_vault_id"), table_name="vault_membership")
    op.drop_index(op.f("ix_vault_membership_principal_id"), table_name="vault_membership")
    op.drop_table("vault_membership")
    op.drop_table("principal")
