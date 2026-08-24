"""SQLAlchemy models for the vault privacy boundary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column

from life_coach.shared.database import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
)


class DataClass(StrEnum):
    """Sensitivity label used to reduce, never expand, allowed processing."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"
    HIGHLY_SENSITIVE = "highly_sensitive"


class CreatedBy(StrEnum):
    """Originator category without copying user content into metadata."""

    USER = "user"
    SYSTEM_COMPONENT = "system_component"
    IMPORT = "import"


class MembershipRole(StrEnum):
    """The deliberately small set of Vault-local human roles."""

    OWNER = "owner"
    MEMBER = "member"


def data_class_type() -> SqlEnum:
    """Return a portable enum type which persists public values, not member names."""

    return SqlEnum(
        DataClass,
        name="data_class",
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
        length=32,
    )


def created_by_type() -> SqlEnum:
    """Return a portable enum type which persists public values, not member names."""

    return SqlEnum(
        CreatedBy,
        name="created_by",
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
        length=32,
    )


def membership_role_type() -> SqlEnum:
    """Persist stable public role values."""

    return SqlEnum(
        MembershipRole,
        name="membership_role",
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
        length=16,
    )


class Principal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A provisioned authentication subject without profile or journal content.

    ``subject_fingerprint`` must be a keyed SHA-256 fingerprint produced by the
    identity provisioning boundary. Raw OIDC subjects are intentionally not stored.
    Runtime requests use a validated internal ``principal_id`` token claim and do
    not receive direct table access.
    """

    __tablename__ = "principal"
    __table_args__ = (
        UniqueConstraint(
            "issuer", "subject_fingerprint", name="uq_principal_issuer_subject_fingerprint"
        ),
        CheckConstraint(
            "length(subject_fingerprint) = 64",
            name="principal_subject_fingerprint_sha256_length",
        ),
    )

    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Vault(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant and privacy boundary with monotonic processing fences."""

    __tablename__ = "vault"
    __table_args__ = (
        CheckConstraint("policy_epoch >= 0", name="policy_epoch_nonnegative"),
        CheckConstraint("source_generation >= 0", name="source_generation_nonnegative"),
    )

    policy_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    source_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[CreatedBy] = mapped_column(
        created_by_type(), nullable=False, default=CreatedBy.USER
    )
    data_class: Mapped[DataClass] = mapped_column(
        data_class_type(), nullable=False, default=DataClass.SENSITIVE
    )


class VaultMembership(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    """A provisioned principal-to-Vault authorization checked inside Vault RLS.

    The runtime role can only read this table. Grant, revoke, and role changes are
    administrative operations so a business request cannot grant itself access.
    """

    __tablename__ = "vault_membership"
    __table_args__ = (
        UniqueConstraint("vault_id", "principal_id", name="uq_vault_membership_vault_principal"),
        ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_vault_membership_vault"),
        ForeignKeyConstraint(
            ["principal_id"], ["principal.id"], name="fk_vault_membership_principal"
        ),
        CheckConstraint("generation > 0", name="vault_membership_generation_positive"),
    )

    principal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    role: Mapped[MembershipRole] = mapped_column(membership_role_type(), nullable=False)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
