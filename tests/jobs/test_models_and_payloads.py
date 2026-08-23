from __future__ import annotations

import uuid
from dataclasses import fields

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from life_coach.jobs.contracts import (
    DispatchLease,
    IdempotencyConflict,
    JobSpec,
    assert_same_request,
)
from life_coach.jobs.enums import JobQueue, JobState
from life_coach.jobs.models import Job, OutboundOperation
from life_coach.jobs.payloads import (
    UnsafePayloadError,
    canonical_request_hash,
    validate_safe_payload,
)
from life_coach.jobs.queueing import QUEUE_METADATA
from life_coach.jobs.repository import resolve_waiting_job_statement


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


def test_canonical_request_hash_is_order_independent() -> None:
    left = canonical_request_hash({"title": "draft", "at": "2026-08-23"})
    right = canonical_request_hash({"at": "2026-08-23", "title": "draft"})

    assert left == right
    assert len(left) == 64


def test_typed_spec_cannot_smuggle_body_through_pipeline_version(
    vault_a: uuid.UUID,
) -> None:
    with pytest.raises(UnsafePayloadError):
        JobSpec(
            vault_id=vault_a,
            job_type="extract_memory",
            resource_id=uuid.uuid4(),
            pipeline_version="这是日记正文",
            idempotency_key="safe-key",
            request_hash=canonical_request_hash({"request": "safe"}),
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
    request_hash = canonical_request_hash({"resource": "same-logical-client-key"})
    db_session.add_all(
        [
            _job(vault_id=vault_a, key="client-key", request_hash=request_hash),
            _job(vault_id=vault_b, key="client-key", request_hash=request_hash),
        ]
    )
    db_session.flush()

    assert db_session.query(Job).count() == 2


def test_same_scoped_key_is_database_unique(db_session: Session, vault_a: uuid.UUID) -> None:
    first_hash = canonical_request_hash({"request": 1})
    second_hash = canonical_request_hash({"request": 2})
    db_session.add(_job(vault_id=vault_a, key="same-key", request_hash=first_hash))
    db_session.flush()
    db_session.add(_job(vault_id=vault_a, key="same-key", request_hash=second_hash))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_key_with_different_request_is_a_safe_conflict() -> None:
    first_hash = canonical_request_hash({"request": 1})
    second_hash = canonical_request_hash({"request": 2})

    with pytest.raises(IdempotencyConflict) as exc_info:
        assert_same_request(first_hash, second_hash)

    assert first_hash not in str(exc_info.value)
    assert second_hash not in str(exc_info.value)


def test_orm_payload_must_match_typed_routing_columns(
    db_session: Session, vault_a: uuid.UUID
) -> None:
    job = _job(
        vault_id=vault_a,
        key="mismatched-routing",
        request_hash=canonical_request_hash({"request": "routing"}),
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


def test_exhausted_waiting_job_resolves_to_dead_instead_of_stranded_queue(
    db_session: Session, vault_a: uuid.UUID
) -> None:
    job = _job(
        vault_id=vault_a,
        key="unknown-on-last-attempt",
        request_hash=canonical_request_hash({"request": "unknown"}),
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
