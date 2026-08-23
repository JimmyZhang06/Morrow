from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.jobs.contracts import (
    CrossVaultAccessError,
    DispatchLease,
    ExactOutboundAuthorization,
    FenceSnapshot,
    IdempotencyConflict,
    JobExecutionContext,
    JobSpec,
    LeaseLostError,
    OutboundExecutionGuardError,
    OutboundExecutionTicket,
    OutboundOperationSpec,
    OutboundReconciliationTicket,
    ProcessorScopeError,
    StagedOutboundExecution,
)
from life_coach.jobs.enums import (
    CompletionStatus,
    FenceCheckpoint,
    JobQueue,
    JobState,
    OutboundAuthorizationDecision,
    OutboundOperationState,
    ReconciliationOutcome,
)
from life_coach.jobs.payloads import canonical_request_hash
from life_coach.jobs.repository import (
    OutboundOperationRepository,
    VaultJobRepository,
    VaultProcessorRepository,
)

_UNSET = object()
_HMAC_KEY = b"test-only-request-hmac-key-material"


def _request_hash(vault_id: uuid.UUID, request: object) -> str:
    return canonical_request_hash(
        request,  # type: ignore[arg-type]
        vault_id=vault_id,
        hmac_key=_HMAC_KEY,
    )


class FakeResult:
    def __init__(
        self,
        *,
        scalar: object = _UNSET,
        mapping: Mapping[str, Any] | None = None,
        scalar_values: Sequence[object] = (),
    ) -> None:
        self._scalar = scalar
        self._mapping = mapping
        self._scalar_values = scalar_values

    def scalar_one_or_none(self) -> object | None:
        return None if self._scalar is _UNSET else self._scalar

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> Mapping[str, Any] | None:
        return self._mapping

    def one(self) -> Mapping[str, Any]:
        assert self._mapping is not None
        return self._mapping

    def scalars(self) -> Sequence[object]:
        return self._scalar_values


class ScriptedAsyncSession:
    def __init__(
        self,
        results: Sequence[FakeResult],
        *,
        active_transaction: bool = True,
    ) -> None:
        self._results = list(results)
        self.calls: list[tuple[object, Mapping[str, object] | None]] = []
        self.transaction: FakeTransaction | None = FakeTransaction() if active_transaction else None
        self.savepoints: list[FakeSavepoint] = []

    async def execute(
        self, statement: object, params: Mapping[str, object] | None = None
    ) -> FakeResult:
        if self.transaction is None:
            self.transaction = FakeTransaction()
        self.calls.append((statement, params))
        if not self._results:
            raise AssertionError("unexpected database call")
        return self._results.pop(0)

    def get_transaction(self) -> FakeTransaction | None:
        return self.transaction

    def begin_nested(self) -> FakeSavepoint:
        savepoint = FakeSavepoint()
        self.savepoints.append(savepoint)
        return savepoint


class FakeTransaction:
    is_active = True


class FakeSavepoint:
    rolled_back = False

    async def __aenter__(self) -> FakeSavepoint:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> bool:
        self.rolled_back = exc_type is not None
        return False


class StaticAuthorizationPort:
    def __init__(
        self,
        decision: OutboundAuthorizationDecision = OutboundAuthorizationDecision.ACCEPTED,
    ) -> None:
        self.decision = decision
        self.bindings: list[ExactOutboundAuthorization] = []

    async def consume_exact(
        self,
        _session: AsyncSession,
        *,
        binding: ExactOutboundAuthorization,
    ) -> OutboundAuthorizationDecision:
        self.bindings.append(binding)
        return self.decision


class StaticFenceReader:
    def __init__(self, current: FenceSnapshot) -> None:
        self.current = current
        self.current_reads = 0
        self.locked_reads = 0
        self.resource_ids: list[uuid.UUID] = []

    async def read_current(
        self,
        _session: AsyncSession,
        *,
        vault_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> FenceSnapshot:
        assert vault_id
        assert resource_id
        self.resource_ids.append(resource_id)
        self.current_reads += 1
        return self.current

    async def read_for_update(
        self,
        _session: AsyncSession,
        *,
        vault_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> FenceSnapshot:
        assert vault_id
        assert resource_id
        self.resource_ids.append(resource_id)
        self.locked_reads += 1
        return self.current


def _context(lease: DispatchLease, *, policy: int = 4, source: int = 9) -> JobExecutionContext:
    return JobExecutionContext(
        lease=lease,
        job_type="extract_memory",
        queue=JobQueue.INGEST_TEXT,
        resource_id=uuid.uuid4(),
        resource_revision_id=uuid.uuid4(),
        pipeline_version="pipeline-v1",
        consent_snapshot_id=uuid.uuid4(),
        expected_fence=FenceSnapshot(policy, source),
        payload={},
        attempts=1,
        max_attempts=5,
    )


def _outbound_spec(vault_id: uuid.UUID) -> OutboundOperationSpec:
    return OutboundOperationSpec(
        vault_id=vault_id,
        connector="todoist",
        operation="create_task",
        resource_id=uuid.uuid4(),
        local_resource_version=str(uuid.uuid4()),
        request_hash=_request_hash(vault_id, {"payload": "opaque"}),
        provider_idempotency_key="provider:1001",
        authorization_id=uuid.uuid4(),
        authorization_generation=3,
        initial_fence=FenceSnapshot(4, 9),
        max_attempts=3,
    )


def _outbound_binding_row(
    spec: OutboundOperationSpec,
    *,
    attempts: int = 0,
    reconciliation_generation: int = 0,
) -> dict[str, object]:
    return {
        "authorization_id": spec.authorization_id,
        "authorization_generation": spec.authorization_generation,
        "connector": spec.connector,
        "operation": spec.operation,
        "resource_id": spec.resource_id,
        "local_resource_version": spec.local_resource_version,
        "request_hash": spec.request_hash,
        "provider_idempotency_key": spec.provider_idempotency_key,
        "policy_epoch": spec.initial_fence.policy_epoch,
        "source_generation": spec.initial_fence.source_generation,
        "initial_tombstoned": spec.initial_fence.tombstoned,
        "attempts": attempts,
        "max_attempts": spec.max_attempts,
        "reconciliation_generation": reconciliation_generation,
        "max_reconciliation_attempts": spec.max_reconciliation_attempts,
    }


def _reconciliation_ticket(
    session: ScriptedAsyncSession,
    operation_id: uuid.UUID,
    vault_id: uuid.UUID,
    generation: int,
) -> OutboundReconciliationTicket:
    return OutboundReconciliationTicket(
        operation_id,
        vault_id,
        generation,
        cast(Any, session.transaction),
    )


@pytest.mark.asyncio
async def test_old_generation_cannot_heartbeat_or_promote_result() -> None:
    vault_id = uuid.uuid4()
    job_id = uuid.uuid4()
    old_lease = DispatchLease(job_id, vault_id, lease_generation=1)
    new_lease = DispatchLease(job_id, vault_id, lease_generation=2)
    durable_binding = {
        "resource_id": uuid.uuid4(),
        "policy_epoch": 4,
        "source_generation": 9,
    }
    session = ScriptedAsyncSession(
        [
            FakeResult(),  # SET LOCAL
            FakeResult(),  # stale heartbeat: conditional UPDATE returned no row
            FakeResult(),  # stale completion cannot load a live exact claim
            FakeResult(mapping=durable_binding),  # current claim binding from the Job row
            FakeResult(scalar=job_id),  # exact claim locked after authority
            FakeResult(scalar=job_id),  # current complete
        ]
    )
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    promoted: list[int] = []

    async def persist(_session: object) -> None:
        promoted.append(1)

    assert not await repository.heartbeat(
        old_lease, lease_owner="worker:1001", lease_for=timedelta(seconds=30)
    )
    old_status = await repository.complete(
        _context(old_lease),
        lease_owner="worker:1001",
        fence_reader=StaticFenceReader(FenceSnapshot(4, 9)),
        persist_result=persist,  # type: ignore[arg-type]
    )
    new_status = await repository.complete(
        _context(new_lease),
        lease_owner="worker:1002",
        fence_reader=StaticFenceReader(FenceSnapshot(4, 9)),
        persist_result=persist,  # type: ignore[arg-type]
    )

    assert old_status is CompletionStatus.LEASE_LOST
    assert new_status is CompletionStatus.DONE
    assert promoted == [1]
    assert session.calls[1][1]["lease_generation"] == 1  # type: ignore[index]
    assert session.calls[2][1]["claim_lease_generation"] == 1  # type: ignore[index]
    assert session.calls[3][1]["claim_lease_generation"] == 2  # type: ignore[index]
    assert session.calls[4][1]["claim_lease_generation"] == 2  # type: ignore[index]
    assert session.calls[5][1]["lease_generation"] == 2  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current",
    [FenceSnapshot(5, 9), FenceSnapshot(4, 10), FenceSnapshot(4, 9, tombstoned=True)],
)
async def test_revocation_while_external_call_is_in_flight_discards_result(
    current: FenceSnapshot,
) -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, lease_generation=3)
    session = ScriptedAsyncSession(
        [
            FakeResult(),
            FakeResult(
                mapping={
                    "resource_id": uuid.uuid4(),
                    "policy_epoch": 4,
                    "source_generation": 9,
                }
            ),
            FakeResult(scalar=lease.job_id),
            FakeResult(scalar=lease.job_id),
        ]
    )
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    promoted = False

    async def persist(_session: object) -> None:
        nonlocal promoted
        promoted = True

    # The provider has returned; the authoritative snapshot was re-read under this transaction.
    fence_reader = StaticFenceReader(current)
    status = await repository.complete(
        _context(lease),
        lease_owner="worker:1001",
        fence_reader=fence_reader,
        persist_result=persist,  # type: ignore[arg-type]
    )

    assert status is CompletionStatus.DISCARDED
    assert not promoted
    assert fence_reader.locked_reads == 1


@pytest.mark.asyncio
async def test_source_read_and_each_external_call_use_authoritative_reader() -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, lease_generation=1)
    durable = {
        "resource_id": uuid.uuid4(),
        "policy_epoch": 4,
        "source_generation": 9,
    }
    session = ScriptedAsyncSession(
        [
            FakeResult(),
            FakeResult(mapping=durable),
            FakeResult(scalar=lease.job_id),
            FakeResult(mapping=durable),
            FakeResult(scalar=lease.job_id),
        ]
    )
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    reader = StaticFenceReader(FenceSnapshot(4, 9))
    context = _context(lease)

    await repository.gate_authoritatively(
        context,
        FenceCheckpoint.BEFORE_SOURCE_READ,
        reader,
        lease_owner="worker:1001",
    )
    await repository.gate_authoritatively(
        context,
        FenceCheckpoint.BEFORE_EXTERNAL_CALL,
        reader,
        lease_owner="worker:1001",
    )

    assert reader.current_reads == 0
    assert reader.locked_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint",
    [FenceCheckpoint.BEFORE_SOURCE_READ, FenceCheckpoint.BEFORE_EXTERNAL_CALL],
)
async def test_lost_lease_cannot_cross_sensitive_gate(checkpoint: FenceCheckpoint) -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, lease_generation=1)
    session = ScriptedAsyncSession([FakeResult(), FakeResult()])
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    reader = StaticFenceReader(FenceSnapshot(4, 9))

    with pytest.raises(LeaseLostError):
        await repository.gate_authoritatively(
            _context(lease),
            checkpoint,
            reader,
            lease_owner="worker:9999",
        )

    assert reader.locked_reads == 0
    params = session.calls[1][1]
    assert params is not None
    assert params["claim_lease_generation"] == 1
    sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "job.lease_owner =" in sql
    assert "job.lease_generation =" in sql
    assert "job.lease_expires_at > clock_timestamp()" in sql
    assert "for update" not in sql


@pytest.mark.asyncio
async def test_completion_ignores_forged_context_fence_and_resource() -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, lease_generation=2)
    real_resource_id = uuid.uuid4()
    session = ScriptedAsyncSession(
        [
            FakeResult(),
            FakeResult(
                mapping={
                    "resource_id": real_resource_id,
                    "policy_epoch": 4,
                    "source_generation": 9,
                }
            ),
            FakeResult(scalar=lease.job_id),
            FakeResult(scalar=lease.job_id),
        ]
    )
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    forged = _context(lease, policy=999, source=999)
    reader = StaticFenceReader(FenceSnapshot(999, 999))
    promoted = False

    async def persist(_session: object) -> None:
        nonlocal promoted
        promoted = True

    status = await repository.complete(
        forged,
        lease_owner="worker:1001",
        fence_reader=reader,
        persist_result=persist,  # type: ignore[arg-type]
    )

    assert status is CompletionStatus.DISCARDED
    assert not promoted
    assert reader.locked_reads == 1
    assert reader.resource_ids == [real_resource_id]


@pytest.mark.asyncio
async def test_result_promotion_failure_rolls_back_job_completion_savepoint() -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, lease_generation=2)
    durable = {
        "resource_id": uuid.uuid4(),
        "policy_epoch": 4,
        "source_generation": 9,
    }
    session = ScriptedAsyncSession(
        [
            FakeResult(),
            FakeResult(mapping=durable),
            FakeResult(scalar=lease.job_id),
            FakeResult(scalar=lease.job_id),
        ]
    )
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()

    async def persist(_session: object) -> None:
        raise RuntimeError("derived write failed")

    with pytest.raises(RuntimeError, match="derived write failed"):
        await repository.complete(
            _context(lease),
            lease_owner="worker:1001",
            fence_reader=StaticFenceReader(FenceSnapshot(4, 9)),
            persist_result=persist,  # type: ignore[arg-type]
        )

    assert session.savepoints[-1].rolled_back


@pytest.mark.asyncio
async def test_processor_requires_scope_and_rejects_cross_vault_lease() -> None:
    vault_id = uuid.uuid4()
    repository = VaultProcessorRepository(  # type: ignore[arg-type]
        ScriptedAsyncSession([FakeResult()]), vault_id
    )
    foreign = DispatchLease(uuid.uuid4(), uuid.uuid4(), 1)

    with pytest.raises(ProcessorScopeError):
        await repository.heartbeat(
            foreign, lease_owner="worker:1001", lease_for=timedelta(seconds=10)
        )
    await repository.initialize_scope()
    with pytest.raises(CrossVaultAccessError):
        await repository.heartbeat(
            foreign, lease_owner="worker:1001", lease_for=timedelta(seconds=10)
        )


@pytest.mark.asyncio
async def test_outbox_replay_materializes_one_job_without_committing() -> None:
    vault_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    request_hash = _request_hash(vault_id, {"event": str(event_id), "subscriber": "index"})
    spec = JobSpec(
        vault_id=vault_id,
        job_type="index_source",
        resource_id=uuid.uuid4(),
        resource_revision_id=uuid.uuid4(),
        pipeline_version="pipeline-v1",
        idempotency_key="subscriber:1001",
        request_hash=request_hash,
    )
    event_row = {
        "id": event_id,
        "vault_id": vault_id,
        "resource_id": spec.resource_id,
        "resource_revision_id": spec.resource_revision_id,
        "pipeline_version": spec.pipeline_version,
        "expected_job_types": [spec.job_type],
    }
    existing_job = {"id": job_id, "vault_id": vault_id, "request_hash": request_hash}
    session = ScriptedAsyncSession(
        [
            FakeResult(mapping=event_row),
            FakeResult(scalar=job_id),
            FakeResult(),  # mark dispatched
            FakeResult(mapping=event_row),
            FakeResult(),  # ON CONFLICT replay
            FakeResult(mapping=existing_job),
            FakeResult(),  # idempotent mark dispatched
        ]
    )
    repository = VaultJobRepository(session, vault_id)  # type: ignore[arg-type]

    first = await repository.materialize_outbox_jobs(event_id, [spec])
    replay = await repository.materialize_outbox_jobs(event_id, [spec])

    assert first[0].resource_id == replay[0].resource_id == job_id
    assert first[0].created
    assert not replay[0].created
    assert len(session.calls) == 7
    insert_sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "on conflict (outbox_event_id, job_type)" in insert_sql
    assert "where outbox_event_id is not null do nothing" in insert_sql


@pytest.mark.asyncio
async def test_outbox_cannot_mark_a_partial_subscriber_set_dispatched() -> None:
    vault_id = uuid.uuid4()
    event_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    session = ScriptedAsyncSession(
        [
            FakeResult(
                mapping={
                    "id": event_id,
                    "vault_id": vault_id,
                    "resource_id": resource_id,
                    "resource_revision_id": revision_id,
                    "pipeline_version": "pipeline-v1",
                    "expected_job_types": ["extract_source", "index_source"],
                }
            )
        ]
    )
    repository = VaultJobRepository(session, vault_id)  # type: ignore[arg-type]
    partial = JobSpec(
        vault_id=vault_id,
        job_type="extract_source",
        resource_id=resource_id,
        resource_revision_id=revision_id,
        pipeline_version="pipeline-v1",
        idempotency_key="subscriber:1002",
        request_hash=_request_hash(vault_id, {"subscriber": "extract"}),
    )

    with pytest.raises(ValueError, match="subscriber set"):
        await repository.materialize_outbox_jobs(event_id, [partial])

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_repository_rejects_same_key_with_different_request_hash() -> None:
    vault_id = uuid.uuid4()
    stored_hash = _request_hash(vault_id, {"payload": "first"})
    new_hash = _request_hash(vault_id, {"payload": "different"})
    session = ScriptedAsyncSession(
        [
            FakeResult(),  # scoped INSERT conflict
            FakeResult(mapping={"id": uuid.uuid4(), "request_hash": stored_hash}),
        ]
    )
    repository = VaultJobRepository(session, vault_id)  # type: ignore[arg-type]
    spec = JobSpec(
        vault_id=vault_id,
        job_type="extract_memory",
        resource_id=uuid.uuid4(),
        pipeline_version="pipeline-v1",
        idempotency_key="client:1001",
        request_hash=new_hash,
    )

    with pytest.raises(IdempotencyConflict):
        await repository.enqueue(spec)


@pytest.mark.asyncio
async def test_outbound_create_persists_and_replays_the_exact_immutable_binding() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    existing = {"id": operation_id, **_outbound_binding_row(spec)}
    session = ScriptedAsyncSession(
        [FakeResult(scalar=operation_id), FakeResult(), FakeResult(mapping=existing)]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    created = await repository.create(spec)
    replay = await repository.create(spec)

    assert created.created
    assert not replay.created
    assert created.resource_id == replay.resource_id == operation_id
    compiled = session.calls[0][0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    assert compiled.params["authorization_id"] == spec.authorization_id
    assert compiled.params["authorization_generation"] == spec.authorization_generation
    assert compiled.params["resource_id"] == spec.resource_id
    assert compiled.params["policy_epoch"] == spec.initial_fence.policy_epoch
    assert compiled.params["source_generation"] == spec.initial_fence.source_generation
    assert compiled.params["initial_tombstoned"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_field",
    [
        "provider_idempotency_key",
        "authorization_id",
        "authorization_generation",
        "resource_id",
        "policy_epoch",
        "source_generation",
        "max_attempts",
        "max_reconciliation_attempts",
    ],
)
async def test_outbound_replay_rejects_changed_immutable_binding(changed_field: str) -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    existing = {"id": uuid.uuid4(), **_outbound_binding_row(spec)}
    if changed_field in {"authorization_id", "resource_id"}:
        existing[changed_field] = uuid.uuid4()
    elif changed_field == "provider_idempotency_key":
        existing[changed_field] = "provider:9999"
    else:
        existing[changed_field] = int(existing[changed_field]) + 1
    session = ScriptedAsyncSession([FakeResult(), FakeResult(mapping=existing)])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    with pytest.raises(IdempotencyConflict, match="immutable binding"):
        await repository.create(spec)


@pytest.mark.asyncio
async def test_outbound_replay_rejects_a_changed_request_fingerprint() -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    existing = {"id": uuid.uuid4(), **_outbound_binding_row(spec)}
    existing["request_hash"] = _request_hash(vault_id, {"payload": "changed"})
    session = ScriptedAsyncSession([FakeResult(), FakeResult(mapping=existing)])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    with pytest.raises(IdempotencyConflict, match="different request"):
        await repository.create(spec)


@pytest.mark.asyncio
async def test_outbound_execution_consumes_exact_authorization_and_locked_fence() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession(
        [FakeResult(mapping=_outbound_binding_row(spec)), FakeResult(scalar=4)]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    authorization = StaticAuthorizationPort()
    reader = StaticFenceReader(spec.initial_fence)

    staged = await repository.begin_execution(
        operation_id,
        authorization_port=authorization,
        fence_reader=reader,
    )

    assert staged == StagedOutboundExecution(operation_id, vault_id, 4)
    assert reader.locked_reads == 1
    assert len(authorization.bindings) == 1
    binding = authorization.bindings[0]
    assert binding.outbound_operation_id == operation_id
    assert binding.authorization_id == spec.authorization_id
    assert binding.authorization_generation == spec.authorization_generation
    assert binding.resource_id == spec.resource_id
    assert binding.provider_idempotency_key == spec.provider_idempotency_key
    assert binding.request_hash == spec.request_hash
    assert binding.initial_fence == spec.initial_fence
    assert len(session.savepoints) == 1
    assert not session.savepoints[0].rolled_back


@pytest.mark.asyncio
async def test_execution_ticket_is_unavailable_until_staging_transaction_commits() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    stage_session = ScriptedAsyncSession(
        [FakeResult(mapping=_outbound_binding_row(spec)), FakeResult(scalar=4)]
    )
    stage_repository = OutboundOperationRepository(  # type: ignore[arg-type]
        stage_session, vault_id
    )
    staged = await stage_repository.begin_execution(
        operation_id,
        authorization_port=StaticAuthorizationPort(),
        fence_reader=StaticFenceReader(spec.initial_fence),
    )
    assert staged is not None

    with pytest.raises(OutboundExecutionGuardError, match="must commit"):
        await stage_repository.load_committed_execution_ticket(
            staged,
            authorization_port=StaticAuthorizationPort(),
            fence_reader=StaticFenceReader(spec.initial_fence),
        )

    committed_row = {**_outbound_binding_row(spec), "execution_generation": 4}
    ticket_session = ScriptedAsyncSession(
        [FakeResult(mapping=committed_row), FakeResult(scalar=4), FakeResult(scalar=operation_id)],
        active_transaction=False,
    )
    ticket_repository = OutboundOperationRepository(  # type: ignore[arg-type]
        ticket_session, vault_id
    )
    ticket = await ticket_repository.load_committed_execution_ticket(
        staged,
        authorization_port=StaticAuthorizationPort(),
        fence_reader=StaticFenceReader(spec.initial_fence),
    )

    assert ticket is not None
    assert ticket.execution_generation == 4
    assert await ticket_repository.mark_succeeded(ticket, external_id="provider:2001")


@pytest.mark.asyncio
async def test_stale_staged_generation_cannot_claim_a_new_execution_attempt() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    stale = StagedOutboundExecution(operation_id, vault_id, execution_generation=1)
    session = ScriptedAsyncSession([FakeResult()], active_transaction=False)
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    authorization = StaticAuthorizationPort()
    reader = StaticFenceReader(FenceSnapshot(4, 9))

    ticket = await repository.load_committed_execution_ticket(
        stale,
        authorization_port=authorization,
        fence_reader=reader,
    )

    assert ticket is None
    assert authorization.bindings == []
    assert reader.locked_reads == 0
    compiled = session.calls[0][0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    assert 1 in compiled.params.values()
    sql = " ".join(str(compiled).lower().split())
    assert "outbound_operation.execution_generation =" in sql


@pytest.mark.asyncio
async def test_concurrent_binding_change_rolls_back_consumed_authorization() -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    authorization = StaticAuthorizationPort()
    session = ScriptedAsyncSession([FakeResult(mapping=_outbound_binding_row(spec)), FakeResult()])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    with pytest.raises(OutboundExecutionGuardError, match="changed before execution"):
        await repository.begin_execution(
            uuid.uuid4(),
            authorization_port=authorization,
            fence_reader=StaticFenceReader(spec.initial_fence),
        )

    assert len(authorization.bindings) == 1
    assert session.savepoints[0].rolled_back


@pytest.mark.asyncio
async def test_outbound_execution_fails_closed_when_a_port_is_missing() -> None:
    vault_id = uuid.uuid4()
    session = ScriptedAsyncSession([])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    with pytest.raises(OutboundExecutionGuardError, match="requires authorization"):
        await repository.begin_execution(
            uuid.uuid4(),
            authorization_port=None,
            fence_reader=StaticFenceReader(FenceSnapshot(1, 1)),
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_pending_execution_budget_exhaustion_requires_manual_review() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = replace(_outbound_spec(vault_id), max_attempts=2)
    session = ScriptedAsyncSession(
        [FakeResult(mapping=_outbound_binding_row(spec, attempts=2)), FakeResult()]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    authorization = StaticAuthorizationPort()

    staged = await repository.begin_execution(
        operation_id,
        authorization_port=authorization,
        fence_reader=StaticFenceReader(spec.initial_fence),
    )

    assert staged is None
    assert authorization.bindings == []
    assert session.savepoints == []
    sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "attempts >= outbound_operation.max_attempts" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        OutboundAuthorizationDecision.REVOKED,
        OutboundAuthorizationDecision.EXPIRED,
        OutboundAuthorizationDecision.MISMATCH,
        OutboundAuthorizationDecision.CONSUMED_ELSEWHERE,
    ],
)
async def test_revoked_expired_or_payload_changed_authorization_blocks_execution(
    decision: OutboundAuthorizationDecision,
) -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession([FakeResult(mapping=_outbound_binding_row(spec)), FakeResult()])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    staged = await repository.begin_execution(
        uuid.uuid4(),
        authorization_port=StaticAuthorizationPort(decision),
        fence_reader=StaticFenceReader(spec.initial_fence),
    )

    assert staged is None
    assert len(session.calls) == 2
    assert not session.savepoints[0].rolled_back


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current",
    [FenceSnapshot(5, 9), FenceSnapshot(4, 10), FenceSnapshot(4, 9, tombstoned=True)],
)
async def test_outbound_execution_rechecks_policy_source_and_tombstone(
    current: FenceSnapshot,
) -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession([FakeResult(mapping=_outbound_binding_row(spec)), FakeResult()])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    authorization = StaticAuthorizationPort()

    staged = await repository.begin_execution(
        uuid.uuid4(),
        authorization_port=authorization,
        fence_reader=StaticFenceReader(current),
    )

    assert staged is None
    assert authorization.bindings == []
    assert not session.savepoints[0].rolled_back


@pytest.mark.asyncio
async def test_unknown_outbound_operation_is_not_blindly_executed() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession(
        [
            FakeResult(),  # begin_execution matched zero rows because DB state is unknown
            FakeResult(mapping=_outbound_binding_row(spec)),
            FakeResult(scalar=1),  # unknown -> reconciling, generation 1
        ]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    generation = await repository.begin_execution(
        operation_id,
        authorization_port=StaticAuthorizationPort(),
        fence_reader=StaticFenceReader(FenceSnapshot(1, 1)),
    )
    authorization = StaticAuthorizationPort()
    reader = StaticFenceReader(spec.initial_fence)
    reconciliation_ticket = await repository.begin_reconciliation(
        operation_id,
        authorization_port=authorization,
        fence_reader=reader,
    )

    assert generation is None
    assert reconciliation_ticket is not None
    assert reconciliation_ticket.reconciliation_generation == 1
    assert reconciliation_ticket._transaction is session.transaction
    assert len(authorization.bindings) == 1
    assert reader.locked_reads == 1
    cas_sql = " ".join(
        str(session.calls[2][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    for column in (
        "authorization_id",
        "authorization_generation",
        "resource_id",
        "request_hash",
        "policy_epoch",
        "source_generation",
        "provider_idempotency_key",
    ):
        assert f"outbound_operation.{column} =" in cas_sql


@pytest.mark.asyncio
async def test_tombstoned_unknown_operation_cannot_query_provider() -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession([FakeResult(mapping=_outbound_binding_row(spec)), FakeResult()])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    authorization = StaticAuthorizationPort()

    ticket = await repository.begin_reconciliation(
        uuid.uuid4(),
        authorization_port=authorization,
        fence_reader=StaticFenceReader(FenceSnapshot(4, 9, tombstoned=True)),
    )

    assert ticket is None
    assert authorization.bindings == []
    reject_sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "outbound_operation.request_hash =" in reject_sql
    assert "outbound_operation.authorization_id =" in reject_sql
    assert "outbound_operation.resource_id =" in reject_sql


@pytest.mark.asyncio
async def test_revoked_unknown_operation_cannot_query_provider() -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession([FakeResult(mapping=_outbound_binding_row(spec)), FakeResult()])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    ticket = await repository.begin_reconciliation(
        uuid.uuid4(),
        authorization_port=StaticAuthorizationPort(OutboundAuthorizationDecision.REVOKED),
        fence_reader=StaticFenceReader(spec.initial_fence),
    )

    assert ticket is None
    reject_sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "outbound_operation.request_hash =" in reject_sql
    assert "outbound_operation.authorization_generation =" in reject_sql


@pytest.mark.asyncio
async def test_confirmed_absence_reauthorizes_exact_binding_before_retry() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession(
        [
            FakeResult(mapping=_outbound_binding_row(spec, attempts=1)),
            FakeResult(scalar=operation_id),
        ]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    authorization = StaticAuthorizationPort()
    reader = StaticFenceReader(spec.initial_fence)
    ticket = _reconciliation_ticket(session, operation_id, vault_id, 2)

    state = await repository.resolve_reconciliation(
        ticket,
        outcome=ReconciliationOutcome.CONFIRMED_NOT_FOUND,
        authorization_port=authorization,
        fence_reader=reader,
    )

    assert state is OutboundOperationState.PENDING
    assert len(authorization.bindings) == 1
    assert authorization.bindings[0].request_hash == spec.request_hash
    assert reader.locked_reads == 1
    assert not session.savepoints[0].rolled_back


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        OutboundAuthorizationDecision.REVOKED,
        OutboundAuthorizationDecision.EXPIRED,
        OutboundAuthorizationDecision.MISMATCH,
    ],
)
async def test_unknown_cannot_return_to_pending_after_authority_change(
    decision: OutboundAuthorizationDecision,
) -> None:
    vault_id = uuid.uuid4()
    spec = _outbound_spec(vault_id)
    session = ScriptedAsyncSession(
        [FakeResult(mapping=_outbound_binding_row(spec, attempts=1)), FakeResult()]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    operation_id = uuid.uuid4()
    ticket = _reconciliation_ticket(session, operation_id, vault_id, 2)

    state = await repository.resolve_reconciliation(
        ticket,
        outcome=ReconciliationOutcome.CONFIRMED_NOT_FOUND,
        authorization_port=StaticAuthorizationPort(decision),
        fence_reader=StaticFenceReader(spec.initial_fence),
    )

    expected = (
        OutboundOperationState.CANCELED
        if decision is OutboundAuthorizationDecision.REVOKED
        else OutboundOperationState.MANUAL_REVIEW
    )
    assert state is expected
    assert not session.savepoints[0].rolled_back


@pytest.mark.asyncio
async def test_unknown_retry_budget_exhaustion_requires_manual_review() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = replace(_outbound_spec(vault_id), max_attempts=2)
    session = ScriptedAsyncSession(
        [
            FakeResult(mapping=_outbound_binding_row(spec, attempts=2)),
            FakeResult(scalar=operation_id),
        ]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    ticket = _reconciliation_ticket(session, operation_id, vault_id, 3)

    state = await repository.resolve_reconciliation(
        ticket,
        outcome=ReconciliationOutcome.CONFIRMED_NOT_FOUND,
    )

    assert state is OutboundOperationState.MANUAL_REVIEW
    assert session.savepoints == []
    sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "attempts >= outbound_operation.max_attempts" in sql


@pytest.mark.asyncio
async def test_reconciliation_budget_exhaustion_requires_manual_review() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    spec = replace(_outbound_spec(vault_id), max_reconciliation_attempts=2)
    session = ScriptedAsyncSession(
        [
            FakeResult(mapping=_outbound_binding_row(spec, reconciliation_generation=2)),
            FakeResult(),
        ]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    ticket = await repository.begin_reconciliation(
        operation_id,
        authorization_port=StaticAuthorizationPort(),
        fence_reader=StaticFenceReader(spec.initial_fence),
    )

    assert ticket is None
    sql = " ".join(
        str(session.calls[1][0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
    )
    assert "reconciliation_generation =" in sql
    assert (
        OutboundOperationState.MANUAL_REVIEW
        in session.calls[1][0]
        .compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        )
        .params.values()
    )


@pytest.mark.asyncio
async def test_crashed_outbound_execution_recovers_to_reconciliation() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    session = ScriptedAsyncSession([FakeResult(scalar_values=[operation_id])])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    recovered = await repository.recover_expired_executions()

    assert recovered == (operation_id,)


@pytest.mark.asyncio
async def test_old_reconciler_generation_cannot_overwrite_new_result() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    session = ScriptedAsyncSession([FakeResult(), FakeResult(scalar=operation_id)])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]
    stale_ticket = _reconciliation_ticket(session, operation_id, vault_id, 1)
    current_ticket = _reconciliation_ticket(session, operation_id, vault_id, 2)

    stale = await repository.resolve_reconciliation(
        stale_ticket,
        outcome=ReconciliationOutcome.FOUND,
        external_id="provider:2001",
    )
    current = await repository.resolve_reconciliation(
        current_ticket,
        outcome=ReconciliationOutcome.FOUND,
        external_id="provider:2001",
    )

    assert stale is None
    assert current is OutboundOperationState.SUCCEEDED


@pytest.mark.asyncio
async def test_waiting_job_resumes_only_after_explicit_resolution() -> None:
    vault_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = ScriptedAsyncSession([FakeResult(mapping={"id": job_id, "state": JobState.QUEUED})])
    repository = VaultJobRepository(session, vault_id)  # type: ignore[arg-type]

    resumed = await repository.resolve_waiting_job(job_id, target_state=JobState.QUEUED)

    assert resumed is JobState.QUEUED


@pytest.mark.asyncio
async def test_expired_outbound_leases_reject_late_worker_results() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    session = ScriptedAsyncSession([FakeResult(), FakeResult(), FakeResult()])
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    transaction = cast(Any, session.transaction)
    ticket = OutboundExecutionTicket(operation_id, vault_id, 1, transaction)
    reconciliation_ticket = OutboundReconciliationTicket(operation_id, vault_id, 1, transaction)

    assert not await repository.mark_unknown(ticket)
    assert not await repository.mark_succeeded(
        ticket,
        external_id="provider:2001",
    )
    assert (
        await repository.resolve_reconciliation(
            reconciliation_ticket,
            outcome=ReconciliationOutcome.FOUND,
            external_id="provider:2001",
        )
        is None
    )

    sql = [
        " ".join(
            str(call[0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
        )
        for call in session.calls
    ]
    assert "execution_expires_at > clock_timestamp()" in sql[0]
    assert "execution_expires_at > clock_timestamp()" in sql[1]
    assert "reconciliation_expires_at > clock_timestamp()" in sql[2]


@pytest.mark.asyncio
async def test_processor_scope_marker_cannot_survive_transaction_reuse() -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, 1)
    session = ScriptedAsyncSession([FakeResult()])
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()

    session.transaction = FakeTransaction()
    with pytest.raises(ProcessorScopeError):
        await repository.heartbeat(
            lease, lease_owner="worker:1001", lease_for=timedelta(seconds=10)
        )
