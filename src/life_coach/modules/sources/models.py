"""SQLAlchemy models for user-authored source records and search projections.

A SourceRevision records what a user or importer stored.  It is evidence that the record
was made; it is not a claim that the described event objectively happened.

``SearchProjection`` is deliberately service-readable derived index data.  Lexical terms
and embeddings remain sensitive personal data protected by vault isolation, access policy,
retention, and deletion controls.  This server-side indexing design is not E2EE.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum, StrEnum
from typing import cast

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from life_coach.modules.identity.models import CreatedBy, DataClass
from life_coach.shared.database import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
    utc_now,
)

from .exceptions import ImmutableRevisionError
from .object_reference import VaultObjectKeyType, VaultObjectReference


def _enum_values(enum_type: type[Enum]) -> list[str]:
    return [str(member.value) for member in enum_type]


def _enum_column(enum_type: type[Enum], *, name: str, length: int) -> SAEnum:
    """Persist public enum values rather than Python member names."""

    return SAEnum(
        enum_type,
        name=name,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=_enum_values,
    )


class SourceType(StrEnum):
    NOTE = "note"
    CONVERSATION = "conversation"
    AUDIO = "audio"
    IMAGE = "image"
    FILE = "file"
    IMPORT = "import"
    CORRECTION = "correction"


class SourceOrigin(StrEnum):
    FIRST_PARTY = "first_party"
    USER_UPLOAD = "user_upload"
    CONNECTOR = "connector"


class ProcessingState(StrEnum):
    READY = "ready"
    PENDING = "pending"
    PARTIAL = "partial"
    FAILED = "failed"


class EditOrigin(StrEnum):
    USER = "user"
    IMPORT_REPLAY = "import_replay"
    TRANSCRIPTION_CORRECTION = "transcription_correction"


class FragmentKind(StrEnum):
    PARAGRAPH = "paragraph"
    UTTERANCE = "utterance"
    PAGE = "page"
    CAPTION = "caption"


class IndexPolicy(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    BOTH = "both"
    NONE = "none"


class _SourceRecordMixin:
    """Fields shared by mutable, vault-scoped source records."""

    created_by: Mapped[CreatedBy] = mapped_column(
        _enum_column(CreatedBy, name="created_by", length=32), nullable=False
    )
    data_class: Mapped[DataClass] = mapped_column(
        _enum_column(DataClass, name="data_class", length=32), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceDocument(
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
    TimestampMixin,
    _SourceRecordMixin,
    Base,
):
    """Stable logical source object whose content lives in immutable revisions."""

    __tablename__ = "source_document"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_source_document_vault_id_id"),
        ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_source_document_vault"),
        ForeignKeyConstraint(
            ["vault_id", "id", "current_revision_id"],
            [
                "source_revision.vault_id",
                "source_revision.document_id",
                "source_revision.id",
            ],
            name="fk_source_document_current_revision_same_document",
            use_alter=True,
        ),
        CheckConstraint(
            "data_class <> 'highly_sensitive' OR title IS NULL",
            name="source_document_highly_sensitive_title_omitted",
        ),
        Index("ix_source_document_vault_live_created", "vault_id", "deleted_at", "created_at"),
    )

    source_type: Mapped[SourceType] = mapped_column(
        _enum_column(SourceType, name="source_type", length=24), nullable=False
    )
    origin: Mapped[SourceOrigin] = mapped_column(
        _enum_column(SourceOrigin, name="source_origin", length=24), nullable=False
    )
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_time_hint: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capture_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_state: Mapped[ProcessingState] = mapped_column(
        _enum_column(ProcessingState, name="source_processing_state", length=16), nullable=False
    )
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class SourceRevision(UUIDPrimaryKeyMixin, VaultScopedMixin, _SourceRecordMixin, Base):
    """Append-only encrypted/object-backed content revision.

    There is intentionally no ``updated_at`` column.  ORM updates are rejected below;
    corrections must append another revision linked through ``supersedes_revision_id``.
    """

    __tablename__ = "source_revision"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_source_revision_vault_id_id"),
        ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_source_revision_vault"),
        UniqueConstraint(
            "vault_id",
            "document_id",
            "id",
            name="uq_source_revision_vault_document_id_id",
        ),
        UniqueConstraint(
            "vault_id",
            "document_id",
            "revision_no",
            name="uq_source_revision_vault_document_revision_no",
        ),
        ForeignKeyConstraint(
            ["vault_id", "document_id"],
            ["source_document.vault_id", "source_document.id"],
            name="fk_source_revision_vault_document",
        ),
        ForeignKeyConstraint(
            ["vault_id", "document_id", "supersedes_revision_id"],
            [
                "source_revision.vault_id",
                "source_revision.document_id",
                "source_revision.id",
            ],
            name="fk_source_revision_supersedes_same_document",
        ),
        CheckConstraint("revision_no >= 1", name="source_revision_no_positive"),
        CheckConstraint(
            "content_ciphertext IS NOT NULL OR object_key IS NOT NULL",
            name="source_revision_has_storage",
        ),
        CheckConstraint(
            "object_key IS NULL OR ("
            "object_key LIKE 'vaults/' || "
            "replace(lower(CAST(vault_id AS VARCHAR(36))), '-', '') || '/objects/%' "
            "AND object_key NOT LIKE '%/../%' AND object_key NOT LIKE '%/..' "
            "AND object_key NOT LIKE '%/./%' AND object_key NOT LIKE '%/.' "
            "AND object_key NOT LIKE '%//%' AND object_key NOT LIKE '%?%' "
            "AND object_key NOT LIKE '%#%')",
            name="source_revision_object_key_matches_vault",
        ),
        Index("ix_source_revision_vault_document", "vault_id", "document_id", "revision_no"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        nullable=True,
        comment="Opaque application-produced ciphertext; never UTF-8 relabeled as encrypted",
    )
    object_key: Mapped[str | None] = mapped_column(VaultObjectKeyType(), nullable=True)
    content_mime: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    supersedes_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    edit_origin: Mapped[EditOrigin] = mapped_column(
        _enum_column(EditOrigin, name="source_edit_origin", length=32), nullable=False
    )


class SourceCommandReceipt(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Content-free idempotency receipt for one Source API mutation."""

    __tablename__ = "source_command_receipt"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_source_command_receipt_vault_id_id"),
        UniqueConstraint(
            "vault_id",
            "operation",
            "client_key_hash",
            name="uq_source_command_receipt_vault_operation_key",
        ),
        ForeignKeyConstraint(
            ["vault_id", "resource_id"],
            ["source_document.vault_id", "source_document.id"],
            name="fk_source_command_receipt_vault_document",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "length(operation) BETWEEN 1 AND 100",
            name="source_command_receipt_operation_technical",
        ),
        CheckConstraint(
            "length(client_key_hash) = 79 AND client_key_hash LIKE 'hmac-sha256:v1:%'",
            name="source_command_receipt_client_key_hash",
        ),
        CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name="source_command_receipt_request_hash",
        ),
        CheckConstraint(
            "result_revision_no IS NULL OR result_revision_no >= 1",
            name="source_command_receipt_revision_positive",
        ),
        CheckConstraint(
            "source_generation >= 0",
            name="source_command_receipt_source_generation_nonnegative",
        ),
        CheckConstraint(
            "policy_epoch >= 0",
            name="source_command_receipt_policy_epoch_nonnegative",
        ),
    )

    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    client_key_hash: Mapped[str] = mapped_column(String(79), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(79), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    result_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


@event.listens_for(SourceRevision, "before_update", propagate=True)
def _reject_source_revision_update(
    _mapper: object, _connection: object, _target: SourceRevision
) -> None:
    raise ImmutableRevisionError("source revisions are immutable; append a new revision")


@event.listens_for(SourceRevision, "before_insert", propagate=True)
def _validate_source_revision_object_reference(
    _mapper: object, _connection: object, target: SourceRevision
) -> None:
    if target.object_key is not None:
        VaultObjectReference(
            vault_id=cast(uuid.UUID, target.vault_id),
            object_key=target.object_key,
        )


class SourceFragment(
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
    TimestampMixin,
    _SourceRecordMixin,
    Base,
):
    """Encrypted fragment anchored to one immutable revision."""

    __tablename__ = "source_fragment"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_source_fragment_vault_id_id"),
        ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_source_fragment_vault"),
        UniqueConstraint(
            "vault_id",
            "revision_id",
            "ordinal",
            name="uq_source_fragment_vault_revision_ordinal",
        ),
        ForeignKeyConstraint(
            ["vault_id", "revision_id"],
            ["source_revision.vault_id", "source_revision.id"],
            name="fk_source_fragment_vault_revision",
        ),
        CheckConstraint("ordinal >= 0", name="source_fragment_ordinal_nonnegative"),
        CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL) OR "
            "(char_start IS NOT NULL AND char_end IS NOT NULL "
            "AND char_start >= 0 AND char_end >= char_start)",
            name="source_fragment_valid_char_range",
        ),
        CheckConstraint(
            "(audio_start_ms IS NULL AND audio_end_ms IS NULL) OR "
            "(audio_start_ms IS NOT NULL AND audio_end_ms IS NOT NULL "
            "AND audio_start_ms >= 0 AND audio_end_ms >= audio_start_ms)",
            name="source_fragment_valid_audio_range",
        ),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="Opaque application-produced ciphertext; never plaintext masquerading as bytes",
    )
    text_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    fragment_kind: Mapped[FragmentKind] = mapped_column(
        _enum_column(FragmentKind, name="source_fragment_kind", length=16), nullable=False
    )


class SearchProjection(
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
    TimestampMixin,
    _SourceRecordMixin,
    Base,
):
    """Sensitive, server-readable, independently deletable derived search index.

    This table is not ciphertext and must not be represented as E2EE.  It requires a
    restricted database role/RLS and the same deletion discipline as source content.
    """

    __tablename__ = "search_projection"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_search_projection_vault_id_id"),
        ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_search_projection_vault"),
        UniqueConstraint(
            "vault_id",
            "source_fragment_id",
            name="uq_search_projection_vault_fragment",
        ),
        ForeignKeyConstraint(
            ["vault_id", "source_fragment_id"],
            ["source_fragment.vault_id", "source_fragment.id"],
            name="fk_search_projection_vault_fragment",
        ),
        ForeignKeyConstraint(
            ["vault_id", "consent_record_id"],
            ["consent_record.vault_id", "consent_record.id"],
            name="fk_search_projection_vault_consent_record",
        ),
        CheckConstraint("source_generation >= 0", name="search_projection_generation_nonnegative"),
        CheckConstraint("policy_epoch > 0", name="search_projection_policy_epoch_positive"),
        CheckConstraint(
            "index_policy <> 'none' OR "
            "(lexical_terms IS NULL AND embedding IS NULL "
            "AND tokenizer_version IS NULL AND embedding_version IS NULL)",
            name="search_projection_none_has_no_payload",
        ),
        CheckConstraint(
            "data_class <> 'highly_sensitive' OR "
            "(index_policy = 'none' AND lexical_terms IS NULL AND embedding IS NULL "
            "AND tokenizer_version IS NULL AND embedding_version IS NULL)",
            name="search_projection_highly_sensitive_no_index",
        ),
        Index("ix_search_projection_vault_live", "vault_id", "deleted_at"),
    )

    source_fragment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lexical_terms: Mapped[list[str] | None] = mapped_column(
        JSON(none_as_null=True),
        nullable=True,
        comment="Sensitive service-readable derived terms, not E2EE",
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        JSON(none_as_null=True),
        nullable=True,
        comment="Sensitive service-readable derived vector, not E2EE",
    )
    tokenizer_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    consent_record_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    index_policy: Mapped[IndexPolicy] = mapped_column(
        _enum_column(IndexPolicy, name="search_index_policy", length=16), nullable=False
    )


@event.listens_for(Session, "do_orm_execute")
def _reject_source_revision_bulk_update(execute_state: ORMExecuteState) -> None:
    """Reject SQLAlchemy bulk UPDATE paths that bypass mapper update events."""

    target_table = getattr(execute_state.statement, "table", None)
    is_revision_target = (
        execute_state.bind_mapper is SourceRevision.__mapper__
        or target_table is SourceRevision.__table__
        or getattr(target_table, "original", None) is SourceRevision.__table__
    )
    if execute_state.is_update and is_revision_target:
        raise ImmutableRevisionError("source revisions are immutable; append a new revision")
