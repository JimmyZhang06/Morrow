from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from life_coach.modules.consent import (
    ConsentAction,
    ConsentDenied,
    ConsentPurpose,
    UserConsentCommand,
    grant_consent,
    revoke_consent,
)
from life_coach.modules.identity import (
    CreatedBy,
    DataClass,
    StaleVaultSnapshot,
    VaultNotFound,
    capture_snapshot,
    create_vault,
)
from life_coach.modules.sources import (
    DELETION_SINKS,
    EditOrigin,
    ImmutableRevisionError,
    IndexPolicy,
    InvalidSourceData,
    SearchProjection,
    SourceNotFound,
    SourceRevision,
    SourceType,
    SourceWriteResult,
    VaultObjectReference,
    append_source_revision,
    create_search_projection,
    create_source_document,
    create_source_fragment,
    get_source_document,
    get_source_revision,
    list_search_projections,
    list_source_documents,
    read_source_document,
    tombstone_source_document,
)
from life_coach.shared.database import Base, utc_now


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    # Consent adds a real Source FK, so import its model before creating shared metadata.
    from life_coach.modules.consent.models import ConsentRecord  # noqa: F401

    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def _create_source(
    session: Session, *, vault_id: UUID, data_class: DataClass = DataClass.SENSITIVE
) -> SourceWriteResult:
    return create_source_document(
        session,
        vault_id=vault_id,
        content_ciphertext=b"\x01protected-revision-one",
        content_hash="hash-one",
        content_mime="text/plain",
        capture_timezone="Asia/Shanghai",
        language="zh-CN",
        data_class=data_class,
    )


def _consent_command(
    vault_id: UUID,
    *,
    action: ConsentAction,
    source_document_id: UUID | None = None,
) -> UserConsentCommand:
    issued_at = datetime.now(UTC)
    return UserConsentCommand(
        vault_id=vault_id,
        principal_id=uuid4(),
        purpose=ConsentPurpose.SEARCH,
        action=action,
        interaction_id=uuid4(),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=1),
        source_document_id=source_document_id,
    )


def test_revision_is_append_only_and_existing_row_cannot_be_updated(session: Session) -> None:
    vault = create_vault(session)
    first = _create_source(session, vault_id=vault.id)
    session.commit()

    second = append_source_revision(
        session,
        vault_id=vault.id,
        document_id=first.document.id,
        expected_revision=1,
        content_ciphertext=b"\x02protected-revision-two",
        content_hash="hash-two",
        content_mime="text/plain",
        language="zh-CN",
    )

    assert second.revision.revision_no == 2
    assert second.revision.supersedes_revision_id == first.revision.id
    assert first.revision.revision_no == 1
    assert first.revision.content_hash == "hash-one"
    assert second.document.current_revision_id == second.revision.id
    session.commit()

    first.revision.content_hash = "attempted-overwrite"
    with pytest.raises(ImmutableRevisionError):
        session.flush()


def test_revision_bulk_update_is_rejected(session: Session) -> None:
    vault = create_vault(session)
    source = _create_source(session, vault_id=vault.id)
    session.commit()

    with pytest.raises(ImmutableRevisionError):
        session.execute(
            update(SourceRevision)
            .where(SourceRevision.id == source.revision.id)
            .values(content_hash="bulk-overwrite")
        )


def test_escalating_to_highly_sensitive_removes_plaintext_title(session: Session) -> None:
    vault = create_vault(session)
    source = create_source_document(
        session,
        vault_id=vault.id,
        content_ciphertext=b"\x08protected",
        content_hash="hash-one",
        content_mime="text/plain",
        title="non-sensitive heading",
    )

    result = append_source_revision(
        session,
        vault_id=vault.id,
        document_id=source.document.id,
        expected_revision=1,
        content_ciphertext=b"\x09protected-high",
        content_hash="hash-two",
        content_mime="text/plain",
        data_class=DataClass.HIGHLY_SENSITIVE,
    )

    assert result.document.data_class is DataClass.HIGHLY_SENSITIVE
    assert result.document.title is None


def test_cross_vault_reads_and_writes_are_rejected(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    source = _create_source(session, vault_id=first_vault.id)
    session.commit()

    with pytest.raises(SourceNotFound):
        get_source_document(
            session,
            vault_id=second_vault.id,
            document_id=source.document.id,
        )
    with pytest.raises(SourceNotFound):
        append_source_revision(
            session,
            vault_id=second_vault.id,
            document_id=source.document.id,
            expected_revision=1,
            content_ciphertext=b"\x03protected-cross-vault",
            content_hash="cross-vault-hash",
            content_mime="text/plain",
        )


def test_object_reference_is_canonical_and_bound_to_one_vault(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    reference = VaultObjectReference(
        vault_id=first_vault.id,
        object_key=(f"{VaultObjectReference.prefix_for(first_vault.id)}uploads/source-payload.bin"),
    )

    source = create_source_document(
        session,
        vault_id=first_vault.id,
        content_ciphertext=None,
        object_ref=reference,
        content_hash="object-hash",
        content_mime="application/octet-stream",
    )
    assert source.revision.object_key == reference.object_key

    with pytest.raises(InvalidSourceData):
        create_source_document(
            session,
            vault_id=second_vault.id,
            content_ciphertext=None,
            object_ref=reference,
            content_hash="cross-vault-object-hash",
            content_mime="application/octet-stream",
        )

    with pytest.raises(InvalidSourceData):
        VaultObjectReference(
            vault_id=first_vault.id,
            object_key=f"{VaultObjectReference.prefix_for(first_vault.id)}../private.txt",
        )


def test_database_rejects_a_cross_vault_revision_relationship(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    source = _create_source(session, vault_id=first_vault.id)
    session.commit()
    rogue_revision = SourceRevision(
        vault_id=second_vault.id,
        document_id=source.document.id,
        revision_no=2,
        content_ciphertext=b"\x05protected-rogue",
        object_key=None,
        content_mime="text/plain",
        content_hash="rogue-hash",
        language=None,
        supersedes_revision_id=None,
        edit_origin=EditOrigin.USER,
        created_by=CreatedBy.USER,
        data_class=DataClass.SENSITIVE,
        deleted_at=None,
    )
    session.add(rogue_revision)

    with pytest.raises(IntegrityError):
        session.flush()


def test_direct_revision_insert_rejects_foreign_vault_object_key(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    source = _create_source(session, vault_id=first_vault.id)
    rogue_revision = SourceRevision(
        vault_id=first_vault.id,
        document_id=source.document.id,
        revision_no=2,
        content_ciphertext=None,
        object_key=(
            f"{VaultObjectReference.prefix_for(second_vault.id)}uploads/foreign-object.bin"
        ),
        content_mime="application/octet-stream",
        content_hash="foreign-object-hash",
        language=None,
        supersedes_revision_id=source.revision.id,
        edit_origin=EditOrigin.USER,
        created_by=CreatedBy.USER,
        data_class=DataClass.SENSITIVE,
        deleted_at=None,
    )
    session.add(rogue_revision)

    with pytest.raises(InvalidSourceData):
        session.flush()


def test_core_insert_rejects_foreign_vault_object_key(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    source = _create_source(session, vault_id=first_vault.id)

    with pytest.raises(IntegrityError):
        session.execute(
            insert(SourceRevision).values(
                vault_id=first_vault.id,
                document_id=source.document.id,
                revision_no=2,
                content_ciphertext=None,
                object_key=(
                    f"{VaultObjectReference.prefix_for(second_vault.id)}uploads/foreign.bin"
                ),
                content_mime="application/octet-stream",
                content_hash="foreign-core-hash",
                language=None,
                supersedes_revision_id=source.revision.id,
                edit_origin=EditOrigin.USER,
                created_by=CreatedBy.USER,
                data_class=DataClass.SENSITIVE,
                deleted_at=None,
            )
        )


def test_current_revision_must_belong_to_the_same_document(session: Session) -> None:
    vault = create_vault(session)
    first = _create_source(session, vault_id=vault.id)
    second = _create_source(session, vault_id=vault.id)
    session.commit()

    first.document.current_revision_id = second.revision.id
    with pytest.raises(IntegrityError):
        session.flush()


def test_default_source_timeline_excludes_internal_corrections(session: Session) -> None:
    vault = create_vault(session)
    visible = _create_source(session, vault_id=vault.id)
    correction = create_source_document(
        session,
        vault_id=vault.id,
        content_ciphertext=b"\x01protected-correction",
        content_hash="correction-hash",
        content_mime="text/plain",
        source_type=SourceType.CORRECTION,
    )

    assert list_source_documents(session, vault_id=vault.id) == [visible.document]
    assert list_source_documents(
        session, vault_id=vault.id, source_type=SourceType.CORRECTION
    ) == [correction.document]


def test_vault_tombstone_hides_all_default_source_reads(session: Session) -> None:
    vault = create_vault(session)
    source = _create_source(session, vault_id=vault.id)
    vault.deleted_at = utc_now()
    session.flush()

    with pytest.raises(VaultNotFound):
        get_source_document(session, vault_id=vault.id, document_id=source.document.id)
    with pytest.raises(VaultNotFound):
        get_source_revision(session, vault_id=vault.id, revision_id=source.revision.id)
    with pytest.raises(VaultNotFound):
        read_source_document(session, vault_id=vault.id, document_id=source.document.id)
    with pytest.raises(VaultNotFound):
        list_source_documents(session, vault_id=vault.id)


def test_source_generation_and_tombstone_plan_are_complete(session: Session) -> None:
    vault = create_vault(session)
    source = _create_source(session, vault_id=vault.id)
    assert source.source_generation == 1

    appended = append_source_revision(
        session,
        vault_id=vault.id,
        document_id=source.document.id,
        expected_revision=1,
        content_ciphertext=b"\x02protected-revision-two",
        content_hash="hash-two",
        content_mime="text/plain",
    )
    assert appended.source_generation == 2

    plan = tombstone_source_document(
        session,
        vault_id=vault.id,
        document_id=source.document.id,
    )

    assert plan.source_generation == 3
    assert plan.policy_epoch == 1
    assert tuple(step.sink for step in plan.steps) == DELETION_SINKS
    with pytest.raises(SourceNotFound):
        get_source_document(session, vault_id=vault.id, document_id=source.document.id)

    repeated = tombstone_source_document(
        session,
        vault_id=vault.id,
        document_id=source.document.id,
    )
    assert repeated.source_generation == plan.source_generation
    assert repeated.policy_epoch == plan.policy_epoch


def test_highly_sensitive_source_forces_no_index_projection(session: Session) -> None:
    vault = create_vault(session)
    source = _create_source(
        session,
        vault_id=vault.id,
        data_class=DataClass.HIGHLY_SENSITIVE,
    )
    fragment = create_source_fragment(
        session,
        vault_id=vault.id,
        revision_id=source.revision.id,
        ordinal=0,
        char_start=0,
        char_end=8,
        text_ciphertext=b"\x04protected-fragment",
        text_hash="fragment-hash",
        fragment_kind="paragraph",
    )
    grant_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.GRANT),
    )
    snapshot = capture_snapshot(session, vault.id)

    projection = create_search_projection(
        session,
        vault_id=vault.id,
        source_fragment_id=fragment.id,
        index_policy=IndexPolicy.BOTH,
        lexical_terms=["小李", "害怕做不好"],
        embedding=[0.1, 0.2, 0.3],
        tokenizer_version="tokenizer-v1",
        embedding_version="embedding-v1",
        snapshot=snapshot,
    )

    assert fragment.data_class is DataClass.HIGHLY_SENSITIVE
    assert projection.data_class is DataClass.HIGHLY_SENSITIVE
    assert projection.index_policy is IndexPolicy.NONE
    assert projection.lexical_terms is None
    assert projection.embedding is None
    assert projection.tokenizer_version is None
    assert projection.embedding_version is None


def test_database_rejects_highly_sensitive_index_payload(session: Session) -> None:
    vault = create_vault(session)
    source = _create_source(
        session,
        vault_id=vault.id,
        data_class=DataClass.HIGHLY_SENSITIVE,
    )
    fragment = create_source_fragment(
        session,
        vault_id=vault.id,
        revision_id=source.revision.id,
        ordinal=0,
        text_ciphertext=b"\x06protected-fragment",
        text_hash="fragment-hash",
        fragment_kind="paragraph",
    )
    consent = grant_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.GRANT),
    )
    snapshot = capture_snapshot(session, vault.id)
    projection = SearchProjection(
        vault_id=vault.id,
        source_fragment_id=fragment.id,
        lexical_terms=["must-not-persist"],
        embedding=None,
        tokenizer_version="v1",
        embedding_version=None,
        source_generation=source.source_generation,
        policy_epoch=snapshot.policy_epoch,
        consent_record_id=consent.id,
        index_policy=IndexPolicy.BOTH,
        created_by=CreatedBy.SYSTEM_COMPONENT,
        data_class=DataClass.HIGHLY_SENSITIVE,
        deleted_at=None,
    )
    session.add(projection)

    with pytest.raises(IntegrityError):
        session.flush()


def test_search_revoke_clears_and_excludes_projection_then_regrant_reuses_row(
    session: Session,
) -> None:
    vault = create_vault(session)
    source = _create_source(session, vault_id=vault.id, data_class=DataClass.NORMAL)
    fragment = create_source_fragment(
        session,
        vault_id=vault.id,
        revision_id=source.revision.id,
        ordinal=0,
        text_ciphertext=b"\x0aprotected-fragment",
        text_hash="revocable-fragment-hash",
        fragment_kind="paragraph",
    )
    grant = grant_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.GRANT),
    )
    granted_snapshot = capture_snapshot(session, vault.id)
    projection = create_search_projection(
        session,
        vault_id=vault.id,
        source_fragment_id=fragment.id,
        index_policy=IndexPolicy.BOTH,
        lexical_terms=["private-term"],
        embedding=[0.25, 0.5],
        tokenizer_version="tokenizer-v1",
        embedding_version="embedding-v1",
        snapshot=granted_snapshot,
    )

    assert projection.policy_epoch == granted_snapshot.policy_epoch
    assert projection.consent_record_id == grant.id
    assert list_search_projections(
        session,
        vault_id=vault.id,
        source_document_id=source.document.id,
    ) == [projection]

    revoke_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.REVOKE),
    )

    assert projection.deleted_at is not None
    assert projection.lexical_terms is None
    assert projection.embedding is None
    assert (
        list_search_projections(
            session,
            vault_id=vault.id,
            source_document_id=source.document.id,
        )
        == []
    )

    renewed_grant = grant_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.GRANT),
    )
    renewed_snapshot = capture_snapshot(session, vault.id)
    renewed = create_search_projection(
        session,
        vault_id=vault.id,
        source_fragment_id=fragment.id,
        index_policy=IndexPolicy.LEXICAL,
        lexical_terms=["renewed-term"],
        tokenizer_version="tokenizer-v2",
        snapshot=renewed_snapshot,
    )

    assert renewed.id == projection.id
    assert renewed.deleted_at is None
    assert renewed.policy_epoch == renewed_snapshot.policy_epoch
    assert renewed.consent_record_id == renewed_grant.id
    assert renewed.lexical_terms == ["renewed-term"]
    assert list_search_projections(
        session,
        vault_id=vault.id,
        source_document_id=source.document.id,
    ) == [renewed]


def test_projection_requires_current_snapshot_and_search_consent(session: Session) -> None:
    vault = create_vault(session)
    source = _create_source(session, vault_id=vault.id, data_class=DataClass.NORMAL)
    fragment = create_source_fragment(
        session,
        vault_id=vault.id,
        revision_id=source.revision.id,
        ordinal=0,
        text_ciphertext=b"\x07protected-fragment",
        text_hash="fragment-hash",
        fragment_kind="paragraph",
    )
    grant_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.GRANT),
    )
    granted_snapshot = capture_snapshot(session, vault.id)
    revoke_consent(
        session,
        command=_consent_command(vault.id, action=ConsentAction.REVOKE),
    )

    with pytest.raises(StaleVaultSnapshot):
        create_search_projection(
            session,
            vault_id=vault.id,
            source_fragment_id=fragment.id,
            index_policy=IndexPolicy.LEXICAL,
            lexical_terms=["stale"],
            snapshot=granted_snapshot,
        )
    assert session.scalar(select(SearchProjection.id)) is None

    revoked_snapshot = capture_snapshot(session, vault.id)
    with pytest.raises(ConsentDenied):
        create_search_projection(
            session,
            vault_id=vault.id,
            source_fragment_id=fragment.id,
            index_policy=IndexPolicy.LEXICAL,
            lexical_terms=["denied"],
            snapshot=revoked_snapshot,
        )
