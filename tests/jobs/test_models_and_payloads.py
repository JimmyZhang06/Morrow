from __future__ import annotations

import uuid
from dataclasses import fields

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from life_coach.jobs.contracts import (
    DispatchLease,
    FenceSnapshot,
    IdempotencyConflict,
    JobSpec,
    OutboundOperationSpec,
    assert_same_request,
)
from life_coach.jobs.enums import JobQueue, JobState
from life_coach.jobs.models import Job, OutboundOperation, OutboxEvent
from life_coach.jobs.payloads import (
    InvalidRequestHashError,
    JsonValue,
    UnsafePayloadError,
    canonical_request_hash,
    validate_safe_payload,
    validate_technical_identifier,
    vault_scoped_request_hash,
)
from life_coach.jobs.queueing import QUEUE_METADATA
from life_coach.jobs.repository import resolve_waiting_job_statement

_HMAC_KEY = b"test-only-request-hmac-key-material"


def _request_hash(vault_id: uuid.UUID, request: JsonValue) -> str:
    return canonical_request_hash(request, vault_id=vault_id, hmac_key=_HMAC_KEY)


def _job(*, vault_id: uuid.UUID, key: str, request_hash: str) -> Job:
    resource_id = uuid.uuid4()
    return Job(
        vault_id=vault_id,
        job_type="extract_memory",
        queue=JobQueue.INGEST_TEXT,
        resource_id=resource_id,
        pipeline_version="pipeline-v1",
        idempotency_key=key,
        request_hash=request_hash,
        payload={
            "vault_id": str(vault_id),
            "resource_id": str(resource_id),
            "pipeline_version": "pipeline-v1",
        },
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "private diary body"},
        {"resource_id": "opaque", "details": {"quote": "private quote"}},
        {"prompt": "private prompt"},
        {"response": "private model result"},
        {"resource_id": "private diary body"},
        {"pipeline_version": "今天我非常难过"},
    ],
)
def test_payload_rejects_non_routing_data_without_echo(payload: dict[str, object]) -> None:
    with pytest.raises(UnsafePayloadError) as exc_info:
        validate_safe_payload(payload)

    message = str(exc_info.value)
    assert "private" not in message
    assert "quote" not in message
    assert "prompt" not in message


def test_request_fingerprint_is_order_independent_and_vault_unlinkable(
    vault_a: uuid.UUID, vault_b: uuid.UUID
) -> None:
    request = {"title": "draft", "at": "2026-08-23"}
    left = _request_hash(vault_a, request)
    right = _request_hash(vault_a, {"at": "2026-08-23", "title": "draft"})
    other_vault = _request_hash(vault_b, request)

    assert left == right
    assert left != other_vault
    assert left.startswith("hmac-sha256:v1:")
    assert len(left) == 79


def test_request_fingerprint_rejects_unkeyed_or_weak_fingerprints(vault_a: uuid.UUID) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        vault_scoped_request_hash({"request": 1}, vault_id=vault_a, hmac_key=b"too-short")
    with pytest.raises(InvalidRequestHashError):
        JobSpec(
            vault_id=vault_a,
            job_type="extract_memory",
            resource_id=uuid.uuid4(),
            pipeline_version="pipeline-v1",
            idempotency_key="job:1000",
            request_hash="0" * 64,
        )


def test_typed_spec_cannot_smuggle_body_through_pipeline_version(
    vault_a: uuid.UUID,
) -> None:
    with pytest.raises(UnsafePayloadError):
        JobSpec(
            vault_id=vault_a,
            job_type="extract_memory",
            resource_id=uuid.uuid4(),
            pipeline_version="这是日记正文",
            idempotency_key="job:1001",
            request_hash=_request_hash(vault_a, {"request": "safe"}),
        )


@pytest.mark.parametrize(
    "value",
    ["private diary body", "private-diary-body", "这是日记正文", "opaque"],
)
def test_persistent_identifier_rejects_prose_shaped_metadata(value: str) -> None:
    with pytest.raises(UnsafePayloadError):
        validate_technical_identifier(value)


def test_outbound_spec_binds_exact_authorization_resource_and_initial_fence(
    vault_a: uuid.UUID,
) -> None:
    authorization_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    spec = OutboundOperationSpec(
        vault_id=vault_a,
        connector="todoist",
        operation="create_task",
        local_resource_version=str(uuid.uuid4()),
        request_hash=_request_hash(vault_a, {"task": "opaque"}),
        provider_idempotency_key="provider:1001",
        authorization_id=authorization_id,
        authorization_generation=2,
        resource_id=resource_id,
        initial_fence=FenceSnapshot(7, 3),
        max_attempts=2,
    )

    assert spec.authorization_id == authorization_id
    assert spec.authorization_generation == 2
    assert spec.resource_id == resource_id
    assert spec.initial_fence == FenceSnapshot(7, 3)


def test_outbound_spec_rejects_tombstone_and_nontechnical_binding(
    vault_a: uuid.UUID,
) -> None:
    common = {
        "vault_id": vault_a,
        "connector": "todoist",
        "operation": "create_task",
        "request_hash": _request_hash(vault_a, {"task": "opaque"}),
        "provider_idempotency_key": "provider:1001",
        "authorization_id": uuid.uuid4(),
        "authorization_generation": 1,
        "resource_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError, match="tombstoned"):
        OutboundOperationSpec(
            **common,
            local_resource_version=str(uuid.uuid4()),
            initial_fence=FenceSnapshot(1, 1, tombstoned=True),
        )
    with pytest.raises(UnsafePayloadError):
        OutboundOperationSpec(
            **common,
            local_resource_version="private-diary-body",
            initial_fence=FenceSnapshot(1, 1),
        )


def test_dispatcher_lease_surface_is_exactly_three_metadata_fields() -> None:
    assert [field.name for field in fields(DispatchLease)] == [
        "job_id",
        "vault_id",
        "lease_generation",
    ]


def test_job_idempotency_key_is_isolated_across_vaults(
    db_session: Session, vault_a: uuid.UUID, vault_b: uuid.UUID
) -> None:
    db_session.add_all(
        [
            _job(
                vault_id=vault_a,
                key="client:1001",
                request_hash=_request_hash(vault_a, {"resource": "same-logical-client-key"}),
            ),
            _job(
                vault_id=vault_b,
                key="client:1001",
                request_hash=_request_hash(vault_b, {"resource": "same-logical-client-key"}),
            ),
        ]
    )
    db_session.flush()

    assert db_session.query(Job).count() == 2


def test_same_scoped_key_is_database_unique(db_session: Session, vault_a: uuid.UUID) -> None:
    first_hash = _request_hash(vault_a, {"request": 1})
    second_hash = _request_hash(vault_a, {"request": 2})
    db_session.add(_job(vault_id=vault_a, key="client:1002", request_hash=first_hash))
    db_session.flush()
    db_session.add(_job(vault_id=vault_a, key="client:1002", request_hash=second_hash))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_key_with_different_request_is_a_safe_conflict(vault_a: uuid.UUID) -> None:
    first_hash = _request_hash(vault_a, {"request": 1})
    second_hash = _request_hash(vault_a, {"request": 2})

    with pytest.raises(IdempotencyConflict) as exc_info:
        assert_same_request(first_hash, second_hash)

    assert first_hash not in str(exc_info.value)
    assert second_hash not in str(exc_info.value)


def test_orm_payload_must_match_typed_routing_columns(
    db_session: Session, vault_a: uuid.UUID
) -> None:
    job = _job(
        vault_id=vault_a,
        key="job:1003",
        request_hash=_request_hash(vault_a, {"request": "routing"}),
    )
    job.payload["resource_id"] = str(uuid.uuid4())
    db_session.add(job)

    with pytest.raises(UnsafePayloadError, match="does not match"):
        db_session.flush()


def test_privacy_critical_queue_has_dedicated_reliability_metadata() -> None:
    privacy = QUEUE_METADATA[JobQueue.PRIVACY_CRITICAL]
    other_priorities = [
        metadata.default_priority
        for queue, metadata in QUEUE_METADATA.items()
        if queue is not JobQueue.PRIVACY_CRITICAL
    ]

    assert privacy.dedicated_worker_pool
    assert privacy.reserved_database_connections
    assert privacy.escalation_alerts
    assert privacy.default_priority > max(other_priorities)


def test_outbound_ledger_has_no_arbitrary_payload_column() -> None:
    assert "payload" not in OutboundOperation.__table__.columns


def test_orm_models_reject_prose_in_persistent_identifier_columns(
    vault_a: uuid.UUID,
) -> None:
    with pytest.raises(UnsafePayloadError):
        _job(
            vault_id=vault_a,
            key="private-diary-body",
            request_hash=_request_hash(vault_a, {"request": "opaque"}),
        )
    with pytest.raises(UnsafePayloadError):
        OutboundOperation(
            vault_id=vault_a,
            connector="todoist",
            operation="create_task",
            resource_id=uuid.uuid4(),
            local_resource_version="private-diary-body",
            request_hash=_request_hash(vault_a, {"request": "opaque"}),
            provider_idempotency_key="provider:1001",
            authorization_id=uuid.uuid4(),
            authorization_generation=1,
            policy_epoch=1,
            source_generation=1,
            initial_tombstoned=False,
        )


def test_job_outbox_foreign_key_rejects_cross_vault_reference(
    db_session: Session, vault_a: uuid.UUID, vault_b: uuid.UUID
) -> None:
    resource_id = uuid.uuid4()
    event = OutboxEvent(
        vault_id=vault_a,
        event_type="source_changed",
        resource_id=resource_id,
        pipeline_version="pipeline-v1",
        idempotency_key="event:1001",
        request_hash=_request_hash(vault_a, {"event": "opaque"}),
        payload={},
        expected_job_types=["index_source"],
    )
    db_session.add(event)
    db_session.flush()
    valid_job = _job(
        vault_id=vault_a,
        key="job:2000",
        request_hash=_request_hash(vault_a, {"job": "valid"}),
    )
    valid_job.job_type = "extract_source"
    valid_job.resource_id = resource_id
    valid_job.payload = {
        "vault_id": str(vault_a),
        "resource_id": str(resource_id),
        "pipeline_version": "pipeline-v1",
    }
    valid_job.outbox_event_id = event.id
    db_session.add(valid_job)
    db_session.flush()

    cross_vault_job = _job(
        vault_id=vault_b,
        key="job:2001",
        request_hash=_request_hash(vault_b, {"job": "opaque"}),
    )
    cross_vault_job.job_type = "index_source"
    cross_vault_job.resource_id = resource_id
    cross_vault_job.payload = {
        "vault_id": str(vault_b),
        "resource_id": str(resource_id),
        "pipeline_version": "pipeline-v1",
    }
    cross_vault_job.outbox_event_id = event.id
    db_session.add(cross_vault_job)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_exhausted_waiting_job_resolves_to_dead_instead_of_stranded_queue(
    db_session: Session, vault_a: uuid.UUID
) -> None:
    job = _job(
        vault_id=vault_a,
        key="job:1004",
        request_hash=_request_hash(vault_a, {"request": "unknown"}),
    )
    job.state = JobState.WAITING
    job.attempts = 5
    job.max_attempts = 5
    db_session.add(job)
    db_session.flush()

    db_session.execute(
        resolve_waiting_job_statement(JobState.QUEUED),
        {"waiting_job_id": job.id, "waiting_vault_id": vault_a},
    )
    db_session.refresh(job)

    assert job.state is JobState.DEAD
    assert job.completed_at is not None
