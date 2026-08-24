"""SQLAlchemy 2 mappings for evidence-backed, user-governed memory.

`ClaimVersion` uses table extension instead of ORM polymorphism: every row has a
matching `DerivedObject`, and all evidence/verdict targets are database-verifiable
composite foreign keys scoped by vault.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    literal_column,
    text,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column

from life_coach.shared.database import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
    utc_now,
)

from .enums import (
    Attribution,
    ClaimVersionOrigin,
    ConfidenceBand,
    CorrectionMode,
    DataClass,
    DerivedObjectKind,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    SourceEvidenceStatus,
    TechnicalActor,
    ValidTimePrecision,
    VerdictType,
)
from .exceptions import AppendOnlyViolationError


def _persisted_enum(enum_class: type[StrEnum], name: str) -> SqlEnum:
    """Persist enum values (rather than Python member names) portably."""

    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


_STRUCTURED_PAYLOAD_TYPE = JSON().with_variant(JSONB(), "postgresql")
_OPTIONAL_STRUCTURED_PAYLOAD_TYPE = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)


class DerivedObject(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    """Super-table for every object that can receive evidence or a verdict."""

    __tablename__ = "derived_object"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_derived_object_vault_id_id"),
        CheckConstraint("review_revision >= 0", name="derived_object_review_revision_nonnegative"),
    )

    object_kind: Mapped[DerivedObjectKind] = mapped_column(
        _persisted_enum(DerivedObjectKind, "derived_object_kind"), nullable=False
    )
    created_by: Mapped[TechnicalActor] = mapped_column(
        _persisted_enum(TechnicalActor, "derived_object_created_by"), nullable=False
    )
    data_class: Mapped[DataClass] = mapped_column(
        _persisted_enum(DataClass, "knowledge_data_class"),
        nullable=False,
        default=DataClass.NORMAL,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MemoryClaim(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    """A stable logical claim whose interpretation changes through versions."""

    __tablename__ = "memory_claim"
    __table_args__ = (UniqueConstraint("vault_id", "id", name="uq_memory_claim_vault_id_id"),)

    kind: Mapped[MemoryClaimKind] = mapped_column(
        _persisted_enum(MemoryClaimKind, "memory_claim_kind"), nullable=False
    )
    subject_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    suppression_lineage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=uuid.uuid4, index=True
    )
    created_by: Mapped[TechnicalActor] = mapped_column(
        _persisted_enum(TechnicalActor, "memory_claim_created_by"), nullable=False
    )
    data_class: Mapped[DataClass] = mapped_column(
        _persisted_enum(DataClass, "memory_claim_data_class"),
        nullable=False,
        default=DataClass.NORMAL,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimVersion(Base):
    """One bitemporal interpretation of a logical memory claim."""

    __tablename__ = "claim_version"

    derived_object_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    vault_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_payload: Mapped[dict[str, Any]] = mapped_column(
        _STRUCTURED_PAYLOAD_TYPE, nullable=False, default=dict
    )
    epistemic_type: Mapped[EpistemicType] = mapped_column(
        _persisted_enum(EpistemicType, "claim_epistemic_type"), nullable=False
    )
    attribution: Mapped[Attribution] = mapped_column(
        _persisted_enum(Attribution, "claim_attribution"), nullable=False
    )
    uncertainty_text: Mapped[str | None] = mapped_column(Text)
    initial_lifecycle_state: Mapped[LifecycleState] = mapped_column(
        _persisted_enum(LifecycleState, "claim_initial_lifecycle_state"), nullable=False
    )
    lifecycle_state: Mapped[LifecycleState] = mapped_column(
        _persisted_enum(LifecycleState, "claim_lifecycle_state"), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_time_precision: Mapped[ValidTimePrecision] = mapped_column(
        _persisted_enum(ValidTimePrecision, "claim_valid_time_precision"), nullable=False
    )
    valid_time_original: Mapped[str | None] = mapped_column(Text)
    valid_timezone: Mapped[str | None] = mapped_column(String(128))
    system_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    system_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence_band: Mapped[ConfidenceBand] = mapped_column(
        _persisted_enum(ConfidenceBand, "claim_confidence_band"), nullable=False
    )
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    origin: Mapped[ClaimVersionOrigin] = mapped_column(
        _persisted_enum(ClaimVersionOrigin, "claim_version_origin"), nullable=False
    )
    correction_mode: Mapped[CorrectionMode | None] = mapped_column(
        _persisted_enum(CorrectionMode, "claim_correction_mode")
    )
    supersedes_derived_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    origin_verdict_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    suppressed_by_derived_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    suppression_override_verdict_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    normalized_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    authorization_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authorization_policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    safety_assessment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    safety_allows_proactive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subject_verification_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["vault_id", "derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_claim_version_vault_derived_object",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "supersedes_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_claim_version_vault_supersedes_derived",
        ),
        ForeignKeyConstraint(
            ["vault_id", "suppressed_by_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_claim_version_vault_suppressed_by_derived",
        ),
        ForeignKeyConstraint(
            ["vault_id", "claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_claim_version_vault_memory_claim",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_claim_version_vault_model_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "vault_id",
            "claim_id",
            "version_no",
            name="uq_claim_version_vault_claim_version",
        ),
        CheckConstraint("version_no > 0", name="claim_version_number_positive"),
        CheckConstraint(
            "length(normalized_fingerprint) = 64",
            name="claim_version_fingerprint_sha256_length",
        ),
        CheckConstraint(
            "authorization_policy_epoch >= 0 AND authorization_source_generation >= 0",
            name="claim_version_authorization_fence_nonnegative",
        ),
        CheckConstraint(
            "length(pipeline_version) BETWEEN 1 AND 64",
            name="claim_version_pipeline_id_length",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from < valid_to",
            name="claim_version_valid_interval_nonempty",
        ),
        CheckConstraint(
            "system_to IS NULL OR system_from < system_to",
            name="claim_version_system_interval_nonempty",
        ),
        Index(
            "uq_claim_version_current",
            "vault_id",
            "claim_id",
            unique=True,
            postgresql_where=text("system_to IS NULL"),
            sqlite_where=text("system_to IS NULL"),
        ),
        Index(
            "ix_claim_version_bitemporal",
            "vault_id",
            "claim_id",
            "valid_from",
            "valid_to",
            "system_from",
            "system_to",
        ),
        Index(
            "ix_claim_version_vault_model_run",
            "vault_id",
            "model_run_id",
        ),
        ExcludeConstraint(
            ("vault_id", "="),
            ("claim_id", "="),
            (
                func.tstzrange(
                    literal_column("system_from"),
                    literal_column("system_to"),
                    literal_column("'[)'"),
                ),
                "&&",
            ),
            name="excl_claim_version_system_overlap",
            using="gist",
        ).ddl_if(dialect="postgresql"),
    )


class EvidenceLink(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    """A support, counterevidence, or context anchor to a Source fragment."""

    __tablename__ = "evidence_link"
    __table_args__ = (
        ForeignKeyConstraint(
            ["vault_id", "target_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_evidence_link_vault_derived_object",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "source_fragment_id"],
            ["source_fragment.vault_id", "source_fragment.id"],
            name="fk_evidence_link_vault_source_fragment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_evidence_link_vault_model_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "vault_id",
            "target_derived_object_id",
            "source_fragment_id",
            "relation",
            "quote_hash",
            name="uq_evidence_link_anchor",
        ),
        CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start",
            name="evidence_link_quote_interval_valid",
        ),
        CheckConstraint("length(quote_hash) = 64", name="evidence_link_quote_hash_sha256_length"),
        CheckConstraint(
            "length(source_content_fingerprint) = 64",
            name="evidence_link_source_fingerprint_sha256_length",
        ),
        CheckConstraint(
            "length(normalized_fingerprint) = 64",
            name="evidence_link_normalized_fingerprint_sha256_length",
        ),
        CheckConstraint(
            "source_policy_epoch >= 0 AND source_generation >= 0",
            name="evidence_link_source_fence_nonnegative",
        ),
    )

    target_derived_object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    relation: Mapped[EvidenceRelation] = mapped_column(
        _persisted_enum(EvidenceRelation, "evidence_relation"), nullable=False
    )
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_reason: Mapped[EvidenceExtractionReason] = mapped_column(
        _persisted_enum(EvidenceExtractionReason, "evidence_extraction_reason"), nullable=False
    )
    strength_band: Mapped[EvidenceStrength] = mapped_column(
        _persisted_enum(EvidenceStrength, "evidence_strength"), nullable=False
    )
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    authorization_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_data_class: Mapped[DataClass] = mapped_column(
        _persisted_enum(DataClass, "evidence_source_data_class"), nullable=False
    )
    created_by: Mapped[TechnicalActor] = mapped_column(
        _persisted_enum(TechnicalActor, "evidence_created_by"), nullable=False
    )
    data_class: Mapped[DataClass] = mapped_column(
        _persisted_enum(DataClass, "evidence_data_class"),
        nullable=False,
        default=DataClass.NORMAL,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_reason: Mapped[SourceEvidenceStatus | None] = mapped_column(
        _persisted_enum(SourceEvidenceStatus, "evidence_invalidation_reason")
    )


class UserVerdict(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """An immutable event in a per-derived-object verdict stream."""

    __tablename__ = "user_verdict"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_user_verdict_vault_id_id"),
        ForeignKeyConstraint(
            ["vault_id", "target_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_user_verdict_vault_derived_object",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "vault_id",
            "target_derived_object_id",
            "sequence_no",
            name="uq_user_verdict_target_sequence",
        ),
        CheckConstraint("sequence_no > 0", name="user_verdict_sequence_positive"),
        CheckConstraint(
            "verdict != 'correct' OR "
            "(correction_text IS NOT NULL AND length(trim(correction_text)) > 0 "
            "AND replacement_payload IS NOT NULL)",
            name="user_verdict_correction_text_required",
        ),
        CheckConstraint(
            "verdict = 'correct' OR (correction_text IS NULL AND replacement_payload IS NULL)",
            name="user_verdict_replacement_scoped",
        ),
        Index(
            "ix_user_verdict_stream",
            "vault_id",
            "target_derived_object_id",
            "sequence_no",
        ),
    )

    target_derived_object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[VerdictType] = mapped_column(
        _persisted_enum(VerdictType, "user_verdict_type"), nullable=False
    )
    correction_text: Mapped[str | None] = mapped_column(Text)
    replacement_payload: Mapped[dict[str, Any] | None] = mapped_column(
        _OPTIONAL_STRUCTURED_PAYLOAD_TYPE
    )
    reason_optional: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    created_by: Mapped[TechnicalActor] = mapped_column(
        _persisted_enum(TechnicalActor, "verdict_created_by"),
        nullable=False,
        default=TechnicalActor.USER,
    )
    data_class: Mapped[DataClass] = mapped_column(
        _persisted_enum(DataClass, "verdict_data_class"),
        nullable=False,
        default=DataClass.NORMAL,
    )


class MemorySuppression(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Append-only cross-UUID suppression keyed by normalized claim content."""

    __tablename__ = "memory_suppression"
    __table_args__ = (
        ForeignKeyConstraint(
            ["vault_id", "source_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_memory_suppression_vault_derived",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["vault_id", "source_verdict_id"],
            ["user_verdict.vault_id", "user_verdict.id"],
            name="fk_memory_suppression_vault_verdict",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "vault_id", "source_verdict_id", name="uq_memory_suppression_source_verdict"
        ),
        CheckConstraint(
            "length(normalized_fingerprint) = 64",
            name="memory_suppression_fingerprint_sha256_length",
        ),
    )

    normalized_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suppression_lineage_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    source_derived_object_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_verdict_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    verdict: Mapped[VerdictType] = mapped_column(
        _persisted_enum(VerdictType, "memory_suppression_verdict"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[TechnicalActor] = mapped_column(
        _persisted_enum(TechnicalActor, "memory_suppression_created_by"), nullable=False
    )
    data_class: Mapped[DataClass] = mapped_column(
        _persisted_enum(DataClass, "memory_suppression_data_class"), nullable=False
    )


@event.listens_for(UserVerdict, "before_update", propagate=True)
def _reject_verdict_update(*_: object) -> None:
    raise AppendOnlyViolationError("UserVerdict rows are append-only")


@event.listens_for(UserVerdict, "before_delete", propagate=True)
def _reject_verdict_delete(*_: object) -> None:
    raise AppendOnlyViolationError("UserVerdict rows are append-only")


@event.listens_for(MemorySuppression, "before_update", propagate=True)
@event.listens_for(MemorySuppression, "before_delete", propagate=True)
def _reject_suppression_mutation(*_: object) -> None:
    raise AppendOnlyViolationError("MemorySuppression rows are append-only")


@event.listens_for(Session, "do_orm_execute")
def _reject_verdict_bulk_mutation(execute_state: Any) -> None:
    if not (execute_state.is_update or execute_state.is_delete):
        return
    mapper = getattr(execute_state, "bind_mapper", None)
    table = getattr(execute_state.statement, "table", None)
    immutable_classes = {UserVerdict, MemorySuppression}
    immutable_tables = {
        (UserVerdict.__tablename__, UserVerdict.__table__.schema),
        (MemorySuppression.__tablename__, MemorySuppression.__table__.schema),
    }
    targets_immutable = (
        mapper is not None and getattr(mapper, "class_", None) in immutable_classes
    ) or (
        table is not None
        and any(
            getattr(table, "name", None) == table_name
            and getattr(table, "schema", None) == table_schema
            for table_name, table_schema in immutable_tables
        )
    )
    if targets_immutable:
        raise AppendOnlyViolationError("Knowledge governance rows are append-only")
