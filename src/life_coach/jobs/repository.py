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
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from life_coach.jobs.contracts import (
    AuthoritativeFenceReader,
    CrossVaultAccessError,
    DispatchLease,
    FenceSnapshot,
    FenceViolation,
    IdempotencyConflict,
    IdempotentWrite,
    JobExecutionContext,
    JobSpec,
    OutboundOperationSpec,
    OutboxEventSpec,
    PersistResult,
    ProcessorScopeError,
    assert_same_request,
)
from life_coach.jobs.enums import (
    CompletionStatus,
    FailureClass,
    FenceCheckpoint,
    JobQueue,
    JobState,
    OutboundOperationState,
    ReconciliationOutcome,
)
from life_coach.jobs.models import Job, OutboundOperation, OutboxEvent
from life_coach.jobs.payloads import SafePayload, validate_safe_payload
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
                    Job.run_after <= func.now(),
                ),
                and_(
                    Job.state == JobState.RUNNING,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= func.now(),
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
            lease_expires_at=func.now() + bindparam("lease_for", type_=Interval()),
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
            Job.lease_expires_at <= func.now(),
            Job.attempts >= Job.max_attempts,
        )
        .values(
            state=JobState.DEAD,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.now(),
            last_error_class="attempts_exhausted",
            safe_error_message="maximum processing attempts reached",
        )
        .returning(Job.id)
    )


def heartbeat_job_statement() -> Update:
    return (
        update(Job)
        .where(
            Job.id == bindparam("job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("claimed_by", type_=String()),
            Job.lease_generation == bindparam("lease_generation"),
            Job.lease_expires_at > func.now(),
        )
        .values(lease_expires_at=func.now() + bindparam("lease_for", type_=Interval()))
        .returning(Job.id)
    )


def complete_job_statement() -> Update:
    """Condition completion on lease plus the caller's locked authoritative fence snapshot."""

    return (
        update(Job)
        .where(
            Job.id == bindparam("job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("claimed_by", type_=String()),
            Job.lease_generation == bindparam("lease_generation"),
            Job.lease_expires_at > func.now(),
            Job.policy_epoch == bindparam("policy_epoch"),
            Job.source_generation == bindparam("source_generation"),
            bindparam("tombstone_clear", type_=Boolean()) == true(),
        )
        .values(
            state=JobState.DONE,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.now(),
            last_error_class=None,
            safe_error_message=None,
        )
        .returning(Job.id)
    )


def cancel_claim_statement() -> Update:
    return (
        update(Job)
        .where(
            Job.id == bindparam("job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("claimed_by", type_=String()),
            Job.lease_generation == bindparam("lease_generation"),
            Job.lease_expires_at > func.now(),
        )
        .values(
            state=JobState.CANCELED,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.now(),
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
        values["run_after"] = func.now() + bindparam("retry_delay", type_=Interval())
    if target_state in {JobState.DEAD, JobState.CANCELED}:
        values["completed_at"] = func.now()
    return (
        update(Job)
        .where(
            Job.id == bindparam("job_id", type_=Uuid(as_uuid=True)),
            Job.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            Job.state == JobState.RUNNING,
            Job.lease_owner == bindparam("claimed_by", type_=String()),
            Job.lease_generation == bindparam("lease_generation"),
            Job.lease_expires_at > func.now(),
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
            "run_after": func.now(),
            "completed_at": case((retryable, None), else_=func.now()),
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
            "completed_at": func.now(),
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
    """Only pending operations are executable; unknown is intentionally excluded."""

    return (
        update(OutboundOperation)
        .where(
            OutboundOperation.id == bindparam("operation_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.vault_id == bindparam("vault_id", type_=Uuid(as_uuid=True)),
            OutboundOperation.state == OutboundOperationState.PENDING,
        )
        .values(
            state=OutboundOperationState.EXECUTING,
            attempts=OutboundOperation.attempts + 1,
            execution_generation=OutboundOperation.execution_generation + 1,
            execution_started_at=func.now(),
            execution_expires_at=func.now() + bindparam("execution_for", type_=Interval()),
            updated_at=func.now(),
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
            OutboundOperation.execution_expires_at <= func.now(),
        )
        .values(
            state=OutboundOperationState.UNKNOWN,
            execution_started_at=None,
            execution_expires_at=None,
            last_error_class=FailureClass.EXTERNAL_OUTCOME_UNKNOWN.value,
            safe_error_message=_SAFE_FAILURE_MESSAGES[FailureClass.EXTERNAL_OUTCOME_UNKNOWN],
            updated_at=func.now(),
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
            OutboundOperation.reconciliation_expires_at <= func.now(),
        )
        .values(
            state=OutboundOperationState.UNKNOWN,
            reconciliation_started_at=None,
            reconciliation_expires_at=None,
            updated_at=func.now(),
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
        "run_after": spec.run_after if spec.run_after is not None else func.now(),
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

    async def load_execution_context(
        self, lease: DispatchLease, *, lease_owner: str
    ) -> JobExecutionContext | None:
        self._require_claim(lease)
        statement = select(
            Job.job_type,
            Job.queue,
            Job.resource_id,
            Job.resource_revision_id,
            Job.pipeline_version,
            Job.consent_snapshot_id,
            Job.policy_epoch,
            Job.source_generation,
            Job.payload,
            Job.attempts,
            Job.max_attempts,
        ).where(
            Job.id == lease.job_id,
            Job.vault_id == self.vault_id,
            Job.state == JobState.RUNNING,
            Job.lease_owner == lease_owner,
            Job.lease_generation == lease.lease_generation,
            Job.lease_expires_at > func.now(),
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
            consent_snapshot_id=row["consent_snapshot_id"],
            expected_fence=FenceSnapshot(
                policy_epoch=row["policy_epoch"],
                source_generation=row["source_generation"],
            ),
            payload=validate_safe_payload(row["payload"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
        )

    def gate(
        self,
        context: JobExecutionContext,
        current: FenceSnapshot,
        checkpoint: FenceCheckpoint,
    ) -> None:
        self._require_claim(context.lease)
        check_execution_fence(context.expected_fence, current, checkpoint)

    async def gate_authoritatively(
        self,
        context: JobExecutionContext,
        checkpoint: FenceCheckpoint,
        fence_reader: AuthoritativeFenceReader,
    ) -> FenceSnapshot:
        """Re-read authority immediately before source access or every external call."""

        self._require_claim(context.lease)
        current = await fence_reader.read_current(
            self._session,
            vault_id=self.vault_id,
            resource_id=context.resource_id,
        )
        check_execution_fence(context.expected_fence, current, checkpoint)
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
        result = await self._session.execute(
            heartbeat_job_statement(),
            {
                "job_id": lease.job_id,
                "vault_id": lease.vault_id,
                "lease_generation": lease.lease_generation,
                "claimed_by": lease_owner,
                "lease_for": lease_for,
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
        current_fence = await fence_reader.read_for_update(
            self._session,
            vault_id=self.vault_id,
            resource_id=context.resource_id,
        )
        try:
            check_execution_fence(
                context.expected_fence,
                current_fence,
                FenceCheckpoint.BEFORE_RESULT_COMMIT,
            )
        except FenceViolation:
            canceled = await self._session.execute(
                cancel_claim_statement(),
                {
                    "job_id": context.lease.job_id,
                    "vault_id": context.lease.vault_id,
                    "lease_generation": context.lease.lease_generation,
                    "claimed_by": lease_owner,
                },
            )
            if canceled.scalar_one_or_none() is None:
                return CompletionStatus.LEASE_LOST
            return CompletionStatus.DISCARDED

        completed = await self._session.execute(
            complete_job_statement(),
            {
                "job_id": context.lease.job_id,
                "vault_id": context.lease.vault_id,
                "lease_generation": context.lease.lease_generation,
                "claimed_by": lease_owner,
                "policy_epoch": current_fence.policy_epoch,
                "source_generation": current_fence.source_generation,
                "tombstone_clear": not current_fence.tombstoned,
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
        decision = retry_decision(
            failure,
            attempts=context.attempts,
            max_attempts=context.max_attempts,
            random_sample=lambda: random_sample,
        )
        statement = failure_job_statement(decision.target_state)
        params: dict[str, object] = {
            "job_id": context.lease.job_id,
            "vault_id": context.lease.vault_id,
            "lease_generation": context.lease.lease_generation,
            "claimed_by": lease_owner,
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

    async def create(self, spec: OutboundOperationSpec) -> IdempotentWrite:
        self._require_vault(spec.vault_id)
        insert = (
            pg_insert(OutboundOperation)
            .values(
                vault_id=spec.vault_id,
                connector=spec.connector,
                operation=spec.operation,
                local_resource_version=spec.local_resource_version,
                request_hash=spec.request_hash,
                provider_idempotency_key=spec.provider_idempotency_key,
                state=OutboundOperationState.PENDING,
            )
            .on_conflict_do_nothing()
            .returning(OutboundOperation.id)
        )
        inserted = (await self._session.execute(insert)).scalar_one_or_none()
        if inserted is not None:
            return IdempotentWrite(inserted, created=True)
        existing_result = await self._session.execute(
            select(OutboundOperation.id, OutboundOperation.request_hash).where(
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.connector == spec.connector,
                OutboundOperation.operation == spec.operation,
                OutboundOperation.local_resource_version == spec.local_resource_version,
            )
        )
        existing = existing_result.mappings().one_or_none()
        if existing is not None:
            assert_same_request(existing["request_hash"], spec.request_hash)
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
        execution_for: timedelta = timedelta(minutes=2),
    ) -> int | None:
        if execution_for <= timedelta(0):
            raise ValueError("outbound execution duration must be positive")
        result = await self._session.execute(
            begin_outbound_execution_statement(),
            {
                "operation_id": operation_id,
                "vault_id": self.vault_id,
                "execution_for": execution_for,
            },
        )
        return result.scalar_one_or_none()

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

    async def mark_unknown(self, operation_id: uuid.UUID, *, execution_generation: int) -> bool:
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.EXECUTING,
                OutboundOperation.execution_generation == execution_generation,
                OutboundOperation.execution_expires_at > func.now(),
            )
            .values(
                state=OutboundOperationState.UNKNOWN,
                execution_started_at=None,
                execution_expires_at=None,
                last_error_class=FailureClass.EXTERNAL_OUTCOME_UNKNOWN.value,
                safe_error_message=_SAFE_FAILURE_MESSAGES[FailureClass.EXTERNAL_OUTCOME_UNKNOWN],
                updated_at=func.now(),
            )
            .returning(OutboundOperation.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def begin_reconciliation(
        self,
        operation_id: uuid.UUID,
        *,
        reconciliation_for: timedelta = timedelta(minutes=2),
    ) -> int | None:
        if reconciliation_for <= timedelta(0):
            raise ValueError("reconciliation duration must be positive")
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.UNKNOWN,
            )
            .values(
                state=OutboundOperationState.RECONCILING,
                reconciliation_generation=OutboundOperation.reconciliation_generation + 1,
                reconciliation_started_at=func.now(),
                reconciliation_expires_at=func.now()
                + bindparam("reconciliation_for", type_=Interval()),
                updated_at=func.now(),
            )
            .returning(OutboundOperation.reconciliation_generation)
        )
        result = await self._session.execute(statement, {"reconciliation_for": reconciliation_for})
        return result.scalar_one_or_none()

    async def resolve_reconciliation(
        self,
        operation_id: uuid.UUID,
        *,
        reconciliation_generation: int,
        outcome: ReconciliationOutcome,
        external_id: str | None = None,
    ) -> OutboundOperationState | None:
        target = reconciliation_target(outcome)
        if target is OutboundOperationState.SUCCEEDED and not external_id:
            raise ValueError("a reconciled external identifier is required")
        values: dict[str, object] = {
            "state": target,
            "external_id": external_id if target is OutboundOperationState.SUCCEEDED else None,
            "reconciled_at": func.now(),
            "reconciliation_started_at": None,
            "reconciliation_expires_at": None,
            "updated_at": func.now(),
        }
        if target in {OutboundOperationState.SUCCEEDED, OutboundOperationState.FAILED}:
            values["completed_at"] = func.now()
        if target in {OutboundOperationState.SUCCEEDED, OutboundOperationState.PENDING}:
            values["last_error_class"] = None
            values["safe_error_message"] = None
        if target is OutboundOperationState.FAILED:
            values["last_error_class"] = "reconciliation_failed"
            values["safe_error_message"] = "external reconciliation failed"
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.RECONCILING,
                OutboundOperation.reconciliation_generation == reconciliation_generation,
                OutboundOperation.reconciliation_expires_at > func.now(),
            )
            .values(**values)
            .returning(OutboundOperation.id)
        )
        result = await self._session.execute(statement)
        if result.scalar_one_or_none() is None:
            return None
        return target

    async def mark_succeeded(
        self,
        operation_id: uuid.UUID,
        *,
        execution_generation: int,
        external_id: str,
    ) -> bool:
        if not external_id:
            raise ValueError("external identifier is required")
        statement = (
            update(OutboundOperation)
            .where(
                OutboundOperation.id == operation_id,
                OutboundOperation.vault_id == self.vault_id,
                OutboundOperation.state == OutboundOperationState.EXECUTING,
                OutboundOperation.execution_generation == execution_generation,
                OutboundOperation.execution_expires_at > func.now(),
            )
            .values(
                state=OutboundOperationState.SUCCEEDED,
                external_id=external_id,
                execution_started_at=None,
                execution_expires_at=None,
                completed_at=func.now(),
                updated_at=func.now(),
                last_error_class=None,
                safe_error_message=None,
            )
            .returning(OutboundOperation.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None
