"""PostgreSQL statements and transaction-scoped repositories for jobs/outbox.

Repositories intentionally never commit. Domain writes, job/outbox writes, materialization, and
result promotion therefore remain under the caller's single database transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import (
    Boolean,
    Interval,
    String,
    Uuid,
    and_,
    bindparam,
    case,
    func,
    literal_column,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select

from life_coach.jobs.contracts import (
    AuthoritativeFenceReader,
    CrossVaultAccessError,
    DispatchLease,
    ExactOutboundAuthorization,
    ExactOutboundAuthorizationPort,
    FenceSnapshot,
    FenceViolation,
    IdempotencyConflict,
    IdempotentWrite,
    JobExecutionContext,
    JobSpec,
    LeaseLostError,
    OutboundExecutionGuardError,
    OutboundExecutionTicket,
    OutboundOperationSpec,
    OutboundReconciliationTicket,
    OutboxEventSpec,
    PersistResult,
    ProcessorScopeError,
    StagedOutboundExecution,
    assert_same_request,
)
from life_coach.jobs.enums import (
    CompletionStatus,
    FailureClass,
    FenceCheckpoint,
    JobQueue,
    JobState,
    OutboundAuthorizationDecision,
    OutboundOperationState,
    ReconciliationOutcome,
)
from life_coach.jobs.models import Job, OutboundOperation, OutboxEvent
from life_coach.jobs.payloads import (
    SafePayload,
    validate_provider_identifier,
    validate_routing_name,
    validate_safe_payload,
    validate_technical_identifier,
)
from life_coach.jobs.queueing import queue_metadata
from life_coach.jobs.retry import retry_decision
from life_coach.jobs.state_machine import (
    assert_job_transition,
    check_execution_fence,
    reconciliation_target,
)

_SAFE_FAILURE_MESSAGES = {
    FailureClass.TRANSIENT: "temporary dependency failure",
    FailureClass.RATE_LIMITED: "dependency rate limit",
    FailureClass.DETERMINISTIC: "request cannot be processed",
    FailureClass.POLICY_REVOKED: "authorization changed",
    FailureClass.SOURCE_CHANGED: "source version changed",
    FailureClass.TOMBSTONED: "source is no longer available",
    FailureClass.EXTERNAL_OUTCOME_UNKNOWN: "external outcome requires reconciliation",
}


def set_local_vault_statement() -> Select[tuple[str]]:
    """Use PostgreSQL's transaction-local equivalent of ``SET LOCAL`` safely."""

    return select(
        func.set_config(
            literal_column("'app.vault_id'"),
            bindparam("processor_vault_id", type_=String()),
            true(),
        )
    )


def _outbound_binding_match(
    binding: ExactOutboundAuthorization,
) -> ColumnElement[bool]:
    """Match every immutable authorization, payload, routing, and retry-budget field."""

    return and_(
        OutboundOperation.authorization_id == binding.authorization_id,
        OutboundOperation.authorization_generation == binding.authorization_generation,
        OutboundOperation.connector == binding.connector,
        OutboundOperation.operation == binding.operation,
        OutboundOperation.resource_id == binding.resource_id,
        OutboundOperation.local_resource_version == binding.local_resource_version,
        OutboundOperation.provider_idempotency_key == binding.provider_idempotency_key,
        OutboundOperation.request_hash == binding.request_hash,
        OutboundOperation.policy_epoch == binding.initial_fence.policy_epoch,
        OutboundOperation.source_generation == binding.initial_fence.source_generation,
        OutboundOperation.initial_tombstoned == binding.initial_fence.tombstoned,
        OutboundOperation.max_attempts == binding.max_attempts,
        OutboundOperation.max_reconciliation_attempts == binding.max_reconciliation_attempts,
    )


def current_claim_binding_statement() -> Select[tuple[uuid.UUID, int, int]]:
    """Read the durable resource/fence only for a live exact lease."""

    return select(Job.resource_id, Job.policy_epoch, Job.source_generation).where(
        Job.id == bindparam("claim_job_id", type_=Uuid(as_uuid=True)),
        Job.vault_id == bindparam("claim_vault_id", type_=Uuid(as_uuid=True)),
        Job.state == JobState.RUNNING,
        Job.lease_owner == bindparam("claim_lease_owner", type_=String()),
        Job.lease_generation == bindparam("claim_lease_generation"),
        Job.lease_expires_at > func.clock_timestamp(),
    )


def lock_current_claim_binding_statement() -> Select[tuple[uuid.UUID]]:
    """After authority is locked, lock and revalidate the exact Job binding."""

    return (
        select(Job.id)
        .where(
            Job.id == bindparam("claim_job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("claim_vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("claim_lease_owner", type_=String()),
            Job.lease_generation == bindparam("claim_lease_generation"),
            Job.resource_id == bindparam("claim_resource_id", type_=Uuid(as_uuid=True)),
            Job.policy_epoch == bindparam("claim_policy_epoch"),
            Job.source_generation == bindparam("claim_source_generation"),
            Job.lease_expires_at > func.clock_timestamp(),
        )
        .with_for_update()
    )


def claim_job_statement() -> Update:
    """Build the one-statement DB-clock claim/reclaim operation.

    The RETURNING projection is deliberately the Dispatcher's entire data surface.
    """

    candidate = (
        select(Job.id)
        .where(
            Job.queue == bindparam("queue", type_=String()),
            Job.attempts < Job.max_attempts,
            or_(
                and_(
                    Job.state.in_((JobState.QUEUED, JobState.RETRYING)),
                    Job.run_after <= func.clock_timestamp(),
                ),
                and_(
                    Job.state == JobState.RUNNING,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= func.clock_timestamp(),
                ),
            ),
        )
        .order_by(Job.priority.desc(), Job.run_after, Job.created_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
        .cte("claim_candidate")
    )
    return (
        update(Job)
        .where(Job.id == candidate.c.id)
        .values(
            state=JobState.RUNNING,
            attempts=Job.attempts + 1,
            lease_owner=bindparam("lease_owner", type_=String()),
            lease_expires_at=func.clock_timestamp() + bindparam("lease_for", type_=Interval()),
            lease_generation=Job.lease_generation + 1,
            completed_at=None,
        )
        .returning(
            Job.id.label("job_id"),
            Job.vault_id.label("vault_id"),
            Job.lease_generation.label("lease_generation"),
        )
    )


def claim_job_type_statement() -> Update:
    """Claim one exact worker-owned type without consuming neighboring queue work."""

    candidate = (
        select(Job.id)
        .where(
            Job.queue == bindparam("queue", type_=String()),
            Job.job_type == bindparam("job_type", type_=String()),
            Job.cancel_requested_at.is_(None),
            Job.attempts < Job.max_attempts,
            or_(
                and_(
                    Job.state.in_((JobState.QUEUED, JobState.RETRYING)),
                    Job.run_after <= func.clock_timestamp(),
                ),
                and_(
                    Job.state == JobState.RUNNING,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= func.clock_timestamp(),
                ),
            ),
        )
        .order_by(Job.priority.desc(), Job.run_after, Job.created_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
        .cte("typed_claim_candidate")
    )
    return (
        update(Job)
        .where(Job.id == candidate.c.id)
        .values(
            state=JobState.RUNNING,
            attempts=Job.attempts + 1,
            lease_owner=bindparam("lease_owner", type_=String()),
            lease_expires_at=func.clock_timestamp() + bindparam("lease_for", type_=Interval()),
            lease_generation=Job.lease_generation + 1,
            completed_at=None,
        )
        .returning(
            Job.id.label("job_id"),
            Job.vault_id.label("vault_id"),
            Job.lease_generation.label("lease_generation"),
        )
    )


def expire_exhausted_leases_statement() -> Update:
    """Prevent an expired, max-attempt running job from remaining stranded forever."""

    return (
        update(Job)
        .where(
            Job.state == JobState.RUNNING,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at <= func.clock_timestamp(),
            Job.attempts >= Job.max_attempts,
        )
        .values(
            state=JobState.DEAD,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.clock_timestamp(),
            last_error_class="attempts_exhausted",
            safe_error_message="maximum processing attempts reached",
        )
        .returning(Job.id)
    )


def heartbeat_job_statement() -> Update:
    return (
        update(Job)
        .where(
            Job.id == bindparam("hb_job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("hb_vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("hb_claimed_by", type_=String()),
            Job.lease_generation == bindparam("hb_lease_generation"),
            Job.lease_expires_at > func.clock_timestamp(),
        )
        .values(
            lease_expires_at=func.clock_timestamp() + bindparam("hb_lease_for", type_=Interval())
        )
        .returning(Job.id)
    )


def complete_job_statement() -> Update:
    """Condition completion on lease plus the caller's locked authoritative fence snapshot."""

    return (
        update(Job)
        .where(
            Job.id == bindparam("complete_job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("complete_vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("complete_claimed_by", type_=String()),
            Job.lease_generation == bindparam("complete_lease_generation"),
            Job.lease_expires_at > func.clock_timestamp(),
            Job.policy_epoch == bindparam("complete_policy_epoch"),
            Job.source_generation == bindparam("complete_source_generation"),
        )
        .values(
            state=JobState.DONE,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.clock_timestamp(),
            last_error_class=None,
            safe_error_message=None,
        )
        .returning(Job.id)
    )


def cancel_claim_statement() -> Update:
    return (
        update(Job)
        .where(
            Job.id == bindparam("cancel_job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("cancel_vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("cancel_claimed_by", type_=String()),
            Job.lease_generation == bindparam("cancel_lease_generation"),
            Job.lease_expires_at > func.clock_timestamp(),
        )
        .values(
            state=JobState.CANCELED,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.clock_timestamp(),
            last_error_class="execution_fence_rejected",
            safe_error_message="authorization or source state changed",
        )
        .returning(Job.id)
    )


def failure_job_statement(target_state: JobState) -> Update:
    assert_job_transition(JobState.RUNNING, target_state)
    values: dict[str, object] = {
        "state": target_state,
        "lease_owner": None,
        "lease_expires_at": None,
        "last_error_class": bindparam("last_error_class", type_=String()),
        "safe_error_message": bindparam("safe_error_message", type_=String()),
    }
    if target_state is JobState.RETRYING:
        values["run_after"] = func.clock_timestamp() + bindparam("retry_delay", type_=Interval())
    if target_state in {JobState.DEAD, JobState.CANCELED}:
        values["completed_at"] = func.clock_timestamp()
    return (
        update(Job)
        .where(
            Job.id == bindparam("failure_job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("failure_vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("failure_claimed_by", type_=String()),
            Job.lease_generation == bindparam("failure_lease_generation"),
            Job.lease_expires_at > func.clock_timestamp(),
        )
        .values(**values)
        .returning(Job.id)
    )


def resolve_waiting_job_statement(target_state: JobState) -> Update:
    """Resolve waiting only after an explicit user/event/reconciliation decision."""

    if target_state not in {JobState.QUEUED, JobState.DEAD, JobState.CANCELED}:
        raise ValueError("waiting jobs may only resume, dead-letter, or cancel")
    values: dict[str, object]
    if target_state is JobState.QUEUED:
        retryable = Job.attempts < Job.max_attempts
        values = {
            "state": case((retryable, JobState.QUEUED.value), else_=JobState.DEAD.value),
            "run_after": func.clock_timestamp(),
            "completed_at": case((retryable, None), else_=func.clock_timestamp()),
            "last_error_class": case((retryable, None), else_="attempts_exhausted"),
            "safe_error_message": case(
                (retryable, None), else_="maximum processing attempts reached"
            ),
        }
    else:
        values = {
            "state": target_state,
            "last_error_class": None,
            "safe_error_message": None,
            "completed_at": func.clock_timestamp(),
        }
    return (
        update(Job)
        .where(
            Job.id == bindparam("waiting_job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("waiting_vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.WAITING,
        )
        .values(**values)
        .returning(Job.id, Job.state)
    )


def begin_outbound_execution_statement() -> Update:
    """CAS the exact durable authorization and privacy binding into execution."""

    return (
        update(OutboundOperation)
        .where(
            OutboundOperation.id == bindparam("operation_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.state == OutboundOperationState.PENDING,
            OutboundOperation.attempts < OutboundOperation.max_attempts,
            OutboundOperation.max_attempts == bindparam("expected_max_attempts"),
            OutboundOperation.max_reconciliation_attempts
            == bindparam("expected_max_reconciliation_attempts"),
            OutboundOperation.authorization_id
            == bindparam("expected_authorization_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.authorization_generation
            == bindparam("expected_authorization_generation"),
            OutboundOperation.resource_id
            == bindparam("expected_resource_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.policy_epoch == bindparam("expected_policy_epoch"),
            OutboundOperation.source_generation == bindparam("expected_source_generation"),
            OutboundOperation.initial_tombstoned
            == bindparam("expected_initial_tombstoned", type_=Boolean()),
            OutboundOperation.connector == bindparam("expected_connector", type_=String()),
            OutboundOperation.operation == bindparam("expected_operation", type_=String()),
            OutboundOperation.local_resource_version
            == bindparam("expected_local_resource_version", type_=String()),
            OutboundOperation.request_hash == bindparam("expected_request_hash", type_=String()),
            OutboundOperation.provider_idempotency_key
            == bindparam("expected_provider_idempotency_key", type_=String()),
        )
        .values(
            state=OutboundOperationState.EXECUTING,
            attempts=OutboundOperation.attempts + 1,
            execution_generation=OutboundOperation.execution_generation + 1,
            execution_started_at=func.clock_timestamp(),
            execution_expires_at=func.clock_timestamp()
            + bindparam("execution_for", type_=Interval()),
            updated_at=func.clock_timestamp(),
        )
        .returning(OutboundOperation.execution_generation)
    )


def expire_uncertain_outbound_executions_statement() -> Update:
    """Move crashed/expired executions to unknown so they reconcile instead of retrying."""

    return (
        update(OutboundOperation)
        .where(
            OutboundOperation.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.state == OutboundOperationState.EXECUTING,
            OutboundOperation.execution_expires_at.is_not(None),
            OutboundOperation.execution_expires_at <= func.clock_timestamp(),
        )
        .values(
            state=OutboundOperationState.UNKNOWN,
            execution_started_at=None,
            execution_expires_at=None,
            last_error_class=FailureClass.EXTERNAL_OUTCOME_UNKNOWN.value,
            safe_error_message=_SAFE_FAILURE_MESSAGES[FailureClass.EXTERNAL_OUTCOME_UNKNOWN],
            updated_at=func.clock_timestamp(),
        )
        .returning(OutboundOperation.id)
    )


def expire_stale_reconciliations_statement() -> Update:
    """Return a crashed reconciliation to unknown; execution is still forbidden."""

    return (
        update(OutboundOperation)
        .where(
            OutboundOperation.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.state == OutboundOperationState.RECONCILING,
            OutboundOperation.reconciliation_expires_at.is_not(None),
            OutboundOperation.reconciliation_expires_at <= func.clock_timestamp(),
        )
        .values(
            state=OutboundOperationState.UNKNOWN,
            reconciliation_started_at=None,
            reconciliation_expires_at=None,
            updated_at=func.clock_timestamp(),
        )
        .returning(OutboundOperation.id)
    )


def _canonical_payload(
    *, vault_id: uuid.UUID, resource_id: uuid.UUID, pipeline_version: str
) -> SafePayload:
    return validate_safe_payload(
        {
            "vault_id": str(vault_id),
            "resource_id": str(resource_id),
            "pipeline_version": pipeline_version,
        }
    )


def _validated_canonical_payload(
    supplied: SafePayload,
    *,
    vault_id: uuid.UUID,
    resource_id: uuid.UUID,
    pipeline_version: str,
) -> SafePayload:
    canonical = _canonical_payload(
        vault_id=vault_id,
        resource_id=resource_id,
        pipeline_version=pipeline_version,
    )
    validated = validate_safe_payload(supplied)
    if validated and validated != canonical:
        raise ValueError("payload routing metadata does not match its typed columns")
    return canonical


def _job_values(spec: JobSpec, *, outbox_event_id: uuid.UUID | None = None) -> dict[str, object]:
    return {
        "vault_id": spec.vault_id,
        "job_type": spec.job_type,
        "queue": spec.queue,
        "resource_id": spec.resource_id,
        "resource_revision_id": spec.resource_revision_id,
        "pipeline_version": spec.pipeline_version,
        "idempotency_key": spec.idempotency_key,
        "request_hash": spec.request_hash,
        "outbox_event_id": outbox_event_id,
        "requested_by_principal_id": spec.requested_by_principal_id,
        "membership_generation": spec.membership_generation,
        "expected_resource_revision": spec.expected_resource_revision,
        "payload": _validated_canonical_payload(
            spec.payload,
            vault_id=spec.vault_id,
            resource_id=spec.resource_id,
            pipeline_version=spec.pipeline_version,
        ),
        "state": JobState.QUEUED,
        "priority": spec.priority
        if spec.priority is not None
        else queue_metadata(spec.queue).default_priority,
        "attempts": 0,
        "max_attempts": spec.max_attempts,
        "run_after": (spec.run_after if spec.run_after is not None else func.clock_timestamp()),
        "lease_generation": 0,
        "consent_snapshot_id": spec.consent_snapshot_id,
        "policy_epoch": spec.policy_epoch,
        "source_generation": spec.source_generation,
    }


class GlobalDispatcherRepository:
    """Metadata-only global queue access. It never loads an ORM Job or its payload."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        *,
        queue: JobQueue,
        lease_owner: str,
        lease_for: timedelta,
    ) -> DispatchLease | None:
        if not lease_owner or lease_for <= timedelta(0):
            raise ValueError("lease owner and positive duration are required")
        validate_technical_identifier(lease_owner)
        result = await self._session.execute(
            claim_job_statement(),
            {"queue": queue.value, "lease_owner": lease_owner, "lease_for": lease_for},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return DispatchLease(
            job_id=row["job_id"],
            vault_id=row["vault_id"],
            lease_generation=row["lease_generation"],
        )

    async def expire_exhausted_leases(self) -> tuple[uuid.UUID, ...]:
        result = await self._session.execute(expire_exhausted_leases_statement())
        return tuple(result.scalars())

    async def claim_type(
        self,
        *,
        queue: JobQueue,
        job_type: str,
        lease_owner: str,
        lease_for: timedelta,
    ) -> DispatchLease | None:
        if not lease_owner or lease_for <= timedelta(0):
            raise ValueError("lease owner and positive duration are required")
        validate_technical_identifier(lease_owner)
        validate_routing_name(job_type)
        result = await self._session.execute(
            claim_job_type_statement(),
            {
                "queue": queue.value,
                "job_type": job_type,
                "lease_owner": lease_owner,
                "lease_for": lease_for,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return DispatchLease(
            job_id=row["job_id"],
            vault_id=row["vault_id"],
            lease_generation=row["lease_generation"],
        )

    async def claim_candidate_insight(
        self,
        *,
        lease_owner: str,
        lease_for: timedelta,
    ) -> DispatchLease | None:
        """Call the narrow SECURITY DEFINER dispatcher boundary for this worker type."""

        if not lease_owner or lease_for <= timedelta(0):
            raise ValueError("lease owner and positive duration are required")
        validate_technical_identifier(lease_owner)
        seconds = int(lease_for.total_seconds())
        if not 1 <= seconds <= 600:
            raise ValueError("candidate insight lease duration must be within 1..600 seconds")
        result = await self._session.execute(
            text(
                "SELECT job_id, vault_id, lease_generation FROM "
                "life_coach_private.claim_candidate_insight_job(:lease_owner, :lease_seconds)"
            ),
            {"lease_owner": lease_owner, "lease_seconds": seconds},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return DispatchLease(
            job_id=row["job_id"],
            vault_id=row["vault_id"],
            lease_generation=row["lease_generation"],
        )


class VaultProcessorRepository:
    """Processor operations bound for their lifetime to exactly one vault transaction."""

    def __init__(self, session: AsyncSession, vault_id: uuid.UUID) -> None:
        self._session = session
        self.vault_id = vault_id
        self._scope_transaction: AsyncSessionTransaction | None = None

    async def initialize_scope(self) -> None:
        await self._session.execute(
            set_local_vault_statement(), {"processor_vault_id": str(self.vault_id)}
        )
        transaction = self._session.get_transaction()
        if transaction is None:
            raise ProcessorScopeError("processor scope requires an active transaction")
        self._scope_transaction = transaction

    def _require_scope(self) -> None:
        current = self._session.get_transaction()
        if (
            self._scope_transaction is None
            or current is not self._scope_transaction
            or not self._scope_transaction.is_active
        ):
            raise ProcessorScopeError("processor transaction has no local vault scope")

    def _require_claim(self, lease: DispatchLease) -> None:
        self._require_scope()
        if lease.vault_id != self.vault_id:
            raise CrossVaultAccessError("processor lease belongs to another vault")

    async def _live_claim_binding(
        self,
        lease: DispatchLease,
        *,
        lease_owner: str,
    ) -> tuple[uuid.UUID, FenceSnapshot] | None:
        """Use the database clock and row lock; never trust a caller-carried fence snapshot."""

        validate_technical_identifier(lease_owner)
        result = await self._session.execute(
            current_claim_binding_statement(),
            {
                "claim_job_id": lease.job_id,
                "claim_vault_id": lease.vault_id,
                "claim_lease_owner": lease_owner,
                "claim_lease_generation": lease.lease_generation,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return (
            row["resource_id"],
            FenceSnapshot(
                policy_epoch=row["policy_epoch"],
                source_generation=row["source_generation"],
            ),
        )

    async def _lock_live_claim_binding(
        self,
        lease: DispatchLease,
        *,
        lease_owner: str,
        resource_id: uuid.UUID,
        expected_fence: FenceSnapshot,
    ) -> bool:
        result = await self._session.execute(
            lock_current_claim_binding_statement(),
            {
                "claim_job_id": lease.job_id,
                "claim_vault_id": lease.vault_id,
                "claim_lease_owner": lease_owner,
                "claim_lease_generation": lease.lease_generation,
                "claim_resource_id": resource_id,
                "claim_policy_epoch": expected_fence.policy_epoch,
                "claim_source_generation": expected_fence.source_generation,
            },
        )
        return result.scalar_one_or_none() is not None

    async def load_execution_context(
        self, lease: DispatchLease, *, lease_owner: str
    ) -> JobExecutionContext | None:
        self._require_claim(lease)
        validate_technical_identifier(lease_owner)
        statement = select(
            Job.job_type,
            Job.queue,
            Job.resource_id,
            Job.resource_revision_id,
            Job.pipeline_version,
            Job.idempotency_key,
            Job.consent_snapshot_id,
            Job.policy_epoch,
            Job.source_generation,
            Job.payload,
            Job.attempts,
            Job.max_attempts,
            Job.requested_by_principal_id,
            Job.membership_generation,
            Job.expected_resource_revision,
            Job.cancel_requested_at,
        ).where(
            Job.id == lease.job_id,
            Job.vault_id == self.vault_id,
            Job.state == JobState.RUNNING,
            Job.lease_owner == lease_owner,
            Job.lease_generation == lease.lease_generation,
            Job.lease_expires_at > func.clock_timestamp(),
        )
        result = await self._session.execute(statement)
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return JobExecutionContext(
            lease=lease,
            job_type=row["job_type"],
            queue=JobQueue(row["queue"]),
            resource_id=row["resource_id"],
            resource_revision_id=row["resource_revision_id"],
            pipeline_version=row["pipeline_version"],
            idempotency_key=row["idempotency_key"],
            consent_snapshot_id=row["consent_snapshot_id"],
            expected_fence=FenceSnapshot(
                policy_epoch=row["policy_epoch"],
                source_generation=row["source_generation"],
            ),
            payload=validate_safe_payload(row["payload"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            requested_by_principal_id=row["requested_by_principal_id"],
            membership_generation=row["membership_generation"],
            expected_resource_revision=row["expected_resource_revision"],
            cancel_requested=row["cancel_requested_at"] is not None,
        )

    async def mark_done_after_external_commit(
        self,
        context: JobExecutionContext,
        *,
        lease_owner: str,
    ) -> bool:
        """Settle bookkeeping after an independently fenced, idempotent result commit."""

        self._require_claim(context.lease)
        validate_technical_identifier(lease_owner)
        result = await self._session.execute(
            complete_job_statement(),
            {
                "complete_job_id": context.lease.job_id,
                "complete_vault_id": context.lease.vault_id,
                "complete_lease_generation": context.lease.lease_generation,
                "complete_claimed_by": lease_owner,
                "complete_policy_epoch": context.expected_fence.policy_epoch,
                "complete_source_generation": context.expected_fence.source_generation,
            },
        )
        return result.scalar_one_or_none() is not None

    async def mark_canceled_by_request(
        self,
        context: JobExecutionContext,
        *,
        lease_owner: str,
    ) -> bool:
        self._require_claim(context.lease)
        validate_technical_identifier(lease_owner)
        result = await self._session.execute(
            cancel_claim_statement(),
            {
                "cancel_job_id": context.lease.job_id,
                "cancel_vault_id": context.lease.vault_id,
                "cancel_lease_generation": context.lease.lease_generation,
                "cancel_claimed_by": lease_owner,
            },
        )
        return result.scalar_one_or_none() is not None

    async def gate_authoritatively(
        self,
        context: JobExecutionContext,
        checkpoint: FenceCheckpoint,
        fence_reader: AuthoritativeFenceReader,
        *,
        lease_owner: str,
    ) -> FenceSnapshot:
        """Lock a live lease and authority immediately before sensitive processing."""

        self._require_claim(context.lease)
        if checkpoint not in {
            FenceCheckpoint.BEFORE_SOURCE_READ,
            FenceCheckpoint.BEFORE_EXTERNAL_CALL,
        }:
            raise ValueError("authoritative gate is only valid before source or external access")
        durable = await self._live_claim_binding(context.lease, lease_owner=lease_owner)
        if durable is None:
            raise LeaseLostError("job lease is no longer current")
        resource_id, expected_fence = durable
        current = await fence_reader.read_for_update(
            self._session,
            vault_id=self.vault_id,
            resource_id=resource_id,
        )
        if not await self._lock_live_claim_binding(
            context.lease,
            lease_owner=lease_owner,
            resource_id=resource_id,
            expected_fence=expected_fence,
        ):
            raise LeaseLostError("job lease changed while authority was being checked")
        check_execution_fence(expected_fence, current, checkpoint)
        return current

    async def heartbeat(
        self,
        lease: DispatchLease,
        *,
        lease_owner: str,
        lease_for: timedelta,
    ) -> bool:
        self._require_claim(lease)
        if lease_for <= timedelta(0):
            raise ValueError("heartbeat duration must be positive")
        validate_technical_identifier(lease_owner)
        result = await self._session.execute(
            heartbeat_job_statement(),
            {
                "hb_job_id": lease.job_id,
                "hb_vault_id": lease.vault_id,
                "hb_lease_generation": lease.lease_generation,
                "hb_claimed_by": lease_owner,
                "hb_lease_for": lease_for,
            },
        )
        return result.scalar_one_or_none() is not None

    async def complete(
        self,
        context: JobExecutionContext,
        *,
        lease_owner: str,
        fence_reader: AuthoritativeFenceReader,
        persist_result: PersistResult,
    ) -> CompletionStatus:
        """Fence, conditionally finish, then promote the result in the same caller transaction."""

        self._require_claim(context.lease)
        async with self._session.begin_nested():
            durable = await self._live_claim_binding(context.lease, lease_owner=lease_owner)
            if durable is None:
                return CompletionStatus.LEASE_LOST
            resource_id, expected_fence = durable
            current_fence = await fence_reader.read_for_update(
                self._session,
                vault_id=self.vault_id,
                resource_id=resource_id,
            )
            if not await self._lock_live_claim_binding(
                context.lease,
                lease_owner=lease_owner,
                resource_id=resource_id,
                expected_fence=expected_fence,
            ):
                return CompletionStatus.LEASE_LOST
            try:
                check_execution_fence(
                    expected_fence,
                    current_fence,
                    FenceCheckpoint.BEFORE_RESULT_COMMIT,
                )
            except FenceViolation:
                canceled = await self._session.execute(
                    cancel_claim_statement(),
                    {
                        "cancel_job_id": context.lease.job_id,
                        "cancel_vault_id": context.lease.vault_id,
                        "cancel_lease_generation": context.lease.lease_generation,
                        "cancel_claimed_by": lease_owner,
                    },
                )
                if canceled.scalar_one_or_none() is None:
                    return CompletionStatus.LEASE_LOST
                return CompletionStatus.DISCARDED

            completed = await self._session.execute(
                complete_job_statement(),
                {
                    "complete_job_id": context.lease.job_id,
                    "complete_vault_id": context.lease.vault_id,
                    "complete_lease_generation": context.lease.lease_generation,
                    "complete_claimed_by": lease_owner,
                    "complete_policy_epoch": expected_fence.policy_epoch,
                    "complete_source_generation": expected_fence.source_generation,
                },
            )
            if completed.scalar_one_or_none() is None:
                return CompletionStatus.LEASE_LOST
            await persist_result(self._session)
            return CompletionStatus.DONE

    async def record_failure(
        self,
        context: JobExecutionContext,
        *,
        lease_owner: str,
        failure: FailureClass,
        random_sample: float,
    ) -> JobState | None:
        self._require_claim(context.lease)
        validate_technical_identifier(lease_owner)
        decision = retry_decision(
            failure,
            attempts=context.attempts,
            max_attempts=context.max_attempts,
            random_sample=lambda: random_sample,
        )
        statement = failure_job_statement(decision.target_state)
        params: dict[str, object] = {
            "failure_job_id": context.lease.job_id,
            "failure_vault_id": context.lease.vault_id,
            "failure_lease_generation": context.lease.lease_generation,
            "failure_claimed_by": lease_owner,
            "last_error_class": failure.value,
            "safe_error_message": _SAFE_FAILURE_MESSAGES[failure],
        }
        if decision.delay is not None:
            params["retry_delay"] = decision.delay
        result = await self._session.execute(statement, params)
        if result.scalar_one_or_none() is None:
            return None
        return decision.target_state


class VaultJobRepository:
    """Scoped enqueue/outbox operations used inside an existing domain transaction."""

    def __init__(self, session: AsyncSession, vault_id: uuid.UUID) -> None:
        self._session = session
        self.vault_id = vault_id

    def _require_vault(self, vault_id: uuid.UUID) -> None:
        if vault_id != self.vault_id:
            raise CrossVaultAccessError("repository spec belongs to another vault")

    async def enqueue(self, spec: JobSpec) -> IdempotentWrite:
        self._require_vault(spec.vault_id)
        insert = (
            pg_insert(Job).values(**_job_values(spec)).on_conflict_do_nothing().returning(Job.id)
        )
        inserted = (await self._session.execute(insert)).scalar_one_or_none()
        if inserted is not None:
            return IdempotentWrite(inserted, created=True)
        existing = await self._session.execute(
            select(Job.id, Job.request_hash).where(
                Job.vault_id == self.vault_id,
                Job.job_type == spec.job_type,
                Job.idempotency_key == spec.idempotency_key,
            )
        )
        row = existing.mappings().one_or_none()
        if row is not None:
            assert_same_request(row["request_hash"], spec.request_hash)
            return IdempotentWrite(row["id"], created=False)
        if spec.resource_revision_id is not None:
            derivative = await self._session.execute(
                select(Job.id, Job.request_hash).where(
                    Job.vault_id == self.vault_id,
                    Job.job_type == spec.job_type,
                    Job.resource_revision_id == spec.resource_revision_id,
                    Job.pipeline_version == spec.pipeline_version,
                )
            )
            row = derivative.mappings().one_or_none()
            if row is not None:
                assert_same_request(row["request_hash"], spec.request_hash)
                return IdempotentWrite(row["id"], created=False)
        raise IdempotencyConflict("job conflicts with an existing uniqueness scope")

    async def add_outbox_event(self, spec: OutboxEventSpec) -> IdempotentWrite:
        self._require_vault(spec.vault_id)
        payload = _validated_canonical_payload(
            spec.payload,
            vault_id=spec.vault_id,
            resource_id=spec.resource_id,
            pipeline_version=spec.pipeline_version,
        )
        insert = (
            pg_insert(OutboxEvent)
            .values(
                vault_id=spec.vault_id,
                event_type=spec.event_type,
                resource_id=spec.resource_id,
                resource_revision_id=spec.resource_revision_id,
                pipeline_version=spec.pipeline_version,
                idempotency_key=spec.idempotency_key,
                request_hash=spec.request_hash,
                payload=payload,
                expected_job_types=list(spec.subscriber_job_types),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    OutboxEvent.vault_id,
                    OutboxEvent.event_type,
                    OutboxEvent.idempotency_key,
                ]
            )
            .returning(OutboxEvent.id)
        )
        inserted = (await self._session.execute(insert)).scalar_one_or_none()
        if inserted is not None:
            return IdempotentWrite(inserted, created=True)
        existing = await self._session.execute(
            select(OutboxEvent.id, OutboxEvent.request_hash).where(
                OutboxEvent.vault_id == self.vault_id,
                OutboxEvent.event_type == spec.event_type,
                OutboxEvent.idempotency_key == spec.idempotency_key,
            )
        )
        row = existing.mappings().one()
        assert_same_request(row["request_hash"], spec.request_hash)
        return IdempotentWrite(row["id"], created=False)

    async def resolve_waiting_job(
        self, job_id: uuid.UUID, *, target_state: JobState
    ) -> JobState | None:
        """Resume after reconciliation or terminate after an explicit final decision."""

        result = await self._session.execute(
            resolve_waiting_job_statement(target_state),
            {"waiting_job_id": job_id, "waiting_vault_id": self.vault_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return JobState(row["state"])

    async def materialize_outbox_jobs(
        self, outbox_event_id: uuid.UUID, specs: Sequence[JobSpec]
    ) -> tuple[IdempotentWrite, ...]:
        """Materialize every subscriber, then mark dispatched, without committing.

        Replays do not skip already-dispatched events, so adding/repairing another subscriber type
        remains safe. The caller must pass the complete intended subscriber set per transaction.
        """

        if not specs:
            raise ValueError("at least one subscriber job is required")
        event_result = await self._session.execute(
            select(
                OutboxEvent.id,
                OutboxEvent.vault_id,
                OutboxEvent.resource_id,
                OutboxEvent.resource_revision_id,
                OutboxEvent.pipeline_version,
                OutboxEvent.expected_job_types,
            )
            .where(
                OutboxEvent.id == outbox_event_id,
                OutboxEvent.vault_id == self.vault_id,
            )
            .with_for_update()
        )
        event = event_result.mappings().one_or_none()
        if event is None:
            raise LookupError("outbox event was not found in the processor vault")

        expected_types = tuple(event["expected_job_types"])
        supplied_types = tuple(spec.job_type for spec in specs)
        if (
            not expected_types
            or len(set(supplied_types)) != len(supplied_types)
            or set(supplied_types) != set(expected_types)
        ):
            raise ValueError("subscriber set does not match the durable outbox contract")

        writes: list[IdempotentWrite] = []
        for spec in specs:
            self._require_vault(spec.vault_id)
            if (
                spec.resource_id != event["resource_id"]
                or spec.resource_revision_id != event["resource_revision_id"]
                or spec.pipeline_version != event["pipeline_version"]
            ):
                raise ValueError("subscriber routing metadata does not match the outbox event")
            insert = (
                pg_insert(Job)
                .values(**_job_values(spec, outbox_event_id=outbox_event_id))
                .on_conflict_do_nothing(
                    index_elements=[Job.outbox_event_id, Job.job_type],
                    index_where=Job.outbox_event_id.is_not(None),
                )
                .returning(Job.id)
            )
            inserted = (await self._session.execute(insert)).scalar_one_or_none()
            if inserted is not None:
                writes.append(IdempotentWrite(inserted, created=True))
                continue
            existing_result = await self._session.execute(
                select(Job.id, Job.vault_id, Job.request_hash).where(
                    Job.outbox_event_id == outbox_event_id,
                    Job.job_type == spec.job_type,
                )
            )
            existing = existing_result.mappings().one()
            if existing["vault_id"] != self.vault_id:
                raise CrossVaultAccessError("materialized job belongs to another vault")
            assert_same_request(existing["request_hash"], spec.request_hash)
            writes.append(IdempotentWrite(existing["id"], created=False))

        await self._session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == outbox_event_id,
                OutboxEvent.vault_id == self.vault_id,
            )
            .values(dispatched_at=func.coalesce(OutboxEvent.dispatched_at, func.now()))
        )
        return tuple(writes)


class OutboundOperationRepository:
    """Single-vault external side-effect ledger with mandatory unknown reconciliation."""

    def __init__(self, session: AsyncSession, vault_id: uuid.UUID) -> None:
        self._session = session
        self.vault_id = vault_id

    def _require_vault(self, vault_id: uuid.UUID) -> None:
        if vault_id != self.vault_id:
            raise CrossVaultAccessError("outbound operation belongs to another vault")

    def _require_execution_ticket(self, ticket: OutboundExecutionTicket) -> None:
        self._require_vault(ticket.vault_id)
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or transaction is not ticket._transaction
            or not transaction.is_active
        ):
            raise OutboundExecutionGuardError(
                "execution ticket is not bound to the current active transaction"
            )

    def _require_reconciliation_ticket(self, ticket: OutboundReconciliationTicket) -> None:
        self._require_vault(ticket.vault_id)
        transaction = self._session.get_transaction()
        if (
            transaction is None
            or transaction is not ticket._transaction
            or not transaction.is_active
        ):
            raise OutboundExecutionGuardError(
                "reconciliation ticket is not bound to the current active transaction"
            )

    async def create(self, spec: OutboundOperationSpec) -> IdempotentWrite:
        self._require_vault(spec.vault_id)
        insert = (
            pg_insert(OutboundOperation)
            .values(
                vault_id=spec.vault_id,
                connector=spec.connector,
                operation=spec.operation,
                resource_id=spec.resource_id,
                local_resource_version=spec.local_resource_version,
                request_hash=spec.request_hash,
                provider_idempotency_key=spec.provider_idempotency_key,
                authorization_id=spec.authorization_id,
                authorization_generation=spec.authorization_generation,
                policy_epoch=spec.initial_fence.policy_epoch,
                source_generation=spec.initial_fence.source_generation,
                initial_tombstoned=spec.initial_fence.tombstoned,
                state=OutboundOperationState.PENDING,
                max_attempts=spec.max_attempts,
                max_reconciliation_attempts=spec.max_reconciliation_attempts,
            )
            .on_conflict_do_nothing()
            .returning(OutboundOperation.id)
        )
        inserted = (await self._session.execute(insert)).scalar_one_or_none()
        if inserted is not None:
            return IdempotentWrite(inserted, created=True)
        existing_result = await self._session.execute(
            select(
                OutboundOperation.id,
                OutboundOperation.request_hash,
                OutboundOperation.provider_idempotency_key,
                OutboundOperation.authorization_id,
                OutboundOperation.authorization_generation,
                OutboundOperation.resource_id,
                OutboundOperation.policy_epoch,
                OutboundOperation.source_generation,
                OutboundOperation.initial_tombstoned,
                OutboundOperation.max_attempts,
                OutboundOperation.max_reconciliation_attempts,
            ).where(
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.connector == spec.connector,
                OutboundOperation.operation == spec.operation,
                OutboundOperation.local_resource_version == spec.local_resource_version,
            )
        )
        existing = existing_result.mappings().one_or_none()
        if existing is not None:
            assert_same_request(existing["request_hash"], spec.request_hash)
            expected_binding = (
                spec.provider_idempotency_key,
                spec.authorization_id,
                spec.authorization_generation,
                spec.resource_id,
                spec.initial_fence.policy_epoch,
                spec.initial_fence.source_generation,
                spec.initial_fence.tombstoned,
                spec.max_attempts,
                spec.max_reconciliation_attempts,
            )
            stored_binding = (
                existing["provider_idempotency_key"],
                existing["authorization_id"],
                existing["authorization_generation"],
                existing["resource_id"],
                existing["policy_epoch"],
                existing["source_generation"],
                existing["initial_tombstoned"],
                existing["max_attempts"],
                existing["max_reconciliation_attempts"],
            )
            if stored_binding != expected_binding:
                raise IdempotencyConflict(
                    "outbound operation scope has a different immutable binding"
                )
            return IdempotentWrite(existing["id"], created=False)
        provider_conflict = await self._session.execute(
            select(OutboundOperation.id).where(
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.connector == spec.connector,
                OutboundOperation.provider_idempotency_key == spec.provider_idempotency_key,
            )
        )
        if provider_conflict.scalar_one_or_none() is not None:
            raise IdempotencyConflict(
                "provider idempotency token belongs to another operation scope"
            )
        raise IdempotencyConflict("outbound operation conflicts with a uniqueness scope")

    async def begin_execution(
        self,
        operation_id: uuid.UUID,
        *,
        authorization_port: ExactOutboundAuthorizationPort | None,
        fence_reader: AuthoritativeFenceReader | None,
        execution_for: timedelta = timedelta(minutes=2),
    ) -> StagedOutboundExecution | None:
        """Atomically stage execution without exposing a provider-I/O ticket before commit."""

        if authorization_port is None or fence_reader is None:
            raise OutboundExecutionGuardError(
                "outbound execution requires authorization and authoritative fence ports"
            )
        if execution_for <= timedelta(0):
            raise ValueError("outbound execution duration must be positive")
        binding_result = await self._session.execute(
            select(
                OutboundOperation.authorization_id,
                OutboundOperation.authorization_generation,
                OutboundOperation.connector,
                OutboundOperation.operation,
                OutboundOperation.resource_id,
                OutboundOperation.local_resource_version,
                OutboundOperation.request_hash,
                OutboundOperation.provider_idempotency_key,
                OutboundOperation.policy_epoch,
                OutboundOperation.source_generation,
                OutboundOperation.initial_tombstoned,
                OutboundOperation.attempts,
                OutboundOperation.max_attempts,
                OutboundOperation.max_reconciliation_attempts,
            ).where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.PENDING,
            )
        )
        row = binding_result.mappings().one_or_none()
        if row is None:
            return None

        expected_fence = FenceSnapshot(
            policy_epoch=row["policy_epoch"],
            source_generation=row["source_generation"],
            tombstoned=row["initial_tombstoned"],
        )
        if expected_fence.tombstoned:
            raise OutboundExecutionGuardError("outbound operation has an invalid initial fence")
        binding = ExactOutboundAuthorization(
            outbound_operation_id=operation_id,
            vault_id=self.vault_id,
            authorization_id=row["authorization_id"],
            authorization_generation=row["authorization_generation"],
            connector=row["connector"],
            operation=row["operation"],
            resource_id=row["resource_id"],
            local_resource_version=row["local_resource_version"],
            provider_idempotency_key=row["provider_idempotency_key"],
            request_hash=row["request_hash"],
            initial_fence=expected_fence,
            max_attempts=row["max_attempts"],
            max_reconciliation_attempts=row["max_reconciliation_attempts"],
        )
        if row["attempts"] >= row["max_attempts"]:
            await self._session.execute(
                update(OutboundOperation)
                .where(
                    OutboundOperation.id == operation_id,
                    OutboundOperation.vault_id == self.vault_id,
                    OutboundOperation.state == OutboundOperationState.PENDING,
                    OutboundOperation.attempts >= OutboundOperation.max_attempts,
                    _outbound_binding_match(binding),
                )
                .values(
                    state=OutboundOperationState.MANUAL_REVIEW,
                    last_error_class="outbound_attempts_exhausted",
                    safe_error_message="manual review is required",
                    updated_at=func.clock_timestamp(),
                )
            )
            return None

        async with self._session.begin_nested():
            current_fence = await fence_reader.read_for_update(
                self._session,
                vault_id=self.vault_id,
                resource_id=binding.resource_id,
            )
            try:
                check_execution_fence(
                    expected_fence,
                    current_fence,
                    FenceCheckpoint.BEFORE_EXTERNAL_CALL,
                )
            except FenceViolation:
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.PENDING,
                        _outbound_binding_match(binding),
                    )
                    .values(
                        state=OutboundOperationState.CANCELED,
                        completed_at=func.clock_timestamp(),
                        last_error_class="privacy_fence_rejected",
                        safe_error_message="authorization or source state changed",
                        updated_at=func.clock_timestamp(),
                    )
                )
                return None
            decision = await authorization_port.consume_exact(self._session, binding=binding)
            if decision is not OutboundAuthorizationDecision.ACCEPTED:
                target = (
                    OutboundOperationState.CANCELED
                    if decision is OutboundAuthorizationDecision.REVOKED
                    else OutboundOperationState.MANUAL_REVIEW
                )
                values: dict[str, object] = {
                    "state": target,
                    "last_error_class": f"authorization_{decision.value}",
                    "safe_error_message": "manual review is required",
                    "updated_at": func.clock_timestamp(),
                }
                if target is OutboundOperationState.CANCELED:
                    values["completed_at"] = func.clock_timestamp()
                    values["safe_error_message"] = "authorization was revoked"
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.PENDING,
                        _outbound_binding_match(binding),
                    )
                    .values(**values)
                )
                return None
            result = await self._session.execute(
                begin_outbound_execution_statement(),
                {
                    "operation_id": operation_id,
                    "vault_id": self.vault_id,
                    "expected_authorization_id": binding.authorization_id,
                    "expected_authorization_generation": binding.authorization_generation,
                    "expected_resource_id": binding.resource_id,
                    "expected_policy_epoch": expected_fence.policy_epoch,
                    "expected_source_generation": expected_fence.source_generation,
                    "expected_initial_tombstoned": expected_fence.tombstoned,
                    "expected_connector": binding.connector,
                    "expected_operation": binding.operation,
                    "expected_local_resource_version": binding.local_resource_version,
                    "expected_request_hash": binding.request_hash,
                    "expected_provider_idempotency_key": binding.provider_idempotency_key,
                    "expected_max_attempts": binding.max_attempts,
                    "expected_max_reconciliation_attempts": (binding.max_reconciliation_attempts),
                    "execution_for": execution_for,
                },
            )
            generation = result.scalar_one_or_none()
            if generation is None:
                raise OutboundExecutionGuardError(
                    "outbound operation changed before execution was staged"
                )
            return StagedOutboundExecution(
                operation_id=operation_id,
                vault_id=self.vault_id,
                execution_generation=int(generation),
            )

    async def load_committed_execution_ticket(
        self,
        staged: StagedOutboundExecution,
        *,
        authorization_port: ExactOutboundAuthorizationPort | None,
        fence_reader: AuthoritativeFenceReader | None,
    ) -> OutboundExecutionTicket | None:
        """Gate a committed execution in a fresh transaction immediately before provider I/O."""

        self._require_vault(staged.vault_id)
        transaction = self._session.get_transaction()
        if transaction is not None and transaction.is_active:
            raise OutboundExecutionGuardError(
                "the staging transaction must commit before an execution ticket is loaded"
            )
        if authorization_port is None or fence_reader is None:
            raise OutboundExecutionGuardError(
                "execution ticket requires authorization and authoritative fence ports"
            )
        binding_result = await self._session.execute(
            select(
                OutboundOperation.authorization_id,
                OutboundOperation.authorization_generation,
                OutboundOperation.connector,
                OutboundOperation.operation,
                OutboundOperation.resource_id,
                OutboundOperation.local_resource_version,
                OutboundOperation.provider_idempotency_key,
                OutboundOperation.request_hash,
                OutboundOperation.policy_epoch,
                OutboundOperation.source_generation,
                OutboundOperation.initial_tombstoned,
                OutboundOperation.max_attempts,
                OutboundOperation.max_reconciliation_attempts,
                OutboundOperation.execution_generation,
            ).where(
                OutboundOperation.id == staged.operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.EXECUTING,
                OutboundOperation.execution_generation == staged.execution_generation,
                OutboundOperation.execution_expires_at > func.clock_timestamp(),
            )
        )
        row = binding_result.mappings().one_or_none()
        if row is None:
            return None
        expected_fence = FenceSnapshot(
            policy_epoch=row["policy_epoch"],
            source_generation=row["source_generation"],
            tombstoned=row["initial_tombstoned"],
        )
        if expected_fence.tombstoned:
            raise OutboundExecutionGuardError("outbound operation has an invalid initial fence")
        binding = ExactOutboundAuthorization(
            outbound_operation_id=staged.operation_id,
            vault_id=self.vault_id,
            authorization_id=row["authorization_id"],
            authorization_generation=row["authorization_generation"],
            connector=row["connector"],
            operation=row["operation"],
            resource_id=row["resource_id"],
            local_resource_version=row["local_resource_version"],
            provider_idempotency_key=row["provider_idempotency_key"],
            request_hash=row["request_hash"],
            initial_fence=expected_fence,
            max_attempts=row["max_attempts"],
            max_reconciliation_attempts=row["max_reconciliation_attempts"],
        )
        async with self._session.begin_nested():
            current_fence = await fence_reader.read_for_update(
                self._session,
                vault_id=self.vault_id,
                resource_id=binding.resource_id,
            )
            try:
                check_execution_fence(
                    expected_fence,
                    current_fence,
                    FenceCheckpoint.BEFORE_EXTERNAL_CALL,
                )
            except FenceViolation:
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == staged.operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.EXECUTING,
                        OutboundOperation.execution_generation == staged.execution_generation,
                        OutboundOperation.execution_expires_at > func.clock_timestamp(),
                        _outbound_binding_match(binding),
                    )
                    .values(
                        state=OutboundOperationState.CANCELED,
                        execution_started_at=None,
                        execution_expires_at=None,
                        completed_at=func.clock_timestamp(),
                        last_error_class="privacy_fence_rejected",
                        safe_error_message="authorization or source state changed",
                        updated_at=func.clock_timestamp(),
                    )
                )
                return None
            decision = await authorization_port.consume_exact(self._session, binding=binding)
            if decision is not OutboundAuthorizationDecision.ACCEPTED:
                target = (
                    OutboundOperationState.CANCELED
                    if decision is OutboundAuthorizationDecision.REVOKED
                    else OutboundOperationState.MANUAL_REVIEW
                )
                values: dict[str, object] = {
                    "state": target,
                    "execution_started_at": None,
                    "execution_expires_at": None,
                    "last_error_class": f"authorization_{decision.value}",
                    "safe_error_message": "manual review is required",
                    "updated_at": func.clock_timestamp(),
                }
                if target is OutboundOperationState.CANCELED:
                    values["completed_at"] = func.clock_timestamp()
                    values["safe_error_message"] = "authorization was revoked"
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == staged.operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.EXECUTING,
                        OutboundOperation.execution_generation == staged.execution_generation,
                        OutboundOperation.execution_expires_at > func.clock_timestamp(),
                        _outbound_binding_match(binding),
                    )
                    .values(**values)
                )
                return None
            ticket_result = await self._session.execute(
                select(OutboundOperation.execution_generation)
                .where(
                    OutboundOperation.id == staged.operation_id,
                    OutboundOperation.vault_id == self.vault_id,
                    OutboundOperation.state == OutboundOperationState.EXECUTING,
                    OutboundOperation.execution_generation == staged.execution_generation,
                    OutboundOperation.execution_expires_at > func.clock_timestamp(),
                    _outbound_binding_match(binding),
                )
                .with_for_update()
            )
            generation = ticket_result.scalar_one_or_none()
            if generation is None:
                raise OutboundExecutionGuardError(
                    "outbound execution changed before the committed ticket was loaded"
                )
            ticket_transaction = self._session.get_transaction()
            if ticket_transaction is None or not ticket_transaction.is_active:
                raise OutboundExecutionGuardError("execution ticket requires an active transaction")
            return OutboundExecutionTicket(
                operation_id=staged.operation_id,
                vault_id=self.vault_id,
                execution_generation=staged.execution_generation,
                _transaction=ticket_transaction,
            )

    async def recover_expired_executions(self) -> tuple[uuid.UUID, ...]:
        result = await self._session.execute(
            expire_uncertain_outbound_executions_statement(),
            {"vault_id": self.vault_id},
        )
        return tuple(result.scalars())

    async def recover_stale_reconciliations(self) -> tuple[uuid.UUID, ...]:
        result = await self._session.execute(
            expire_stale_reconciliations_statement(),
            {"vault_id": self.vault_id},
        )
        return tuple(result.scalars())

    async def mark_unknown(self, ticket: OutboundExecutionTicket) -> bool:
        self._require_execution_ticket(ticket)
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == ticket.operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.EXECUTING,
                OutboundOperation.execution_generation == ticket.execution_generation,
                OutboundOperation.execution_expires_at > func.clock_timestamp(),
            )
            .values(
                state=OutboundOperationState.UNKNOWN,
                execution_started_at=None,
                execution_expires_at=None,
                last_error_class=FailureClass.EXTERNAL_OUTCOME_UNKNOWN.value,
                safe_error_message=_SAFE_FAILURE_MESSAGES[FailureClass.EXTERNAL_OUTCOME_UNKNOWN],
                updated_at=func.clock_timestamp(),
            )
            .returning(OutboundOperation.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def begin_reconciliation(
        self,
        operation_id: uuid.UUID,
        *,
        authorization_port: ExactOutboundAuthorizationPort | None,
        fence_reader: AuthoritativeFenceReader | None,
        reconciliation_for: timedelta = timedelta(minutes=2),
    ) -> OutboundReconciliationTicket | None:
        """Authorize and lease one provider lookup in the current transaction."""

        if authorization_port is None or fence_reader is None:
            raise OutboundExecutionGuardError(
                "reconciliation requires authorization and authoritative fence ports"
            )
        if reconciliation_for <= timedelta(0):
            raise ValueError("reconciliation duration must be positive")

        binding_result = await self._session.execute(
            select(
                OutboundOperation.authorization_id,
                OutboundOperation.authorization_generation,
                OutboundOperation.connector,
                OutboundOperation.operation,
                OutboundOperation.resource_id,
                OutboundOperation.local_resource_version,
                OutboundOperation.provider_idempotency_key,
                OutboundOperation.request_hash,
                OutboundOperation.policy_epoch,
                OutboundOperation.source_generation,
                OutboundOperation.initial_tombstoned,
                OutboundOperation.attempts,
                OutboundOperation.max_attempts,
                OutboundOperation.reconciliation_generation,
                OutboundOperation.max_reconciliation_attempts,
            ).where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.UNKNOWN,
            )
        )
        row = binding_result.mappings().one_or_none()
        if row is None:
            return None
        expected_fence = FenceSnapshot(
            policy_epoch=row["policy_epoch"],
            source_generation=row["source_generation"],
            tombstoned=row["initial_tombstoned"],
        )
        if expected_fence.tombstoned:
            raise OutboundExecutionGuardError("outbound operation has an invalid initial fence")
        binding = ExactOutboundAuthorization(
            outbound_operation_id=operation_id,
            vault_id=self.vault_id,
            authorization_id=row["authorization_id"],
            authorization_generation=row["authorization_generation"],
            connector=row["connector"],
            operation=row["operation"],
            resource_id=row["resource_id"],
            local_resource_version=row["local_resource_version"],
            provider_idempotency_key=row["provider_idempotency_key"],
            request_hash=row["request_hash"],
            initial_fence=expected_fence,
            max_attempts=row["max_attempts"],
            max_reconciliation_attempts=row["max_reconciliation_attempts"],
        )
        current_generation = int(row["reconciliation_generation"])
        if current_generation >= binding.max_reconciliation_attempts:
            await self._session.execute(
                update(OutboundOperation)
                .where(
                    OutboundOperation.id == operation_id,
                    OutboundOperation.vault_id == self.vault_id,
                    OutboundOperation.state == OutboundOperationState.UNKNOWN,
                    OutboundOperation.reconciliation_generation == current_generation,
                    _outbound_binding_match(binding),
                )
                .values(
                    state=OutboundOperationState.MANUAL_REVIEW,
                    last_error_class="reconciliation_attempts_exhausted",
                    safe_error_message="manual review is required",
                    updated_at=func.clock_timestamp(),
                )
            )
            return None

        async with self._session.begin_nested():
            current_fence = await fence_reader.read_for_update(
                self._session,
                vault_id=self.vault_id,
                resource_id=binding.resource_id,
            )
            try:
                check_execution_fence(
                    expected_fence,
                    current_fence,
                    FenceCheckpoint.BEFORE_EXTERNAL_CALL,
                )
            except FenceViolation:
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.UNKNOWN,
                        OutboundOperation.reconciliation_generation == current_generation,
                        _outbound_binding_match(binding),
                    )
                    .values(
                        state=OutboundOperationState.CANCELED,
                        completed_at=func.clock_timestamp(),
                        last_error_class="privacy_fence_rejected",
                        safe_error_message="authorization or source state changed",
                        updated_at=func.clock_timestamp(),
                    )
                )
                return None
            decision = await authorization_port.consume_exact(self._session, binding=binding)
            if decision is not OutboundAuthorizationDecision.ACCEPTED:
                target = (
                    OutboundOperationState.CANCELED
                    if decision is OutboundAuthorizationDecision.REVOKED
                    else OutboundOperationState.MANUAL_REVIEW
                )
                values: dict[str, object] = {
                    "state": target,
                    "last_error_class": f"authorization_{decision.value}",
                    "safe_error_message": "manual review is required",
                    "updated_at": func.clock_timestamp(),
                }
                if target is OutboundOperationState.CANCELED:
                    values["completed_at"] = func.clock_timestamp()
                    values["safe_error_message"] = "authorization was revoked"
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.UNKNOWN,
                        OutboundOperation.reconciliation_generation == current_generation,
                        _outbound_binding_match(binding),
                    )
                    .values(**values)
                )
                return None
            result = await self._session.execute(
                update(OutboundOperation)
                .where(
                    OutboundOperation.id == operation_id,
                    OutboundOperation.vault_id == self.vault_id,
                    OutboundOperation.state == OutboundOperationState.UNKNOWN,
                    OutboundOperation.reconciliation_generation == current_generation,
                    OutboundOperation.reconciliation_generation
                    < OutboundOperation.max_reconciliation_attempts,
                    _outbound_binding_match(binding),
                )
                .values(
                    state=OutboundOperationState.RECONCILING,
                    reconciliation_generation=OutboundOperation.reconciliation_generation + 1,
                    reconciliation_started_at=func.clock_timestamp(),
                    reconciliation_expires_at=func.clock_timestamp()
                    + bindparam("reconciliation_for", type_=Interval()),
                    updated_at=func.clock_timestamp(),
                )
                .returning(OutboundOperation.reconciliation_generation)
            )
            generation = result.scalar_one_or_none()
            if generation is None:
                raise OutboundExecutionGuardError(
                    "outbound operation changed before reconciliation was staged"
                )
            transaction = self._session.get_transaction()
            if transaction is None or not transaction.is_active:
                raise OutboundExecutionGuardError(
                    "reconciliation ticket requires an active transaction"
                )
            return OutboundReconciliationTicket(
                operation_id=operation_id,
                vault_id=self.vault_id,
                reconciliation_generation=int(generation),
                _transaction=transaction,
            )

    async def resolve_reconciliation(
        self,
        ticket: OutboundReconciliationTicket,
        *,
        outcome: ReconciliationOutcome,
        external_id: str | None = None,
        authorization_port: ExactOutboundAuthorizationPort | None = None,
        fence_reader: AuthoritativeFenceReader | None = None,
    ) -> OutboundOperationState | None:
        self._require_reconciliation_ticket(ticket)
        target = reconciliation_target(outcome)
        if target is OutboundOperationState.SUCCEEDED and not external_id:
            raise ValueError("a reconciled external identifier is required")
        if external_id is not None:
            validate_provider_identifier(external_id)
        if target is OutboundOperationState.PENDING:
            return await self._resume_after_confirmed_absence(
                ticket,
                authorization_port=authorization_port,
                fence_reader=fence_reader,
            )
        values: dict[str, object] = {
            "state": target,
            "external_id": external_id if target is OutboundOperationState.SUCCEEDED else None,
            "reconciled_at": func.clock_timestamp(),
            "reconciliation_started_at": None,
            "reconciliation_expires_at": None,
            "updated_at": func.clock_timestamp(),
        }
        if target in {OutboundOperationState.SUCCEEDED, OutboundOperationState.FAILED}:
            values["completed_at"] = func.clock_timestamp()
        if target in {OutboundOperationState.SUCCEEDED, OutboundOperationState.PENDING}:
            values["last_error_class"] = None
            values["safe_error_message"] = None
        if target is OutboundOperationState.FAILED:
            values["last_error_class"] = "reconciliation_failed"
            values["safe_error_message"] = "external reconciliation failed"
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == ticket.operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.RECONCILING,
                OutboundOperation.reconciliation_generation == ticket.reconciliation_generation,
                OutboundOperation.reconciliation_expires_at > func.clock_timestamp(),
            )
            .values(**values)
            .returning(OutboundOperation.id)
        )
        result = await self._session.execute(statement)
        if result.scalar_one_or_none() is None:
            return None
        return target

    async def _resume_after_confirmed_absence(
        self,
        ticket: OutboundReconciliationTicket,
        *,
        authorization_port: ExactOutboundAuthorizationPort | None,
        fence_reader: AuthoritativeFenceReader | None,
    ) -> OutboundOperationState | None:
        """Reauthorize a finite retry; exhausted operations require manual review."""

        binding_result = await self._session.execute(
            select(
                OutboundOperation.authorization_id,
                OutboundOperation.authorization_generation,
                OutboundOperation.connector,
                OutboundOperation.operation,
                OutboundOperation.resource_id,
                OutboundOperation.local_resource_version,
                OutboundOperation.provider_idempotency_key,
                OutboundOperation.request_hash,
                OutboundOperation.policy_epoch,
                OutboundOperation.source_generation,
                OutboundOperation.initial_tombstoned,
                OutboundOperation.attempts,
                OutboundOperation.max_attempts,
                OutboundOperation.max_reconciliation_attempts,
            ).where(
                OutboundOperation.id == ticket.operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.RECONCILING,
                OutboundOperation.reconciliation_generation == ticket.reconciliation_generation,
                OutboundOperation.reconciliation_expires_at > func.clock_timestamp(),
            )
        )
        row = binding_result.mappings().one_or_none()
        if row is None:
            return None
        if row["attempts"] >= row["max_attempts"]:
            result = await self._session.execute(
                update(OutboundOperation)
                .where(
                    OutboundOperation.id == ticket.operation_id,
                    OutboundOperation.vault_id == self.vault_id,
                    OutboundOperation.state == OutboundOperationState.RECONCILING,
                    OutboundOperation.reconciliation_generation == ticket.reconciliation_generation,
                    OutboundOperation.reconciliation_expires_at > func.clock_timestamp(),
                    OutboundOperation.attempts >= OutboundOperation.max_attempts,
                )
                .values(
                    state=OutboundOperationState.MANUAL_REVIEW,
                    reconciliation_started_at=None,
                    reconciliation_expires_at=None,
                    reconciled_at=func.clock_timestamp(),
                    last_error_class="outbound_attempts_exhausted",
                    safe_error_message="manual review is required",
                    updated_at=func.clock_timestamp(),
                )
                .returning(OutboundOperation.id)
            )
            if result.scalar_one_or_none() is None:
                return None
            return OutboundOperationState.MANUAL_REVIEW
        if authorization_port is None or fence_reader is None:
            raise OutboundExecutionGuardError(
                "outbound retry requires authorization and authoritative fence ports"
            )

        expected_fence = FenceSnapshot(
            policy_epoch=row["policy_epoch"],
            source_generation=row["source_generation"],
            tombstoned=row["initial_tombstoned"],
        )
        if expected_fence.tombstoned:
            raise OutboundExecutionGuardError("outbound operation has an invalid initial fence")
        binding = ExactOutboundAuthorization(
            outbound_operation_id=ticket.operation_id,
            vault_id=self.vault_id,
            authorization_id=row["authorization_id"],
            authorization_generation=row["authorization_generation"],
            connector=row["connector"],
            operation=row["operation"],
            resource_id=row["resource_id"],
            local_resource_version=row["local_resource_version"],
            provider_idempotency_key=row["provider_idempotency_key"],
            request_hash=row["request_hash"],
            initial_fence=expected_fence,
            max_attempts=row["max_attempts"],
            max_reconciliation_attempts=row["max_reconciliation_attempts"],
        )
        async with self._session.begin_nested():
            current_fence = await fence_reader.read_for_update(
                self._session,
                vault_id=self.vault_id,
                resource_id=binding.resource_id,
            )
            try:
                check_execution_fence(
                    expected_fence,
                    current_fence,
                    FenceCheckpoint.BEFORE_EXTERNAL_CALL,
                )
            except FenceViolation:
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == ticket.operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.RECONCILING,
                        OutboundOperation.reconciliation_generation
                        == ticket.reconciliation_generation,
                        _outbound_binding_match(binding),
                        OutboundOperation.reconciliation_expires_at > func.clock_timestamp(),
                    )
                    .values(
                        state=OutboundOperationState.CANCELED,
                        reconciliation_started_at=None,
                        reconciliation_expires_at=None,
                        completed_at=func.clock_timestamp(),
                        last_error_class="privacy_fence_rejected",
                        safe_error_message="authorization or source state changed",
                        updated_at=func.clock_timestamp(),
                    )
                )
                return OutboundOperationState.CANCELED
            decision = await authorization_port.consume_exact(self._session, binding=binding)
            if decision is not OutboundAuthorizationDecision.ACCEPTED:
                target = (
                    OutboundOperationState.CANCELED
                    if decision is OutboundAuthorizationDecision.REVOKED
                    else OutboundOperationState.MANUAL_REVIEW
                )
                values: dict[str, object] = {
                    "state": target,
                    "reconciliation_started_at": None,
                    "reconciliation_expires_at": None,
                    "last_error_class": f"authorization_{decision.value}",
                    "safe_error_message": "manual review is required",
                    "updated_at": func.clock_timestamp(),
                }
                if target is OutboundOperationState.CANCELED:
                    values["completed_at"] = func.clock_timestamp()
                    values["safe_error_message"] = "authorization was revoked"
                await self._session.execute(
                    update(OutboundOperation)
                    .where(
                        OutboundOperation.id == ticket.operation_id,
                        OutboundOperation.vault_id == self.vault_id,
                        OutboundOperation.state == OutboundOperationState.RECONCILING,
                        OutboundOperation.reconciliation_generation
                        == ticket.reconciliation_generation,
                        _outbound_binding_match(binding),
                        OutboundOperation.reconciliation_expires_at > func.clock_timestamp(),
                    )
                    .values(**values)
                )
                return target
            result = await self._session.execute(
                update(OutboundOperation)
                .where(
                    OutboundOperation.id == ticket.operation_id,
                    OutboundOperation.vault_id == self.vault_id,
                    OutboundOperation.state == OutboundOperationState.RECONCILING,
                    OutboundOperation.reconciliation_generation == ticket.reconciliation_generation,
                    OutboundOperation.reconciliation_expires_at > func.clock_timestamp(),
                    OutboundOperation.attempts < OutboundOperation.max_attempts,
                    OutboundOperation.max_attempts == binding.max_attempts,
                    OutboundOperation.max_reconciliation_attempts
                    == binding.max_reconciliation_attempts,
                    OutboundOperation.authorization_id == binding.authorization_id,
                    OutboundOperation.authorization_generation == binding.authorization_generation,
                    OutboundOperation.resource_id == binding.resource_id,
                    OutboundOperation.connector == binding.connector,
                    OutboundOperation.operation == binding.operation,
                    OutboundOperation.local_resource_version == binding.local_resource_version,
                    OutboundOperation.provider_idempotency_key == binding.provider_idempotency_key,
                    OutboundOperation.request_hash == binding.request_hash,
                    OutboundOperation.policy_epoch == expected_fence.policy_epoch,
                    OutboundOperation.source_generation == expected_fence.source_generation,
                    OutboundOperation.initial_tombstoned == expected_fence.tombstoned,
                )
                .values(
                    state=OutboundOperationState.PENDING,
                    reconciliation_started_at=None,
                    reconciliation_expires_at=None,
                    reconciled_at=func.clock_timestamp(),
                    last_error_class=None,
                    safe_error_message=None,
                    updated_at=func.clock_timestamp(),
                )
                .returning(OutboundOperation.id)
            )
            if result.scalar_one_or_none() is None:
                raise OutboundExecutionGuardError(
                    "outbound operation changed before retry authorization completed"
                )
            return OutboundOperationState.PENDING

    async def resolve_manual_review(
        self,
        operation_id: uuid.UUID,
        *,
        target_state: OutboundOperationState,
        external_id: str | None = None,
    ) -> OutboundOperationState | None:
        """Record a human-reviewed terminal outcome without creating another side effect."""

        if target_state not in {
            OutboundOperationState.SUCCEEDED,
            OutboundOperationState.FAILED,
            OutboundOperationState.CANCELED,
        }:
            raise ValueError("manual review may only record a terminal outcome")
        if target_state is OutboundOperationState.SUCCEEDED:
            if not external_id:
                raise ValueError("a reviewed external identifier is required")
            validate_provider_identifier(external_id)
        values: dict[str, object] = {
            "state": target_state,
            "external_id": external_id
            if target_state is OutboundOperationState.SUCCEEDED
            else None,
            "completed_at": func.clock_timestamp(),
            "updated_at": func.clock_timestamp(),
            "last_error_class": None,
            "safe_error_message": None,
        }
        if target_state is OutboundOperationState.FAILED:
            values["last_error_class"] = "manual_review_failed"
            values["safe_error_message"] = "manual review rejected the external operation"
        result = await self._session.execute(
            update(OutboundOperation)
            .where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.MANUAL_REVIEW,
            )
            .values(**values)
            .returning(OutboundOperation.id)
        )
        if result.scalar_one_or_none() is None:
            return None
        return target_state

    async def mark_succeeded(
        self,
        ticket: OutboundExecutionTicket,
        *,
        external_id: str,
    ) -> bool:
        self._require_execution_ticket(ticket)
        if not external_id:
            raise ValueError("external identifier is required")
        validate_provider_identifier(external_id)
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == ticket.operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.EXECUTING,
                OutboundOperation.execution_generation == ticket.execution_generation,
                OutboundOperation.execution_expires_at > func.clock_timestamp(),
            )
            .values(
                state=OutboundOperationState.SUCCEEDED,
                external_id=external_id,
                execution_started_at=None,
                execution_expires_at=None,
                completed_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
                last_error_class=None,
                safe_error_message=None,
            )
            .returning(OutboundOperation.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None
