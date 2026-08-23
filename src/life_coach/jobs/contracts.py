"""Narrow data contracts shared by dispatchers, processors, and repositories."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from life_coach.jobs.enums import FenceCheckpoint, JobQueue, OutboundAuthorizationDecision
from life_coach.jobs.payloads import (
    SafePayload,
    VaultRequestFingerprint,
    validate_resource_version_identifier,
    validate_routing_name,
    validate_safe_payload,
    validate_subscriber_job_types,
    validate_technical_identifier,
    validate_vault_request_fingerprint,
)


class IdempotencyConflict(RuntimeError):
    """A scoped key was reused for a materially different request."""


class CrossVaultAccessError(PermissionError):
    """A single-vault repository received a lease/spec for another vault."""


class ProcessorScopeError(RuntimeError):
    """A processor attempted work before setting transaction-local vault scope."""


class LeaseLostError(RuntimeError):
    """A processor tried to cross a privacy boundary after losing its database lease."""


class FenceViolation(RuntimeError):
    """A policy/source/tombstone execution fence rejected further processing."""

    def __init__(self, checkpoint: FenceCheckpoint, reason: str) -> None:
        super().__init__(f"execution fence rejected {checkpoint.value}: {reason}")
        self.checkpoint = checkpoint
        self.reason = reason


class OutboundExecutionGuardError(RuntimeError):
    """An external side effect was denied before its execution CAS."""


class OutboundAuthorizationRejected(OutboundExecutionGuardError):
    """The exact one-time user authorization could not be consumed."""

    def __init__(self, decision: OutboundAuthorizationDecision) -> None:
        super().__init__(f"outbound authorization rejected: {decision.value}")
        self.decision = decision


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
    request_hash: VaultRequestFingerprint
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
        validate_technical_identifier(self.idempotency_key)
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.policy_epoch < 0 or self.source_generation < 0:
            raise ValueError("fence generations cannot be negative")
        validate_vault_request_fingerprint(self.request_hash, vault_id=self.vault_id)
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
    request_hash: VaultRequestFingerprint
    resource_revision_id: uuid.UUID | None = None
    subscriber_job_types: tuple[str, ...] = ()
    payload: SafePayload = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key or not self.pipeline_version:
            raise ValueError("event type, idempotency key, and pipeline version are required")
        validate_routing_name(self.event_type)
        validate_technical_identifier(self.idempotency_key)
        validate_vault_request_fingerprint(self.request_hash, vault_id=self.vault_id)
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
    request_hash: VaultRequestFingerprint
    provider_idempotency_key: str
    authorization_id: uuid.UUID
    authorization_generation: int
    resource_id: uuid.UUID
    initial_fence: FenceSnapshot
    max_attempts: int = 3
    max_reconciliation_attempts: int = 10

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
        validate_resource_version_identifier(self.local_resource_version)
        validate_technical_identifier(self.provider_idempotency_key)
        if self.authorization_generation < 0:
            raise ValueError("authorization generation cannot be negative")
        if self.max_attempts < 1:
            raise ValueError("outbound max_attempts must be positive")
        if self.max_reconciliation_attempts < 1:
            raise ValueError("outbound max_reconciliation_attempts must be positive")
        if self.initial_fence.tombstoned:
            raise ValueError("cannot authorize an operation for a tombstoned resource")
        validate_vault_request_fingerprint(self.request_hash, vault_id=self.vault_id)


@dataclass(frozen=True, slots=True)
class ExactOutboundAuthorization:
    """Every value an authorization port must atomically match and consume."""

    outbound_operation_id: uuid.UUID
    vault_id: uuid.UUID
    authorization_id: uuid.UUID
    authorization_generation: int
    connector: str
    operation: str
    resource_id: uuid.UUID
    local_resource_version: str
    provider_idempotency_key: str
    request_hash: str
    initial_fence: FenceSnapshot
    max_attempts: int
    max_reconciliation_attempts: int


@dataclass(frozen=True, slots=True)
class StagedOutboundExecution:
    """An authorization/CAS staged in the caller's still-uncommitted transaction."""

    operation_id: uuid.UUID
    vault_id: uuid.UUID
    execution_generation: int


@dataclass(frozen=True, slots=True)
class OutboundExecutionTicket:
    """A committed, freshly gated execution lease usable for provider I/O."""

    operation_id: uuid.UUID
    vault_id: uuid.UUID
    execution_generation: int
    _transaction: AsyncSessionTransaction = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OutboundReconciliationTicket:
    """A freshly authorized provider-query lease bound to one database transaction."""

    operation_id: uuid.UUID
    vault_id: uuid.UUID
    reconciliation_generation: int
    _transaction: AsyncSessionTransaction = field(repr=False, compare=False)


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


class ExactOutboundAuthorizationPort(Protocol):
    """Database port for a one-time, payload-exact user authorization.

    Implementations must use ``session`` to lock and CAS an unused, unexpired, unrevoked
    authorization to this operation. A replay may return ``ACCEPTED`` only for the same operation
    and identical binding, so a provider-confirmed-absent network retry can revalidate the same
    one-logical-side-effect grant. Replays must still reject an expired or revoked grant. Every
    rejection must be read-only and return its fail-closed decision. The port must neither commit
    nor roll back, and no network access belongs in it.
    """

    async def consume_exact(
        self,
        session: AsyncSession,
        *,
        binding: ExactOutboundAuthorization,
    ) -> OutboundAuthorizationDecision: ...


def assert_same_request(existing_hash: str, requested_hash: str) -> None:
    """Enforce scoped-key replay semantics without leaking either fingerprint."""

    if not secrets.compare_digest(existing_hash, requested_hash):
        raise IdempotencyConflict("idempotency key was reused with a different request")
