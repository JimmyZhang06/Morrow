"""Durable, user-visible background execution for candidate insights."""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from life_coach.ai.provider import (
    GatewayConfigurationError,
    ModelPolicyViolation,
    ProviderOutputDecodeError,
    StructuredOutputValidationError,
    ToolDirectiveRejected,
)
from life_coach.application.candidate_insight import CANDIDATE_INSIGHT_TASK_TYPE
from life_coach.application.candidate_insight_entry import (
    EntryCandidateRevisionConflict,
    EntryCandidateSourceUnavailable,
    GenerateCandidateInsightForEntry,
)
from life_coach.application.model_gateway import ModelInvocationDenied
from life_coach.application.model_runtime import (
    AuthorizedVaultSessionOpener,
    ModelResultRejected,
    ModelRunFinalizationConflict,
    ModelRunProviderOutcomeUnknown,
    ModelRunProviderUnavailable,
    ModelRunReplayInProgress,
    ModelRunReplayTerminal,
    ModelRunResultDiscarded,
    ModelRunResultPersistenceError,
)
from life_coach.jobs.contracts import JobExecutionContext, JobSpec
from life_coach.jobs.enums import FailureClass, JobQueue, JobState
from life_coach.jobs.models import Job
from life_coach.jobs.payloads import JsonValue, canonical_request_hash
from life_coach.jobs.repository import (
    GlobalDispatcherRepository,
    VaultJobRepository,
    VaultProcessorRepository,
)
from life_coach.modules.identity.models import Vault
from life_coach.modules.model_runs.contracts import ModelRunArtifactRef
from life_coach.modules.model_runs.models import ModelRun, ModelRunArtifact, ModelRunState
from life_coach.platform.auth import (
    AuthenticatedPrincipal,
    AuthenticationDenied,
    VaultMembershipDenied,
)
from life_coach.platform.database import AsyncSessionFactory, vault_transaction

CANDIDATE_INSIGHT_JOB_TYPE = "candidate_insight.generate"
CANDIDATE_INSIGHT_JOB_PIPELINE = "candidate-insight-job-v1"
_LEASE_FOR = timedelta(seconds=90)
_LOGGER = logging.getLogger(__name__)


class CandidateInsightJobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DENIED = "denied"
    CANCELING = "canceling"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class CandidateInsightJobProjection:
    job_id: uuid.UUID
    status: CandidateInsightJobStatus
    stage: str
    progress: int
    retryable: bool
    run_id: uuid.UUID | None = None
    memory_id: uuid.UUID | None = None
    derived_object_id: uuid.UUID | None = None


class QueuedCandidateInsightRuntime(Protocol):
    async def run_for_principal(
        self,
        *,
        principal: AuthenticatedPrincipal,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: str,
        expected_membership_generation: int | None = None,
    ) -> ModelRunArtifactRef: ...


class CandidateInsightJobService:
    """Authenticate, enqueue, inspect, and cancel one Vault-scoped task."""

    def __init__(
        self,
        *,
        sessions: AuthorizedVaultSessionOpener,
        hmac_key: bytes,
    ) -> None:
        if len(hmac_key) < 32:
            raise ValueError("candidate job fingerprint key must contain at least 32 bytes")
        self._sessions = sessions
        self._hmac_key = bytes(hmac_key)

    async def enqueue(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> CandidateInsightJobProjection:
        principal = await self._sessions.authenticate(authorization)
        async with self._sessions.open_for_principal(
            principal=principal,
            vault_id=vault_id,
        ) as authorized:
            revision_no, _fragment_ids = await authorized.session.run_sync(
                lambda session: GenerateCandidateInsightForEntry._resolve_entry(
                    session,
                    vault_id=vault_id,
                    entry_id=entry_id,
                )
            )
            if revision_no != expected_revision:
                raise EntryCandidateRevisionConflict(current_revision=revision_no)
            fence = (
                await authorized.session.execute(
                    select(Vault.policy_epoch, Vault.source_generation).where(Vault.id == vault_id)
                )
            ).one_or_none()
            if fence is None:
                raise EntryCandidateSourceUnavailable("entry is unavailable")
            policy_epoch, source_generation = fence
            fingerprint = canonical_request_hash(
                cast(
                    JsonValue,
                    {
                        "domain": "candidate_insight.job.v1",
                        "entry_id": str(entry_id),
                        "expected_revision": expected_revision,
                        "principal_id": str(principal.principal_id),
                        "membership_generation": authorized.context.membership_generation,
                        "idempotency_key": str(idempotency_key),
                    },
                ),
                vault_id=vault_id,
                hmac_key=self._hmac_key,
            )
            write = await VaultJobRepository(authorized.session, vault_id).enqueue(
                JobSpec(
                    vault_id=vault_id,
                    job_type=CANDIDATE_INSIGHT_JOB_TYPE,
                    queue=JobQueue.REFLECTION,
                    resource_id=entry_id,
                    pipeline_version=CANDIDATE_INSIGHT_JOB_PIPELINE,
                    idempotency_key=str(idempotency_key),
                    request_hash=fingerprint,
                    max_attempts=1,
                    policy_epoch=policy_epoch,
                    source_generation=source_generation,
                    requested_by_principal_id=principal.principal_id,
                    membership_generation=authorized.context.membership_generation,
                    expected_resource_revision=expected_revision,
                )
            )
            return await self._read_projection(authorized.session, vault_id, write.resource_id)

    async def get(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CandidateInsightJobProjection:
        principal = await self._sessions.authenticate(authorization)
        async with self._sessions.open_for_principal(
            principal=principal,
            vault_id=vault_id,
        ) as authorized:
            return await self._read_projection(authorized.session, vault_id, job_id)

    async def cancel(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CandidateInsightJobProjection:
        principal = await self._sessions.authenticate(authorization)
        async with self._sessions.open_for_principal(
            principal=principal,
            vault_id=vault_id,
        ) as authorized:
            result = await authorized.session.execute(
                select(Job.state, Job.cancel_requested_at)
                .where(
                    Job.id == job_id,
                    Job.vault_id == vault_id,
                    Job.job_type == CANDIDATE_INSIGHT_JOB_TYPE,
                )
                .with_for_update()
            )
            row = result.one_or_none()
            if row is None:
                raise EntryCandidateSourceUnavailable("candidate job is unavailable")
            state = JobState(row.state)
            now = func.clock_timestamp()
            if state in {JobState.QUEUED, JobState.RETRYING, JobState.WAITING}:
                await authorized.session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.vault_id == vault_id)
                    .values(
                        state=JobState.CANCELED,
                        cancel_requested_at=now,
                        completed_at=now,
                        last_error_class="user_canceled",
                        safe_error_message="canceled by user",
                    )
                )
            elif state is JobState.RUNNING and row.cancel_requested_at is None:
                await authorized.session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.vault_id == vault_id)
                    .values(cancel_requested_at=now)
                )
            return await self._read_projection(authorized.session, vault_id, job_id)

    @staticmethod
    async def _read_projection(
        session: AsyncSession,
        vault_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CandidateInsightJobProjection:
        row = (
            (
                await session.execute(
                    select(
                        Job.id.label("job_id"),
                        Job.state.label("job_state"),
                        Job.cancel_requested_at,
                        Job.last_error_class,
                        ModelRun.id.label("run_id"),
                        ModelRun.state.label("run_state"),
                        ModelRunArtifact.memory_claim_id,
                        ModelRunArtifact.derived_object_id,
                    )
                    .outerjoin(
                        ModelRun,
                        and_(
                            ModelRun.vault_id == Job.vault_id,
                            ModelRun.task_type == CANDIDATE_INSIGHT_TASK_TYPE,
                            ModelRun.idempotency_key == Job.idempotency_key,
                        ),
                    )
                    .outerjoin(
                        ModelRunArtifact,
                        and_(
                            ModelRunArtifact.vault_id == ModelRun.vault_id,
                            ModelRunArtifact.model_run_id == ModelRun.id,
                        ),
                    )
                    .where(
                        Job.id == job_id,
                        Job.vault_id == vault_id,
                        Job.job_type == CANDIDATE_INSIGHT_JOB_TYPE,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise EntryCandidateSourceUnavailable("candidate job is unavailable")
        return _project_job(row)


def _project_job(row: object) -> CandidateInsightJobProjection:
    from sqlalchemy.engine import RowMapping

    if not isinstance(row, RowMapping):
        raise TypeError("candidate job projection requires a database row")
    job_state = JobState(row["job_state"])
    run_state = ModelRunState(row["run_state"]) if row["run_state"] is not None else None
    cancel_requested = row["cancel_requested_at"] is not None
    status: CandidateInsightJobStatus
    stage: str
    progress: int
    retryable = False
    if job_state is JobState.CANCELED:
        status, stage, progress = CandidateInsightJobStatus.CANCELED, "canceled", 100
    elif job_state is JobState.DEAD:
        status, stage, progress, retryable = (
            CandidateInsightJobStatus.FAILED,
            "failed",
            100,
            True,
        )
    elif job_state is JobState.WAITING or run_state is ModelRunState.UNKNOWN:
        status, stage, progress, retryable = (
            CandidateInsightJobStatus.UNKNOWN,
            "outcome_unknown",
            100,
            True,
        )
    elif job_state is JobState.DONE or run_state is ModelRunState.SUCCEEDED:
        status, stage, progress = CandidateInsightJobStatus.SUCCEEDED, "ready", 100
    elif run_state is ModelRunState.DENIED:
        status, stage, progress = CandidateInsightJobStatus.DENIED, "denied", 100
    elif run_state in {ModelRunState.FAILED, ModelRunState.CANCELED}:
        status, stage, progress, retryable = (
            CandidateInsightJobStatus.FAILED,
            "failed",
            100,
            True,
        )
    elif cancel_requested:
        status, stage, progress = CandidateInsightJobStatus.CANCELING, "canceling", 85
    elif run_state is ModelRunState.DISPATCHING:
        status, stage, progress = CandidateInsightJobStatus.PROCESSING, "generating", 65
    elif run_state is ModelRunState.AUTHORIZED:
        status, stage, progress = CandidateInsightJobStatus.PROCESSING, "preparing", 35
    elif job_state is JobState.RUNNING:
        status, stage, progress = CandidateInsightJobStatus.PROCESSING, "reading_source", 20
    else:
        status, stage, progress = CandidateInsightJobStatus.QUEUED, "queued", 5
    return CandidateInsightJobProjection(
        job_id=row["job_id"],
        status=status,
        stage=stage,
        progress=progress,
        retryable=retryable,
        run_id=row["run_id"],
        memory_id=row["memory_claim_id"],
        derived_object_id=row["derived_object_id"],
    )


class CandidateInsightJobWorker:
    """One-process, single-concurrency worker over the durable Job table."""

    def __init__(
        self,
        *,
        dispatcher_sessions: async_sessionmaker[AsyncSession],
        vault_sessions: AsyncSessionFactory,
        sessions: AuthorizedVaultSessionOpener,
        runtime: QueuedCandidateInsightRuntime,
        poll_seconds: float = 0.35,
    ) -> None:
        self._dispatcher_sessions = dispatcher_sessions
        self._vault_sessions = vault_sessions
        self._sessions = sessions
        self._runtime = runtime
        self._poll_seconds = poll_seconds
        self._lease_owner = f"candidate-worker:{secrets.token_hex(8)}"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="candidate-insight-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._task, timeout=5)
            if not self._task.done():
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            lease = None
            try:
                async with self._dispatcher_sessions() as session, session.begin():
                    lease = await GlobalDispatcherRepository(session).claim_candidate_insight(
                        lease_owner=self._lease_owner,
                        lease_for=_LEASE_FOR,
                    )
                if lease is not None:
                    await self._process(lease)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No request content or provider diagnostics cross this worker boundary.
                _LOGGER.warning(
                    "candidate_worker_iteration_failed error_type=%s",
                    type(exc).__name__,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)

    async def _process(self, lease: object) -> None:
        from life_coach.jobs.contracts import DispatchLease

        if not isinstance(lease, DispatchLease):
            return
        context = await self._load_context(lease)
        if context is None:
            return
        if context.cancel_requested:
            await self._cancel_context(context)
            return
        failure: FailureClass | None = None
        try:
            if (
                context.requested_by_principal_id is None
                or context.membership_generation is None
                or context.expected_resource_revision is None
            ):
                raise EntryCandidateSourceUnavailable("candidate job context is unavailable")
            principal = AuthenticatedPrincipal(
                principal_id=context.requested_by_principal_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            async with self._sessions.open_for_principal(
                principal=principal,
                vault_id=lease.vault_id,
                expected_membership_generation=context.membership_generation,
            ) as authorized:
                revision, fragment_ids = await authorized.session.run_sync(
                    lambda session: GenerateCandidateInsightForEntry._resolve_entry(
                        session,
                        vault_id=lease.vault_id,
                        entry_id=context.resource_id,
                    )
                )
                if revision != context.expected_resource_revision:
                    raise EntryCandidateRevisionConflict(current_revision=revision)
            invocation = asyncio.create_task(
                self._runtime.run_for_principal(
                    principal=principal,
                    vault_id=lease.vault_id,
                    task_type=CANDIDATE_INSIGHT_TASK_TYPE,
                    fragment_ids=fragment_ids,
                    idempotency_key=context.idempotency_key,
                    expected_membership_generation=context.membership_generation,
                )
            )
            cancellation = asyncio.create_task(self._wait_for_cancel(context))
            done, _pending = await asyncio.wait(
                {invocation, cancellation}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation in done:
                try:
                    cancel_requested = cancellation.result()
                except Exception:
                    invocation.cancel()
                    with suppress(asyncio.CancelledError):
                        await invocation
                    raise
                if cancel_requested:
                    invocation.cancel()
                    with suppress(asyncio.CancelledError):
                        await invocation
                    await self._cancel_context(context)
                    return
            cancellation.cancel()
            with suppress(asyncio.CancelledError):
                await cancellation
            await invocation
            await self._complete_context(context)
            return
        except asyncio.CancelledError:
            raise
        except (EntryCandidateRevisionConflict, EntryCandidateSourceUnavailable):
            failure = FailureClass.SOURCE_CHANGED
        except (AuthenticationDenied, VaultMembershipDenied, ModelInvocationDenied):
            failure = FailureClass.POLICY_REVOKED
        except (ModelRunReplayInProgress, ModelRunProviderOutcomeUnknown):
            failure = FailureClass.EXTERNAL_OUTCOME_UNKNOWN
        except ModelRunReplayTerminal as exc:
            failure = (
                FailureClass.EXTERNAL_OUTCOME_UNKNOWN
                if exc.state is ModelRunState.UNKNOWN
                else FailureClass.DETERMINISTIC
            )
        except ModelRunProviderUnavailable:
            failure = FailureClass.TRANSIENT
        except (
            GatewayConfigurationError,
            ModelPolicyViolation,
            ProviderOutputDecodeError,
            StructuredOutputValidationError,
            ToolDirectiveRejected,
            ModelResultRejected,
            ModelRunResultDiscarded,
        ):
            failure = FailureClass.DETERMINISTIC
        except (
            ModelRunFinalizationConflict,
            ModelRunResultPersistenceError,
        ):
            failure = FailureClass.EXTERNAL_OUTCOME_UNKNOWN
        except Exception:
            failure = FailureClass.EXTERNAL_OUTCOME_UNKNOWN
        if failure is not None:
            await self._fail_context(context, failure)

    async def _load_context(self, lease: object) -> JobExecutionContext | None:
        from life_coach.jobs.contracts import DispatchLease

        assert isinstance(lease, DispatchLease)
        async with vault_transaction(self._vault_sessions, lease.vault_id) as session:
            repository = VaultProcessorRepository(session, lease.vault_id)
            await repository.initialize_scope()
            return await repository.load_execution_context(
                lease,
                lease_owner=self._lease_owner,
            )

    async def _wait_for_cancel(self, context: JobExecutionContext) -> bool:
        polls = 0
        while True:
            await asyncio.sleep(0.2)
            async with vault_transaction(self._vault_sessions, context.lease.vault_id) as session:
                repository = VaultProcessorRepository(session, context.lease.vault_id)
                await repository.initialize_scope()
                requested = await session.scalar(
                    select(Job.cancel_requested_at).where(
                        Job.id == context.lease.job_id,
                        Job.vault_id == context.lease.vault_id,
                    )
                )
                if polls % 50 == 0 and not await repository.heartbeat(
                    context.lease,
                    lease_owner=self._lease_owner,
                    lease_for=_LEASE_FOR,
                ):
                    return True
            if requested is not None:
                return True
            polls += 1

    async def _complete_context(self, context: JobExecutionContext) -> None:
        async with vault_transaction(self._vault_sessions, context.lease.vault_id) as session:
            repository = VaultProcessorRepository(session, context.lease.vault_id)
            await repository.initialize_scope()
            completed = await repository.mark_done_after_external_commit(
                context,
                lease_owner=self._lease_owner,
            )
            if not completed:
                raise RuntimeError("candidate job lease was lost before completion")

    async def _cancel_context(self, context: JobExecutionContext) -> None:
        async with vault_transaction(self._vault_sessions, context.lease.vault_id) as session:
            repository = VaultProcessorRepository(session, context.lease.vault_id)
            await repository.initialize_scope()
            await repository.mark_canceled_by_request(
                context,
                lease_owner=self._lease_owner,
            )

    async def _fail_context(
        self,
        context: JobExecutionContext,
        failure: FailureClass,
    ) -> None:
        async with vault_transaction(self._vault_sessions, context.lease.vault_id) as session:
            repository = VaultProcessorRepository(session, context.lease.vault_id)
            await repository.initialize_scope()
            await repository.record_failure(
                context,
                lease_owner=self._lease_owner,
                failure=failure,
                random_sample=0.5,
            )


__all__ = [
    "CANDIDATE_INSIGHT_JOB_PIPELINE",
    "CANDIDATE_INSIGHT_JOB_TYPE",
    "CandidateInsightJobProjection",
    "CandidateInsightJobService",
    "CandidateInsightJobStatus",
    "CandidateInsightJobWorker",
    "QueuedCandidateInsightRuntime",
]
