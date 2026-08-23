"""Pure synchronous Session services for the source vault.

The service owns no transaction: every operation flushes and never commits.  This lets the
composition root atomically add privacy-critical jobs/outbox rows alongside source changes.
All reads and relationships are explicitly scoped by ``vault_id``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from life_coach.modules.consent import ConsentPurpose, require_consent
from life_coach.modules.identity.models import CreatedBy, DataClass, Vault
from life_coach.modules.identity.service import (
    VaultSnapshot,
    get_vault,
    increment_policy_epoch,
    increment_source_generation,
    require_current_snapshot,
)
from life_coach.shared.database import utc_now

from .contracts import (
    DeletionSink,
    DeletionStep,
    SourceDeletionPlan,
    SourceReadResult,
    SourceWriteResult,
)
from .exceptions import InvalidSourceData, RevisionConflict, SourceDeleted, SourceNotFound
from .models import (
    EditOrigin,
    FragmentKind,
    IndexPolicy,
    ProcessingState,
    SearchProjection,
    SourceDocument,
    SourceFragment,
    SourceOrigin,
    SourceRevision,
    SourceType,
)

DELETION_SINKS: tuple[DeletionSink, ...] = tuple(DeletionSink)

_DATA_CLASS_RANK = {
    DataClass.NORMAL: 0,
    DataClass.SENSITIVE: 1,
    DataClass.HIGHLY_SENSITIVE: 2,
}


def _coerce_enum[T](enum_type: type[T], value: T | str, field_name: str) -> T:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        raise InvalidSourceData(f"invalid {field_name}") from exc


def _most_sensitive(*classes: DataClass) -> DataClass:
    return max(classes, key=_DATA_CLASS_RANK.__getitem__)


def _validate_nonblank(value: str, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidSourceData(f"{field_name} must be a non-blank string up to {maximum} chars")
    return value


def _validate_ciphertext(value: bytes | None, field_name: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, bytes) or not value:
        raise InvalidSourceData(f"{field_name} must be non-empty opaque bytes")
    return value


def _validate_revision_storage(
    *,
    content_ciphertext: bytes | None,
    object_key: str | None,
    content_hash: str,
    content_mime: str,
) -> tuple[bytes | None, str | None, str, str]:
    ciphertext = _validate_ciphertext(content_ciphertext, "content_ciphertext")
    if object_key is not None:
        object_key = _validate_nonblank(object_key, "object_key", maximum=1024)
    if ciphertext is None and object_key is None:
        raise InvalidSourceData("a ciphertext or vault-bound object key is required")
    content_hash = _validate_nonblank(content_hash, "content_hash", maximum=128)
    content_mime = _validate_nonblank(content_mime, "content_mime", maximum=255)
    return ciphertext, object_key, content_hash, content_mime


def _validate_range(start: int | None, end: int | None, field_name: str) -> None:
    if (start is None) != (end is None):
        raise InvalidSourceData(f"{field_name} start and end must be supplied together")
    if start is not None and (start < 0 or end is None or end < start):
        raise InvalidSourceData(f"invalid {field_name} range")


def _as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidSourceData(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def _lock_vault(session: Session, vault_id: uuid.UUID) -> Vault:
    return get_vault(session, vault_id, for_update=True)


def _document_statement(
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    include_deleted: bool,
) -> Select[tuple[SourceDocument]]:
    statement = select(SourceDocument).where(
        SourceDocument.vault_id == vault_id,
        SourceDocument.id == document_id,
    )
    if not include_deleted:
        statement = statement.where(SourceDocument.deleted_at.is_(None))
    return statement


def get_source_document(
    session: Session,
    *,
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
    include_deleted: bool = False,
) -> SourceDocument:
    """Read a document only inside ``vault_id``; cross-vault IDs look absent."""

    get_vault(session, vault_id)
    document = session.scalar(
        _document_statement(vault_id, document_id, include_deleted=include_deleted)
    )
    if document is None:
        raise SourceNotFound("source document is unavailable")
    if document.deleted_at is not None and not include_deleted:
        raise SourceDeleted("source document has been deleted")
    return document


def list_source_documents(
    session: Session,
    *,
    vault_id: uuid.UUID,
    limit: int = 100,
    before_created_at: datetime | None = None,
    source_type: SourceType | str | None = None,
) -> list[SourceDocument]:
    """List the live source timeline for exactly one vault."""

    if limit < 1 or limit > 500:
        raise InvalidSourceData("limit must be between 1 and 500")
    get_vault(session, vault_id)
    statement = select(SourceDocument).where(
        SourceDocument.vault_id == vault_id,
        SourceDocument.deleted_at.is_(None),
    )
    if before_created_at is not None:
        before_created_at = _as_utc(before_created_at, "before_created_at")
        statement = statement.where(SourceDocument.created_at < before_created_at)
    if source_type is not None:
        source_type = _coerce_enum(SourceType, source_type, "source_type")
        statement = statement.where(SourceDocument.source_type == source_type)
    statement = statement.order_by(SourceDocument.created_at.desc(), SourceDocument.id.desc())
    return list(session.scalars(statement.limit(limit)))


def get_source_revision(
    session: Session,
    *,
    vault_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> SourceRevision:
    """Read a revision only while its owning document remains live."""

    get_vault(session, vault_id)
    revision = session.scalar(
        select(SourceRevision)
        .join(
            SourceDocument,
            (SourceDocument.vault_id == SourceRevision.vault_id)
            & (SourceDocument.id == SourceRevision.document_id),
        )
        .where(
            SourceRevision.vault_id == vault_id,
            SourceRevision.id == revision_id,
            SourceRevision.deleted_at.is_(None),
            SourceDocument.deleted_at.is_(None),
        )
    )
    if revision is None:
        raise SourceNotFound("source revision is unavailable")
    return revision


def read_source_document(
    session: Session,
    *,
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
) -> SourceReadResult:
    """Read a live document and its current immutable revision."""

    document = get_source_document(session, vault_id=vault_id, document_id=document_id)
    if document.current_revision_id is None:
        raise SourceNotFound("source current revision is unavailable")
    revision = session.scalar(
        select(SourceRevision).where(
            SourceRevision.vault_id == vault_id,
            SourceRevision.document_id == document_id,
            SourceRevision.id == document.current_revision_id,
            SourceRevision.deleted_at.is_(None),
        )
    )
    if revision is None:
        raise SourceNotFound("source current revision is unavailable")
    return SourceReadResult(document=document, revision=revision)


def create_source_document(
    session: Session,
    *,
    vault_id: uuid.UUID,
    content_ciphertext: bytes | None,
    content_hash: str,
    content_mime: str,
    object_key: str | None = None,
    source_type: SourceType | str = SourceType.NOTE,
    origin: SourceOrigin | str = SourceOrigin.FIRST_PARTY,
    title: str | None = None,
    event_time_hint: datetime | None = None,
    capture_timezone: str = "UTC",
    processing_state: ProcessingState | str = ProcessingState.PENDING,
    retention_policy_id: uuid.UUID | None = None,
    language: str | None = None,
    edit_origin: EditOrigin | str = EditOrigin.USER,
    created_by: CreatedBy | str = CreatedBy.USER,
    data_class: DataClass | str = DataClass.SENSITIVE,
) -> SourceWriteResult:
    """Atomically create a logical document and immutable revision 1.

    ``content_ciphertext`` must already be encrypted opaque bytes.  This service does not
    encode plaintext and does not claim to implement encryption or E2EE.
    """

    ciphertext, object_key, content_hash, content_mime = _validate_revision_storage(
        content_ciphertext=content_ciphertext,
        object_key=object_key,
        content_hash=content_hash,
        content_mime=content_mime,
    )
    capture_timezone = _validate_nonblank(capture_timezone, "capture_timezone", maximum=64)
    if language is not None:
        language = _validate_nonblank(language, "language", maximum=35)
    if title is not None and not isinstance(title, str):
        raise InvalidSourceData("title must be a string")
    if event_time_hint is not None:
        event_time_hint = _as_utc(event_time_hint, "event_time_hint")
    source_type = _coerce_enum(SourceType, source_type, "source_type")
    origin = _coerce_enum(SourceOrigin, origin, "origin")
    processing_state = _coerce_enum(ProcessingState, processing_state, "processing_state")
    edit_origin = _coerce_enum(EditOrigin, edit_origin, "edit_origin")
    created_by = _coerce_enum(CreatedBy, created_by, "created_by")
    data_class = _coerce_enum(DataClass, data_class, "data_class")
    if data_class is DataClass.HIGHLY_SENSITIVE and title is not None:
        raise InvalidSourceData("title must be omitted for highly sensitive sources")

    _lock_vault(session, vault_id)
    document = SourceDocument(
        vault_id=vault_id,
        source_type=source_type,
        origin=origin,
        current_revision_id=None,
        title=title,
        event_time_hint=event_time_hint,
        capture_timezone=capture_timezone,
        processing_state=processing_state,
        retention_policy_id=retention_policy_id,
        created_by=created_by,
        data_class=data_class,
        deleted_at=None,
    )
    session.add(document)
    session.flush()

    revision = SourceRevision(
        vault_id=vault_id,
        document_id=document.id,
        revision_no=1,
        content_ciphertext=ciphertext,
        object_key=object_key,
        content_mime=content_mime,
        content_hash=content_hash,
        language=language,
        supersedes_revision_id=None,
        edit_origin=edit_origin,
        created_by=created_by,
        data_class=data_class,
        deleted_at=None,
    )
    session.add(revision)
    session.flush()
    document.current_revision_id = revision.id
    source_generation = increment_source_generation(session, vault_id)
    session.flush()
    return SourceWriteResult(
        document=document,
        revision=revision,
        source_generation=source_generation,
    )


def append_source_revision(
    session: Session,
    *,
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
    expected_revision: int,
    content_ciphertext: bytes | None,
    content_hash: str,
    content_mime: str,
    object_key: str | None = None,
    language: str | None = None,
    edit_origin: EditOrigin | str = EditOrigin.USER,
    created_by: CreatedBy | str = CreatedBy.USER,
    data_class: DataClass | str | None = None,
) -> SourceWriteResult:
    """Append a revision under a row lock and optimistic revision precondition."""

    if expected_revision < 1:
        raise InvalidSourceData("expected_revision must be positive")
    ciphertext, object_key, content_hash, content_mime = _validate_revision_storage(
        content_ciphertext=content_ciphertext,
        object_key=object_key,
        content_hash=content_hash,
        content_mime=content_mime,
    )
    if language is not None:
        language = _validate_nonblank(language, "language", maximum=35)
    edit_origin = _coerce_enum(EditOrigin, edit_origin, "edit_origin")
    created_by = _coerce_enum(CreatedBy, created_by, "created_by")

    _lock_vault(session, vault_id)
    document = session.scalar(
        _document_statement(vault_id, document_id, include_deleted=True).with_for_update()
    )
    if document is None:
        raise SourceNotFound("source document is unavailable")
    if document.deleted_at is not None:
        raise SourceDeleted("source document has been deleted")
    if document.current_revision_id is None:
        raise SourceNotFound("source current revision is unavailable")

    current_revision = session.scalar(
        select(SourceRevision).where(
            SourceRevision.vault_id == vault_id,
            SourceRevision.document_id == document_id,
            SourceRevision.id == document.current_revision_id,
            SourceRevision.deleted_at.is_(None),
        )
    )
    if current_revision is None:
        raise SourceNotFound("source current revision is unavailable")
    if current_revision.revision_no != expected_revision:
        raise RevisionConflict(
            expected_revision=expected_revision,
            current_revision=current_revision.revision_no,
        )

    requested_data_class = (
        current_revision.data_class
        if data_class is None
        else _coerce_enum(DataClass, data_class, "data_class")
    )
    effective_data_class = _most_sensitive(
        document.data_class,
        current_revision.data_class,
        requested_data_class,
    )
    if effective_data_class is DataClass.HIGHLY_SENSITIVE:
        document.title = None
    revision = SourceRevision(
        vault_id=vault_id,
        document_id=document_id,
        revision_no=current_revision.revision_no + 1,
        content_ciphertext=ciphertext,
        object_key=object_key,
        content_mime=content_mime,
        content_hash=content_hash,
        language=language,
        supersedes_revision_id=current_revision.id,
        edit_origin=edit_origin,
        created_by=created_by,
        data_class=effective_data_class,
        deleted_at=None,
    )
    session.add(revision)
    session.flush()
    document.current_revision_id = revision.id
    document.data_class = effective_data_class
    source_generation = increment_source_generation(session, vault_id)
    session.flush()
    return SourceWriteResult(
        document=document,
        revision=revision,
        source_generation=source_generation,
    )


def create_source_fragment(
    session: Session,
    *,
    vault_id: uuid.UUID,
    revision_id: uuid.UUID,
    ordinal: int,
    text_ciphertext: bytes,
    text_hash: str,
    fragment_kind: FragmentKind | str,
    char_start: int | None = None,
    char_end: int | None = None,
    audio_start_ms: int | None = None,
    audio_end_ms: int | None = None,
    created_by: CreatedBy | str = CreatedBy.SYSTEM_COMPONENT,
    data_class: DataClass | str | None = None,
) -> SourceFragment:
    """Create an encrypted fragment for a live revision within the same vault."""

    if ordinal < 0:
        raise InvalidSourceData("ordinal must be nonnegative")
    ciphertext = _validate_ciphertext(text_ciphertext, "text_ciphertext")
    if ciphertext is None:  # narrowed defensively for static type checkers
        raise InvalidSourceData("text_ciphertext is required")
    text_hash = _validate_nonblank(text_hash, "text_hash", maximum=128)
    _validate_range(char_start, char_end, "character")
    _validate_range(audio_start_ms, audio_end_ms, "audio")
    fragment_kind = _coerce_enum(FragmentKind, fragment_kind, "fragment_kind")
    created_by = _coerce_enum(CreatedBy, created_by, "created_by")

    _lock_vault(session, vault_id)
    revision = session.scalar(
        select(SourceRevision)
        .join(
            SourceDocument,
            (SourceDocument.vault_id == SourceRevision.vault_id)
            & (SourceDocument.id == SourceRevision.document_id),
        )
        .where(
            SourceRevision.vault_id == vault_id,
            SourceRevision.id == revision_id,
            SourceRevision.deleted_at.is_(None),
            SourceDocument.deleted_at.is_(None),
        )
    )
    if revision is None:
        raise SourceNotFound("source revision is unavailable")
    requested_data_class = (
        revision.data_class
        if data_class is None
        else _coerce_enum(DataClass, data_class, "data_class")
    )
    effective_data_class = _most_sensitive(revision.data_class, requested_data_class)
    fragment = SourceFragment(
        vault_id=vault_id,
        revision_id=revision_id,
        ordinal=ordinal,
        char_start=char_start,
        char_end=char_end,
        audio_start_ms=audio_start_ms,
        audio_end_ms=audio_end_ms,
        text_ciphertext=ciphertext,
        text_hash=text_hash,
        fragment_kind=fragment_kind,
        created_by=created_by,
        data_class=effective_data_class,
        deleted_at=None,
    )
    session.add(fragment)
    session.flush()
    return fragment


def create_search_projection(
    session: Session,
    *,
    vault_id: uuid.UUID,
    source_fragment_id: uuid.UUID,
    index_policy: IndexPolicy | str,
    lexical_terms: Sequence[str] | None = None,
    embedding: Sequence[float] | None = None,
    tokenizer_version: str | None = None,
    embedding_version: str | None = None,
    snapshot: VaultSnapshot,
    expected_source_generation: int | None = None,
    created_by: CreatedBy | str = CreatedBy.SYSTEM_COMPONENT,
) -> SearchProjection:
    """Create a sensitive server-readable projection for one live fragment.

    A current full vault snapshot and SEARCH-purpose grant are mandatory. Highly-sensitive
    fragments always produce a ``none`` projection with no terms or embedding, regardless
    of caller input. The resulting index is controlled server-readable data, not E2EE.
    """

    index_policy = _coerce_enum(IndexPolicy, index_policy, "index_policy")
    created_by = _coerce_enum(CreatedBy, created_by, "created_by")
    _lock_vault(session, vault_id)
    if snapshot.vault_id != vault_id:
        raise InvalidSourceData("snapshot belongs to a different vault")
    require_current_snapshot(session, snapshot)
    fragment_row = session.execute(
        select(SourceFragment, SourceRevision.document_id)
        .join(
            SourceRevision,
            (SourceRevision.vault_id == SourceFragment.vault_id)
            & (SourceRevision.id == SourceFragment.revision_id),
        )
        .join(
            SourceDocument,
            (SourceDocument.vault_id == SourceRevision.vault_id)
            & (SourceDocument.id == SourceRevision.document_id),
        )
        .where(
            SourceFragment.vault_id == vault_id,
            SourceFragment.id == source_fragment_id,
            SourceFragment.deleted_at.is_(None),
            SourceRevision.deleted_at.is_(None),
            SourceDocument.deleted_at.is_(None),
        )
    ).one_or_none()
    if fragment_row is None:
        raise SourceNotFound("source fragment is unavailable")
    fragment, document_id = fragment_row
    require_consent(
        session,
        vault_id=vault_id,
        purpose=ConsentPurpose.SEARCH,
        source_document_id=document_id,
    )
    if (
        expected_source_generation is not None
        and snapshot.source_generation != expected_source_generation
    ):
        raise RevisionConflict(
            expected_revision=expected_source_generation,
            current_revision=snapshot.source_generation,
        )

    terms = None if lexical_terms is None else list(lexical_terms)
    vector = None if embedding is None else [float(value) for value in embedding]
    if vector is not None and any(not math.isfinite(value) for value in vector):
        raise InvalidSourceData("embedding values must be finite")
    if terms is not None and any(not isinstance(term, str) or not term for term in terms):
        raise InvalidSourceData("lexical_terms must contain non-empty strings")
    if tokenizer_version is not None:
        tokenizer_version = _validate_nonblank(tokenizer_version, "tokenizer_version", maximum=128)
    if embedding_version is not None:
        embedding_version = _validate_nonblank(embedding_version, "embedding_version", maximum=128)

    if fragment.data_class is DataClass.HIGHLY_SENSITIVE:
        index_policy = IndexPolicy.NONE
    if index_policy is IndexPolicy.NONE:
        terms = None
        vector = None
        tokenizer_version = None
        embedding_version = None
    elif index_policy is IndexPolicy.LEXICAL:
        vector = None
        embedding_version = None
    elif index_policy is IndexPolicy.SEMANTIC:
        terms = None
        tokenizer_version = None

    projection_data_class = _most_sensitive(DataClass.SENSITIVE, fragment.data_class)
    projection = SearchProjection(
        vault_id=vault_id,
        source_fragment_id=source_fragment_id,
        lexical_terms=terms,
        embedding=vector,
        tokenizer_version=tokenizer_version,
        embedding_version=embedding_version,
        source_generation=snapshot.source_generation,
        index_policy=index_policy,
        created_by=created_by,
        data_class=projection_data_class,
        deleted_at=None,
    )
    session.add(projection)
    session.flush()
    return projection


def _build_deletion_plan(
    *,
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
    tombstoned_at: datetime,
    policy_epoch: int,
    source_generation: int,
) -> SourceDeletionPlan:
    return SourceDeletionPlan(
        vault_id=vault_id,
        document_id=document_id,
        tombstoned_at=tombstoned_at,
        policy_epoch=policy_epoch,
        source_generation=source_generation,
        steps=tuple(DeletionStep(sink=sink) for sink in DELETION_SINKS),
    )


def build_source_deletion_plan(
    session: Session,
    *,
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
) -> SourceDeletionPlan:
    """Build a complete content-free plan for an already tombstoned document."""

    document = get_source_document(
        session,
        vault_id=vault_id,
        document_id=document_id,
        include_deleted=True,
    )
    if document.deleted_at is None:
        raise InvalidSourceData("source document must be tombstoned before deletion planning")
    vault = get_vault(session, vault_id)
    return _build_deletion_plan(
        vault_id=vault_id,
        document_id=document_id,
        tombstoned_at=document.deleted_at,
        policy_epoch=vault.policy_epoch,
        source_generation=vault.source_generation,
    )


def tombstone_source_document(
    session: Session,
    *,
    vault_id: uuid.UUID,
    document_id: uuid.UUID,
) -> SourceDeletionPlan:
    """Immediately isolate a source and return its asynchronous erase plan.

    The operation is idempotent after the first tombstone.  On first execution it hides all
    fragments/projections, clears service-readable projection payloads, and advances both
    policy and source fences.  It deliberately does not mutate immutable revisions.
    """

    vault = _lock_vault(session, vault_id)
    document = session.scalar(
        _document_statement(vault_id, document_id, include_deleted=True).with_for_update()
    )
    if document is None:
        raise SourceNotFound("source document is unavailable")
    if document.deleted_at is not None:
        return _build_deletion_plan(
            vault_id=vault_id,
            document_id=document_id,
            tombstoned_at=document.deleted_at,
            policy_epoch=vault.policy_epoch,
            source_generation=vault.source_generation,
        )

    tombstoned_at = utc_now()
    revision_ids = select(SourceRevision.id).where(
        SourceRevision.vault_id == vault_id,
        SourceRevision.document_id == document_id,
    )
    fragment_ids = select(SourceFragment.id).where(
        SourceFragment.vault_id == vault_id,
        SourceFragment.revision_id.in_(revision_ids),
    )
    session.execute(
        update(SearchProjection)
        .where(
            SearchProjection.vault_id == vault_id,
            SearchProjection.source_fragment_id.in_(fragment_ids),
            SearchProjection.deleted_at.is_(None),
        )
        .values(
            deleted_at=tombstoned_at,
            updated_at=tombstoned_at,
            lexical_terms=None,
            embedding=None,
            tokenizer_version=None,
            embedding_version=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    session.execute(
        update(SourceFragment)
        .where(
            SourceFragment.vault_id == vault_id,
            SourceFragment.revision_id.in_(revision_ids),
            SourceFragment.deleted_at.is_(None),
        )
        .values(deleted_at=tombstoned_at, updated_at=tombstoned_at)
        .execution_options(synchronize_session="fetch")
    )
    document.deleted_at = tombstoned_at
    policy_epoch = increment_policy_epoch(session, vault_id)
    source_generation = increment_source_generation(session, vault_id)
    session.flush()
    return _build_deletion_plan(
        vault_id=vault_id,
        document_id=document_id,
        tombstoned_at=tombstoned_at,
        policy_epoch=policy_epoch,
        source_generation=source_generation,
    )


class SourceService:
    """Small injectable facade over the module-level Session functions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_document(self, **kwargs: object) -> SourceWriteResult:
        return create_source_document(self.session, **kwargs)  # type: ignore[arg-type]

    def append_revision(self, **kwargs: object) -> SourceWriteResult:
        return append_source_revision(self.session, **kwargs)  # type: ignore[arg-type]

    def read_document(self, **kwargs: object) -> SourceReadResult:
        return read_source_document(self.session, **kwargs)  # type: ignore[arg-type]

    def list_documents(self, **kwargs: object) -> list[SourceDocument]:
        return list_source_documents(self.session, **kwargs)  # type: ignore[arg-type]

    def tombstone_document(self, **kwargs: object) -> SourceDeletionPlan:
        return tombstone_source_document(self.session, **kwargs)  # type: ignore[arg-type]
