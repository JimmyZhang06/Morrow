"""Vault-scoped persistence for evidence-backed narrative work."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column

from life_coach.shared.database import Base, TimestampMixin, UUIDPrimaryKeyMixin, VaultScopedMixin


class NarrativeGenerationKind(StrEnum):
    LIFE_LINE = "life_line"
    MEMOIR_CHAPTER = "memoir_chapter"


class CalendarCandidateState(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REVOKED = "revoked"


def _enum(enum_type: type[StrEnum], name: str) -> SqlEnum:
    return SqlEnum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class NarrativeProject(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    __tablename__ = "narrative_project"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_narrative_project_vault_id_id"),
        Index(
            "uq_narrative_project_default",
            "vault_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
        CheckConstraint(
            "scope_to IS NULL OR scope_from IS NULL OR scope_from < scope_to",
            name="narrative_project_scope_valid",
        ),
    )

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    scope_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_default: Mapped[bool] = mapped_column(nullable=False, default=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")


class NarrativeGeneration(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    __tablename__ = "narrative_generation"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_narrative_generation_vault_id_id"),
        UniqueConstraint("vault_id", "model_run_id", name="uq_narrative_generation_model_run"),
        ForeignKeyConstraint(
            ["vault_id", "project_id"],
            ["narrative_project.vault_id", "narrative_project.id"],
            name="fk_narrative_generation_vault_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_narrative_generation_vault_model_run",
            ondelete="RESTRICT",
        ),
        Index("ix_narrative_generation_project_created", "vault_id", "project_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    model_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[NarrativeGenerationKind] = mapped_column(
        _enum(NarrativeGenerationKind, "narrative_generation_kind"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    uncertainty: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NarrativeTheme(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    __tablename__ = "narrative_theme"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_narrative_theme_vault_id_id"),
        UniqueConstraint(
            "vault_id", "generation_id", "position", name="uq_narrative_theme_position"
        ),
        ForeignKeyConstraint(
            ["vault_id", "generation_id"],
            ["narrative_generation.vault_id", "narrative_generation.id"],
            name="fk_narrative_theme_vault_generation",
            ondelete="CASCADE",
        ),
        CheckConstraint("position > 0", name="narrative_theme_position_positive"),
    )

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    interpretation: Mapped[str] = mapped_column(Text, nullable=False)
    counterpoint: Mapped[str] = mapped_column(Text, nullable=False)
    uncovered_period: Mapped[str] = mapped_column(Text, nullable=False)


class NarrativeCitation(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    __tablename__ = "narrative_citation"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_narrative_citation_vault_id_id"),
        UniqueConstraint(
            "vault_id",
            "generation_id",
            "theme_id",
            "memory_claim_id",
            "relation",
            name="uq_narrative_citation_anchor",
        ),
        ForeignKeyConstraint(
            ["vault_id", "generation_id"],
            ["narrative_generation.vault_id", "narrative_generation.id"],
            name="fk_narrative_citation_vault_generation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "theme_id"],
            ["narrative_theme.vault_id", "narrative_theme.id"],
            name="fk_narrative_citation_vault_theme",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "memory_claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_narrative_citation_vault_memory",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["vault_id", "derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_narrative_citation_vault_derived",
            ondelete="RESTRICT",
        ),
        CheckConstraint("material_ordinal > 0", name="narrative_citation_ordinal_positive"),
    )

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    theme_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    memory_claim_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    derived_object_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    material_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    relation: Mapped[str] = mapped_column(String(24), nullable=False)


class CalendarCandidate(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    __tablename__ = "calendar_candidate"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_calendar_candidate_vault_id_id"),
        ForeignKeyConstraint(
            ["vault_id", "generation_id"],
            ["narrative_generation.vault_id", "narrative_generation.id"],
            name="fk_calendar_candidate_vault_generation",
            ondelete="RESTRICT",
        ),
        CheckConstraint("starts_at < ends_at", name="calendar_candidate_interval_valid"),
        CheckConstraint("revision > 0", name="calendar_candidate_revision_positive"),
        CheckConstraint("length(payload_hash) = 64", name="calendar_candidate_payload_hash"),
    )

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[CalendarCandidateState] = mapped_column(
        _enum(CalendarCandidateState, "calendar_candidate_state"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_provider: Mapped[str | None] = mapped_column(String(32))
    external_event_id: Mapped[str | None] = mapped_column(String(256))


class CalendarCommandReceipt(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    __tablename__ = "calendar_command_receipt"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_calendar_command_receipt_vault_id_id"),
        UniqueConstraint(
            "vault_id", "idempotency_key", name="uq_calendar_command_receipt_idempotency"
        ),
        ForeignKeyConstraint(
            ["vault_id", "candidate_id"],
            ["calendar_candidate.vault_id", "calendar_candidate.id"],
            name="fk_calendar_command_receipt_vault_candidate",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(command_hash) = 64", name="calendar_command_receipt_hash"),
    )

    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "CalendarCandidate",
    "CalendarCandidateState",
    "CalendarCommandReceipt",
    "NarrativeCitation",
    "NarrativeGeneration",
    "NarrativeGenerationKind",
    "NarrativeProject",
    "NarrativeTheme",
]
