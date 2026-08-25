from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from life_coach.modules.action.lifecycle import (
    ActionIdempotencyConflictError,
    ActionNotFoundError,
    MemoryNotEligibleForActionError,
    ReversibleActionState,
    ReversibleActionVerdict,
)
from life_coach.modules.action.models import ActionCommandReceipt, ActionVerdict
from life_coach.modules.action.service import ReversibleActionService
from life_coach.modules.knowledge.contracts import ClaimProposal
from life_coach.modules.knowledge.enums import (
    Attribution,
    ConfidenceBand,
    DataClass,
    EpistemicType,
    MemoryClaimKind,
    ValidTimePrecision,
    VerdictType,
)
from life_coach.platform.model_registry import load_model_registry
from life_coach.shared.database import Base
from tests.knowledge.fakes import (
    evidence_anchor,
    make_memory_service,
    record_authoritative_source,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    load_model_registry()
    database = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(database, "connect")
    def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(database)
    try:
        with Session(database, expire_on_commit=False) as active_session:
            yield active_session
            active_session.rollback()
    finally:
        Base.metadata.drop_all(database)
        database.dispose()


def _memory(session: Session, *, confirmed: bool) -> tuple[uuid.UUID, uuid.UUID]:
    vault_id = uuid.uuid4()
    fragment_id = record_authoritative_source(session, vault_id=vault_id)
    memory_service = make_memory_service(session, clock=lambda: NOW)
    detail = memory_service.create_claim(
        vault_id=vault_id,
        proposal=ClaimProposal(
            kind=MemoryClaimKind.PREFERENCE,
            canonical_text="I focus better after a short walk.",
            structured_payload={},
            epistemic_type=EpistemicType.STATED,
            attribution=Attribution.SELF_REPORT,
            uncertainty_text=None,
            valid_from=NOW,
            valid_to=None,
            valid_time_precision=ValidTimePrecision.EXACT,
            valid_time_original=None,
            valid_timezone="UTC",
            confidence_band=ConfidenceBand.MEDIUM,
            pipeline_version="action-loop-test-v1",
            data_class=DataClass.NORMAL,
            evidence=(evidence_anchor(fragment_id),),
        ),
    )
    if confirmed:
        memory_service.record_verdict(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            verdict=VerdictType.CONFIRM,
            expected_etag=detail.etag,
        )
    return vault_id, detail.memory_id


def test_confirmed_memory_drives_idempotent_accept_complete_and_revoke(
    session: Session,
) -> None:
    vault_id, memory_id = _memory(session, confirmed=True)
    service = ReversibleActionService(session)
    create_key = uuid.uuid4()

    proposed = service.create_for_memory(
        vault_id=vault_id,
        memory_id=memory_id,
        idempotency_key=create_key,
    )
    replay = service.create_for_memory(
        vault_id=vault_id,
        memory_id=memory_id,
        idempotency_key=create_key,
    )

    assert replay == proposed
    assert proposed.state is ReversibleActionState.PROPOSED
    assert proposed.estimated_minutes == 10
    assert proposed.is_reversible is True
    assert "I focus" not in proposed.description

    accept_key = uuid.uuid4()
    accepted = service.record_verdict(
        vault_id=vault_id,
        action_id=proposed.action_id,
        verdict=ReversibleActionVerdict.ACCEPT,
        expected_revision=1,
        idempotency_key=accept_key,
    )
    accepted_replay = service.record_verdict(
        vault_id=vault_id,
        action_id=proposed.action_id,
        verdict=ReversibleActionVerdict.ACCEPT,
        expected_revision=1,
        idempotency_key=accept_key,
    )
    assert accepted_replay.verdict_id == accepted.verdict_id
    assert accepted.action.state is ReversibleActionState.ACCEPTED
    assert accepted.action.revision == 2

    completed = service.record_verdict(
        vault_id=vault_id,
        action_id=proposed.action_id,
        verdict=ReversibleActionVerdict.COMPLETE,
        expected_revision=2,
        idempotency_key=uuid.uuid4(),
    )
    assert completed.action.state is ReversibleActionState.COMPLETED
    assert completed.action.revision == 3

    revoked = service.record_verdict(
        vault_id=vault_id,
        action_id=proposed.action_id,
        verdict=ReversibleActionVerdict.REVOKE,
        expected_revision=3,
        idempotency_key=uuid.uuid4(),
    )
    assert revoked.action.state is ReversibleActionState.REVOKED
    assert revoked.action.revision == 4
    assert session.query(ActionVerdict).count() == 3
    assert session.query(ActionCommandReceipt).count() == 4


def test_action_requires_confirmed_or_corrected_current_memory(session: Session) -> None:
    vault_id, memory_id = _memory(session, confirmed=False)

    with pytest.raises(MemoryNotEligibleForActionError):
        ReversibleActionService(session).create_for_memory(
            vault_id=vault_id,
            memory_id=memory_id,
            idempotency_key=uuid.uuid4(),
        )


def test_idempotency_key_is_bound_to_exact_command(session: Session) -> None:
    vault_id, memory_id = _memory(session, confirmed=True)
    service = ReversibleActionService(session)
    action = service.create_for_memory(
        vault_id=vault_id,
        memory_id=memory_id,
        idempotency_key=uuid.uuid4(),
    )
    key = uuid.uuid4()
    service.record_verdict(
        vault_id=vault_id,
        action_id=action.action_id,
        verdict=ReversibleActionVerdict.ACCEPT,
        expected_revision=1,
        idempotency_key=key,
    )

    with pytest.raises(ActionIdempotencyConflictError):
        service.record_verdict(
            vault_id=vault_id,
            action_id=action.action_id,
            verdict=ReversibleActionVerdict.REVOKE,
            expected_revision=2,
            idempotency_key=key,
        )


def test_vault_filter_hides_action_from_other_vault(session: Session) -> None:
    vault_id, memory_id = _memory(session, confirmed=True)
    service = ReversibleActionService(session)
    action = service.create_for_memory(
        vault_id=vault_id,
        memory_id=memory_id,
        idempotency_key=uuid.uuid4(),
    )

    with pytest.raises(ActionNotFoundError):
        service.get(vault_id=uuid.uuid4(), action_id=action.action_id)


def test_action_history_is_vault_scoped_paginated_and_current(session: Session) -> None:
    vault_id, memory_id = _memory(session, confirmed=True)
    service = ReversibleActionService(session)
    action = service.create_for_memory(
        vault_id=vault_id,
        memory_id=memory_id,
        idempotency_key=uuid.uuid4(),
    )
    accepted = service.record_verdict(
        vault_id=vault_id,
        action_id=action.action_id,
        verdict=ReversibleActionVerdict.ACCEPT,
        expected_revision=1,
        idempotency_key=uuid.uuid4(),
    )

    page = service.list(vault_id=vault_id, limit=1)

    assert page.items == (accepted.action,)
    assert page.next_cursor is None
    assert service.list(vault_id=uuid.uuid4()).items == ()
    with pytest.raises(ValueError, match="invalid action cursor"):
        service.list(vault_id=vault_id, cursor="not-a-cursor")
