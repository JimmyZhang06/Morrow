from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from life_coach.modules.consent import (
    ConsentPurpose,
    ConsentRecord,
    ConsentRecordImmutable,
    ConsentScopeNotFound,
    capture_snapshot,
    check_consent,
    check_snapshot,
    grant_consent,
    resolve_consent,
    revoke_consent,
)
from life_coach.modules.identity import create_vault
from life_coach.modules.sources import (
    SourceWriteResult,
    create_source_document,
    tombstone_source_document,
)
from life_coach.shared.database import Base


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def _source(session: Session, vault_id: UUID, marker: bytes) -> SourceWriteResult:
    return create_source_document(
        session,
        vault_id=vault_id,
        content_ciphertext=marker,
        content_hash=marker.hex(),
        content_mime="text/plain",
    )


def test_consent_defaults_to_deny_and_revocation_wins_by_epoch(session: Session) -> None:
    vault = create_vault(session)
    assert (
        check_consent(
            session,
            vault_id=vault.id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
        )
        is False
    )

    granted = grant_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
        provider_policy={"allowed_providers": ["zero-retention-provider"]},
    )
    snapshot = capture_snapshot(session, vault.id)
    assert granted.policy_epoch == 1
    assert (
        check_consent(
            session,
            vault_id=vault.id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
        )
        is True
    )

    revoked = revoke_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
    )

    assert revoked.policy_epoch == 2
    assert (
        check_consent(
            session,
            vault_id=vault.id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
        )
        is False
    )
    assert check_snapshot(session, snapshot) is False
    records = list(
        session.scalars(
            select(ConsentRecord)
            .where(ConsentRecord.vault_id == vault.id)
            .order_by(ConsentRecord.policy_epoch)
        )
    )
    assert records == [granted, revoked]


def test_source_specific_event_overrides_older_vault_grant(session: Session) -> None:
    vault = create_vault(session)
    first = _source(session, vault.id, b"\x01protected-first")
    second = _source(session, vault.id, b"\x02protected-second")
    grant_consent(session, vault_id=vault.id, purpose=ConsentPurpose.SEARCH)
    revoke_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.SEARCH,
        source_document_id=first.document.id,
    )

    first_resolution = resolve_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.SEARCH,
        source_document_id=first.document.id,
    )
    assert first_resolution.allowed is False
    assert first_resolution.source_document_id == first.document.id
    assert (
        check_consent(
            session,
            vault_id=vault.id,
            purpose=ConsentPurpose.SEARCH,
            source_document_id=second.document.id,
        )
        is True
    )

    grant_consent(session, vault_id=vault.id, purpose=ConsentPurpose.SEARCH)
    assert (
        check_consent(
            session,
            vault_id=vault.id,
            purpose=ConsentPurpose.SEARCH,
            source_document_id=first.document.id,
        )
        is False
    )


def test_cross_vault_and_tombstoned_source_scopes_are_rejected(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    source = _source(session, first_vault.id, b"\x03protected")

    with pytest.raises(ConsentScopeNotFound):
        grant_consent(
            session,
            vault_id=second_vault.id,
            purpose=ConsentPurpose.SEARCH,
            source_document_id=source.document.id,
        )

    tombstone_source_document(
        session,
        vault_id=first_vault.id,
        document_id=source.document.id,
    )
    with pytest.raises(ConsentScopeNotFound):
        check_consent(
            session,
            vault_id=first_vault.id,
            purpose=ConsentPurpose.SEARCH,
            source_document_id=source.document.id,
        )


def test_consent_record_cannot_be_updated_in_place(session: Session) -> None:
    vault = create_vault(session)
    record = grant_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.PASSIVE_QA,
    )
    session.commit()

    record.provider_policy["unexpected"] = True
    with pytest.raises(ConsentRecordImmutable):
        session.flush()


def test_consent_record_cannot_be_bulk_deleted(session: Session) -> None:
    vault = create_vault(session)
    record = revoke_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.PASSIVE_QA,
    )
    session.commit()

    with pytest.raises(ConsentRecordImmutable):
        session.execute(delete(ConsentRecord).where(ConsentRecord.id == record.id))
