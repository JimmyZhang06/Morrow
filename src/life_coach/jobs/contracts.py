"""Narrow data contracts shared by dispatchers, processors, and repositories."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.jobs.enums import FenceCheckpoint, JobQueue
from life_coach.jobs.payloads import (
    SafePayload,
    validate_request_hash,
    validate_routing_name,
    validate_safe_payload,
    validate_subscriber_job_types,
)


class IdempotencyConflict(RuntimeError):
    """A scoped key was reused for a materially different request."""


class CrossVaultAccessError(PermissionError):
    """A single-vault repository received a lease/spec for another vault."""


class ProcessorScopeError(RuntimeError):
    """A processor attempted work before setting transaction-local vault scope."""


class FenceViolation(RuntimeError):
    """A policy/source/tombstone execution fence rejected further processing."""

    def __init__(self, checkpoint: FenceCheckpoint, reason: str) -> None:
        super().__init__(f"execution fence rejected {checkpoint.value}: {reason}")
        self.checkpoint = checkpoint
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DispatchLease:
    """The entire data surface visible to the global Dispatcher."""

    job_id: uuid.UUID
    vault_id: uuid.UUID
    lease_generation: int


@dataclass(frozen=True, slots=True)
class FenceSnapshot:
    policy_epoch: int
    source_generation: int
    tombstoned: bool = False

    def __post_init__(self) -> None:
        if self.policy_epoch < 0 or self.source_generation < 0:
            raise ValueError("fence generations cannot be negative")


@dataclass(frozen=True, slots=True)
class JobExecutionContext:
    """Privacy-safe execution metadata available only inside one vault Processor."""

    lease: DispatchLease
    job_type: str
    queue: JobQueue
    resource_id: uuid.UUID
    resource_revision_id: uuid.UUID | None
    pipeline_version: str
    consent_snapshot_id: uuid.UUID | None
    expected_fence: FenceSnapshot
    payload: SafePayload
    attempts: int
    max_attempts: int

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.max_attempts < 1 or self.attempts > self.max_attempts:
            raise ValueError("invalid claimed-job attempt counters")


@dataclass(frozen=True, slots=True)
class JobSpec:
    vault_id: uuid.UUID
    job_type: str
    resource_id: uuid.UUID
    pipeline_version: str
    idempotency_key: str
    request_hash: str
    queue: JobQueue = JobQueue.INGEST_TEXT
    resource_revision_id: uuid.UUID | None = None
    priority: int | None = None
    max_attempts: int = 5
    run_after: datetime | None = None
    consent_snapshot_id: uuid.UUID | None = None
    policy_epoch: int = 0
    source_generation: int = 0
    payload: SafePayload = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key or not self.pipeline_version:
            raise ValueError("job type, idempotency key, and pipeline version are required")
        validate_routing_name(self.job_type)
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.policy_epoch < 0 or self.source_generation < 0:
            raise ValueError("fence generations cannot be negative")
        object.__setattr__(self, "request_hash", validate_request_hash(self.request_hash))
        validate_safe_payload(
            {
                "vault_id": str(self.vault_id),
                "resource_id": str(self.resource_id),
                "pipeline_version": self.pipeline_version,
            }
        )
        object.__setattr__(self, "payload", validate_safe_payload(self.payload))


@dataclass(frozen=True, slots=True)
class OutboxEventSpec:
    vault_id: uuid.UUID
    event_type: str
    resource_id: uuid.UUID
    pipeline_version: str
    idempotency_key: str
    request_hash: str
    resource_revision_id: uuid.UUID | None = None
    subscriber_job_types: tuple[str, ...] = ()
    payload: SafePayload = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key or not self.pipeline_version:
            raise ValueError("event type, idempotency key, and pipeline version are required")
        validate_routing_name(self.event_type)
        object.__setattr__(self, "request_hash", validate_request_hash(self.request_hash))
        validate_safe_payload(
            {
                "vault_id": str(self.vault_id),
                "resource_id": str(self.resource_id),
                "pipeline_version": self.pipeline_version,
            }
        )
        object.__setattr__(
            self,
            "subscriber_job_types",
            validate_subscriber_job_types(self.subscriber_job_types),
        )
        object.__setattr__(self, "payload", validate_safe_payload(self.payload))


@dataclass(frozen=True, slots=True)
class OutboundOperationSpec:
    vault_id: uuid.UUID
    connector: str
    operation: str
    local_resource_version: str
    request_hash: str
    provider_idempotency_key: str

    def __post_init__(self) -> None:
        required = (
            self.connector,
            self.operation,
            self.local_resource_version,
            self.provider_idempotency_key,
        )
        if any(not value for value in required):
            raise ValueError("outbound operation scope values are required")
        validate_routing_name(self.connector)
        validate_routing_name(self.operation)
        object.__setattr__(self, "request_hash", validate_request_hash(self.request_hash))


@dataclass(frozen=True, slots=True)
class IdempotentWrite:
    resource_id: uuid.UUID
    created: bool


type PersistResult = Callable[[AsyncSession], Awaitable[None]]


class AuthoritativeFenceReader(Protocol):
    """Reads policy/source state through the same single-vault database transaction.

    Integrations must implement ``read_for_update`` with row locks that serialize the final
    derived-result write against policy revocation, source generation bumps, and tombstoning.
    """

    async def read_current(
        self,
        session: AsyncSession,
        *,
        vault_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> FenceSnapshot: ...

    async def read_for_update(
        self,
        session: AsyncSession,
        *,
        vault_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> FenceSnapshot: ...


def assert_same_request(existing_hash: str, requested_hash: str) -> None:
    """Enforce scoped-key replay semantics without leaking either fingerprint."""

    if not secrets.compare_digest(existing_hash, requested_hash):
        raise IdempotencyConflict("idempotency key was reused with a different request")
