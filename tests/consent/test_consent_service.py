from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from life_coach.modules.consent import (
    ConsentAction,
    ConsentInteractionReplayed,
    ConsentPurpose,
    ConsentRecord,
    ConsentRecordImmutable,
    ConsentScope,
    ConsentScopeNotFound,
    InvalidConsentActor,
    InvalidConsentCommand,
    InvalidProviderPolicy,
    ProviderPolicy,
    UserConsentCommand,
    capture_snapshot,
    check_consent,
    check_snapshot,
    grant_consent,
    record_consent,
    resolve_consent,
    revoke_consent,
)
from life_coach.modules.identity import CreatedBy, DataClass, create_vault
from life_coach.modules.sources import (
    SourceWriteResult,
    create_source_document,
    tombstone_source_document,
)
from life_coach.shared.database import Base, utc_now


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


def _command(
    vault_id: UUID,
    *,
    purpose: ConsentPurpose = ConsentPurpose.PASSIVE_QA,
    action: ConsentAction = ConsentAction.GRANT,
    source_document_id: UUID | None = None,
    provider_policy: ProviderPolicy | Mapping[str, object] | None = None,
    principal_id: UUID | None = None,
    interaction_id: UUID | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> UserConsentCommand:
    now = utc_now()
    issued_at = issued_at or now - timedelta(seconds=1)
    expires_at = expires_at or now + timedelta(minutes=4)
    return UserConsentCommand(
        vault_id=vault_id,
        principal_id=principal_id or uuid4(),
        purpose=purpose,
        action=action,
        interaction_id=interaction_id or uuid4(),
        issued_at=issued_at,
        expires_at=expires_at,
        source_document_id=source_document_id,
        provider_policy=provider_policy,
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

    principal_id = uuid4()
    granted = grant_consent(
        session,
        command=_command(
            vault.id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
            provider_policy={"allowed_providers": ["zero-retention-provider"]},
            principal_id=principal_id,
        ),
    )
    snapshot = capture_snapshot(session, vault.id)
    assert granted.policy_epoch == 1
    assert granted.principal_id == principal_id
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
        command=_command(
            vault.id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
            action=ConsentAction.REVOKE,
            principal_id=principal_id,
        ),
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
    assert granted.provider_policy == ProviderPolicy(allowed_providers=("zero-retention-provider",))
    assert granted.data_class is DataClass.SENSITIVE


@pytest.mark.parametrize(
    ("vault_action", "source_action", "order", "expected"),
    [
        (None, None, "vault_first", False),
        (ConsentAction.GRANT, None, "vault_first", True),
        (ConsentAction.REVOKE, None, "vault_first", False),
        (None, ConsentAction.GRANT, "vault_first", True),
        (None, ConsentAction.REVOKE, "vault_first", False),
        (ConsentAction.GRANT, ConsentAction.GRANT, "vault_first", True),
        (ConsentAction.GRANT, ConsentAction.REVOKE, "vault_first", False),
        (ConsentAction.GRANT, ConsentAction.REVOKE, "source_first", False),
        (ConsentAction.REVOKE, ConsentAction.REVOKE, "vault_first", False),
        (ConsentAction.REVOKE, ConsentAction.GRANT, "vault_first", True),
        (ConsentAction.REVOKE, ConsentAction.GRANT, "source_first", False),
    ],
)
def test_vault_and_source_decision_truth_table(
    session: Session,
    vault_action: ConsentAction | None,
    source_action: ConsentAction | None,
    order: str,
    expected: bool,
) -> None:
    vault = create_vault(session)
    source = _source(session, vault.id, uuid4().bytes)

    def append(action: ConsentAction | None, *, source_scoped: bool) -> None:
        if action is not None:
            record_consent(
                session,
                command=_command(
                    vault.id,
                    action=action,
                    source_document_id=source.document.id if source_scoped else None,
                ),
            )

    events = (
        ((vault_action, False), (source_action, True))
        if order == "vault_first"
        else ((source_action, True), (vault_action, False))
    )
    for action, source_scoped in events:
        append(action, source_scoped=source_scoped)

    resolution = resolve_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.PASSIVE_QA,
        source_document_id=source.document.id,
    )
    assert resolution.allowed is expected


@pytest.mark.parametrize("vault_first", [True, False])
def test_provider_policy_is_restrictive_meet_across_scopes(
    session: Session, vault_first: bool
) -> None:
    vault = create_vault(session)
    source = _source(session, vault.id, uuid4().bytes)
    vault_policy = ProviderPolicy(
        allowed_providers=("zero-retention-provider", "another-provider"),
        processing_regions=("eu", "apac"),
        zero_retention_required=True,
        training_use_allowed=False,
        max_retention_days=7,
        policy_version="v1",
    )
    source_policy = ProviderPolicy(
        allowed_providers=("regional-provider", "zero-retention-provider"),
        processing_regions=("us", "eu"),
        zero_retention_required=False,
        training_use_allowed=True,
        max_retention_days=30,
        policy_version="v1",
    )

    commands: tuple[UserConsentCommand, ...] = (
        _command(vault.id, provider_policy=vault_policy),
        _command(
            vault.id,
            source_document_id=source.document.id,
            provider_policy=source_policy,
        ),
    )
    if not vault_first:
        commands = tuple(reversed(commands))
    for command in commands:
        grant_consent(session, command=command)

    resolution = resolve_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.PASSIVE_QA,
        source_document_id=source.document.id,
    )
    assert resolution.allowed is True
    assert resolution.provider_policy == ProviderPolicy(
        allowed_providers=("zero-retention-provider",),
        processing_regions=("eu",),
        zero_retention_required=True,
        training_use_allowed=False,
        max_retention_days=7,
        policy_version="v1",
    )
    assert resolution.policy_epoch == 2


def test_policy_empty_intersection_is_deny_all_not_wildcard(session: Session) -> None:
    vault = create_vault(session)
    source = _source(session, vault.id, uuid4().bytes)
    grant_consent(
        session,
        command=_command(
            vault.id,
            provider_policy={
                "allowed_providers": ["zero-retention-provider"],
                "processing_regions": ["us"],
            },
        ),
    )
    grant_consent(
        session,
        command=_command(
            vault.id,
            source_document_id=source.document.id,
            provider_policy={
                "allowed_providers": ["regional-provider"],
                "processing_regions": ["apac"],
            },
        ),
    )

    resolution = resolve_consent(
        session,
        vault_id=vault.id,
        purpose=ConsentPurpose.PASSIVE_QA,
        source_document_id=source.document.id,
    )
    assert resolution.allowed is True
    assert resolution.provider_policy.allowed_providers == ()
    assert resolution.provider_policy.processing_regions == ()


def test_cross_vault_and_tombstoned_source_scopes_are_rejected(session: Session) -> None:
    first_vault = create_vault(session)
    second_vault = create_vault(session)
    source = _source(session, first_vault.id, b"\x03protected")

    with pytest.raises(ConsentScopeNotFound):
        grant_consent(
            session,
            command=_command(second_vault.id, source_document_id=source.document.id),
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
    record = grant_consent(session, command=_command(vault.id))
    session.commit()

    record.provider_policy = ProviderPolicy(allowed_providers=("another-provider",))
    with pytest.raises(ConsentRecordImmutable):
        session.flush()


def test_consent_record_cannot_be_bulk_deleted(session: Session) -> None:
    vault = create_vault(session)
    record = revoke_consent(
        session,
        command=_command(vault.id, action=ConsentAction.REVOKE),
    )
    session.commit()

    with pytest.raises(ConsentRecordImmutable):
        session.execute(delete(ConsentRecord).where(ConsentRecord.id == record.id))


def test_service_has_no_self_reported_created_by_escape_hatch(session: Session) -> None:
    vault = create_vault(session)
    with pytest.raises(TypeError):
        grant_consent(  # type: ignore[call-arg]
            session,
            command=_command(vault.id),
            created_by=CreatedBy.USER,
        )
    assert vault.policy_epoch == 0


@pytest.mark.parametrize(
    "provider_policy",
    [
        {"private_note": "今天和小李谈完后的私密正文"},
        {"allowed_providers": ["今天和小李谈完后的私密正文"]},
        {"allowed_providers": ["unknown-provider"]},
        {"processing_regions": ["unknown-region"]},
        {"policy_version": "unknown-version"},
        {"zero_retention_required": "yes"},
    ],
)
def test_provider_policy_rejects_unbounded_prose_or_unregistered_ids(
    session: Session, provider_policy: dict[str, object]
) -> None:
    vault = create_vault(session)

    with pytest.raises(InvalidProviderPolicy):
        _command(
            vault.id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
            provider_policy=provider_policy,
        )

    assert vault.policy_epoch == 0
    assert session.scalar(select(ConsentRecord.id)) is None


def test_provider_policy_instance_is_always_revalidated() -> None:
    policy = ProviderPolicy(allowed_providers=("zero-retention-provider",))
    revalidated = ProviderPolicy.from_value(policy)
    assert revalidated == policy
    assert revalidated is not policy

    object.__setattr__(policy, "allowed_providers", ("unknown-provider",))
    with pytest.raises(InvalidProviderPolicy):
        ProviderPolicy.from_value(policy)


@pytest.mark.parametrize("state", ["expired", "future"])
def test_expired_or_future_interaction_fails_before_epoch_increment(
    session: Session, state: str
) -> None:
    vault = create_vault(session)
    now = utc_now()
    issued_at, expires_at = (
        (now - timedelta(minutes=2), now - timedelta(minutes=1))
        if state == "expired"
        else (now + timedelta(minutes=1), now + timedelta(minutes=2))
    )
    command = _command(
        vault.id,
        issued_at=issued_at,
        expires_at=expires_at,
    )

    with pytest.raises(InvalidConsentCommand):
        record_consent(session, command=command)
    assert vault.policy_epoch == 0
    assert session.scalar(select(ConsentRecord.id)) is None


def test_interaction_is_single_use_and_replay_does_not_advance_epoch(session: Session) -> None:
    vault = create_vault(session)
    interaction_id = uuid4()
    first = _command(vault.id, interaction_id=interaction_id)
    record_consent(session, command=first)

    replay_with_changed_action = _command(
        vault.id,
        action=ConsentAction.REVOKE,
        interaction_id=interaction_id,
        principal_id=first.principal_id,
    )
    with pytest.raises(ConsentInteractionReplayed):
        record_consent(session, command=replay_with_changed_action)

    assert vault.policy_epoch == 1
    assert len(list(session.scalars(select(ConsentRecord.id)))) == 1


def test_command_requires_bounded_aware_interaction_window() -> None:
    now = utc_now()
    with pytest.raises(InvalidConsentCommand):
        _command(uuid4(), issued_at=now.replace(tzinfo=None), expires_at=now)
    with pytest.raises(InvalidConsentCommand):
        _command(
            uuid4(),
            issued_at=now,
            expires_at=now + timedelta(minutes=6),
        )


def test_search_revoke_invalidates_matching_projections_in_same_call(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = create_vault(session)
    source = _source(session, vault.id, uuid4().bytes)
    calls: list[tuple[UUID, UUID | None]] = []

    def fake_invalidate(
        _session: Session, *, vault_id: UUID, source_document_id: UUID | None = None
    ) -> int:
        calls.append((vault_id, source_document_id))
        return 0

    monkeypatch.setattr(
        "life_coach.modules.sources.service.invalidate_search_projections",
        fake_invalidate,
    )
    revoke_consent(
        session,
        command=_command(
            vault.id,
            purpose=ConsentPurpose.SEARCH,
            action=ConsentAction.REVOKE,
            source_document_id=source.document.id,
        ),
    )
    assert calls == [(vault.id, source.document.id)]


def test_direct_orm_insert_cannot_bypass_actor_or_provider_policy_validation(
    session: Session,
) -> None:
    vault = create_vault(session)
    now = utc_now()
    forged = ConsentRecord(
        vault_id=vault.id,
        purpose=ConsentPurpose.SEARCH,
        action=ConsentAction.GRANT,
        scope=ConsentScope.VAULT,
        source_document_id=None,
        principal_id=uuid4(),
        interaction_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        provider_policy=ProviderPolicy(),
        policy_epoch=1,
        created_by=CreatedBy.SYSTEM_COMPONENT,
        data_class=DataClass.SENSITIVE,
        deleted_at=None,
    )
    session.add(forged)
    with pytest.raises(InvalidConsentActor):
        session.flush()

    session.rollback()
    vault = create_vault(session)
    unsafe_policy = ConsentRecord(
        vault_id=vault.id,
        purpose=ConsentPurpose.SEARCH,
        action=ConsentAction.GRANT,
        scope=ConsentScope.VAULT,
        source_document_id=None,
        principal_id=uuid4(),
        interaction_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        provider_policy={"private_note": "私密正文"},
        policy_epoch=1,
        created_by=CreatedBy.USER,
        data_class=DataClass.SENSITIVE,
        deleted_at=None,
    )
    session.add(unsafe_policy)
    with pytest.raises(InvalidProviderPolicy):
        session.flush()
