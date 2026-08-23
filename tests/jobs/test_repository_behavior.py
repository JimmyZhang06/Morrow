from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.jobs.contracts import (
    CrossVaultAccessError,
    DispatchLease,
    FenceSnapshot,
    IdempotencyConflict,
    JobExecutionContext,
    JobSpec,
    ProcessorScopeError,
)
from life_coach.jobs.enums import (
    CompletionStatus,
    FenceCheckpoint,
    JobQueue,
    JobState,
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
    def __init__(self, results: Sequence[FakeResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[object, Mapping[str, object] | None]] = []
        self.transaction = FakeTransaction()

    async def execute(
        self, statement: object, params: Mapping[str, object] | None = None
    ) -> FakeResult:
        self.calls.append((statement, params))
        if not self._results:
            raise AssertionError("unexpected database call")
        return self._results.pop(0)

    def get_transaction(self) -> FakeTransaction:
        return self.transaction


class FakeTransaction:
    is_active = True


class StaticFenceReader:
    def __init__(self, current: FenceSnapshot) -> None:
        self.current = current
        self.current_reads = 0
        self.locked_reads = 0

    async def read_current(
        self,
        _session: AsyncSession,
        *,
        vault_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> FenceSnapshot:
        assert vault_id
        assert resource_id
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


@pytest.mark.asyncio
async def test_old_generation_cannot_heartbeat_or_promote_result() -> None:
    vault_id = uuid.uuid4()
    job_id = uuid.uuid4()
    old_lease = DispatchLease(job_id, vault_id, lease_generation=1)
    new_lease = DispatchLease(job_id, vault_id, lease_generation=2)
    session = ScriptedAsyncSession(
        [
            FakeResult(),  # SET LOCAL
            FakeResult(),  # stale heartbeat: conditional UPDATE returned no row
            FakeResult(),  # stale complete: conditional UPDATE returned no row
            FakeResult(scalar=job_id),  # current complete
        ]
    )
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    promoted: list[int] = []

    async def persist(_session: object) -> None:
        promoted.append(1)

    assert not await repository.heartbeat(
        old_lease, lease_owner="worker-a", lease_for=timedelta(seconds=30)
    )
    old_status = await repository.complete(
        _context(old_lease),
        lease_owner="worker-a",
        fence_reader=StaticFenceReader(FenceSnapshot(4, 9)),
        persist_result=persist,  # type: ignore[arg-type]
    )
    new_status = await repository.complete(
        _context(new_lease),
        lease_owner="worker-b",
        fence_reader=StaticFenceReader(FenceSnapshot(4, 9)),
        persist_result=persist,  # type: ignore[arg-type]
    )

    assert old_status is CompletionStatus.LEASE_LOST
    assert new_status is CompletionStatus.DONE
    assert promoted == [1]
    generation_params = [call[1]["lease_generation"] for call in session.calls[1:]]  # type: ignore[index]
    assert generation_params == [1, 1, 2]


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
    session = ScriptedAsyncSession([FakeResult(), FakeResult(scalar=lease.job_id)])
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
        lease_owner="worker-a",
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
    session = ScriptedAsyncSession([FakeResult()])
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()
    reader = StaticFenceReader(FenceSnapshot(4, 9))
    context = _context(lease)

    await repository.gate_authoritatively(context, FenceCheckpoint.BEFORE_SOURCE_READ, reader)
    await repository.gate_authoritatively(context, FenceCheckpoint.BEFORE_EXTERNAL_CALL, reader)

    assert reader.current_reads == 2
    assert reader.locked_reads == 0


@pytest.mark.asyncio
async def test_processor_requires_scope_and_rejects_cross_vault_lease() -> None:
    vault_id = uuid.uuid4()
    repository = VaultProcessorRepository(  # type: ignore[arg-type]
        ScriptedAsyncSession([FakeResult()]), vault_id
    )
    foreign = DispatchLease(uuid.uuid4(), uuid.uuid4(), 1)

    with pytest.raises(ProcessorScopeError):
        await repository.heartbeat(foreign, lease_owner="worker", lease_for=timedelta(seconds=10))
    await repository.initialize_scope()
    with pytest.raises(CrossVaultAccessError):
        await repository.heartbeat(foreign, lease_owner="worker", lease_for=timedelta(seconds=10))


@pytest.mark.asyncio
async def test_outbox_replay_materializes_one_job_without_committing() -> None:
    vault_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    request_hash = canonical_request_hash({"event": str(event_id), "subscriber": "index"})
    spec = JobSpec(
        vault_id=vault_id,
        job_type="index_source",
        resource_id=uuid.uuid4(),
        resource_revision_id=uuid.uuid4(),
        pipeline_version="pipeline-v1",
        idempotency_key="subscriber-index",
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
        idempotency_key="extract",
        request_hash=canonical_request_hash({"subscriber": "extract"}),
    )

    with pytest.raises(ValueError, match="subscriber set"):
        await repository.materialize_outbox_jobs(event_id, [partial])

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_repository_rejects_same_key_with_different_request_hash() -> None:
    vault_id = uuid.uuid4()
    stored_hash = canonical_request_hash({"payload": "first"})
    new_hash = canonical_request_hash({"payload": "different"})
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
        idempotency_key="same-key",
        request_hash=new_hash,
    )

    with pytest.raises(IdempotencyConflict):
        await repository.enqueue(spec)


@pytest.mark.asyncio
async def test_unknown_outbound_operation_is_not_blindly_executed() -> None:
    vault_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    session = ScriptedAsyncSession(
        [
            FakeResult(),  # begin_execution matched zero rows because DB state is unknown
            FakeResult(scalar=1),  # unknown -> reconciling, generation 1
        ]
    )
    repository = OutboundOperationRepository(session, vault_id)  # type: ignore[arg-type]

    generation = await repository.begin_execution(operation_id)
    reconciliation_started = await repository.begin_reconciliation(operation_id)

    assert generation is None
    assert reconciliation_started == 1


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

    stale = await repository.resolve_reconciliation(
        operation_id,
        reconciliation_generation=1,
        outcome=ReconciliationOutcome.FOUND,
        external_id="provider-id",
    )
    current = await repository.resolve_reconciliation(
        operation_id,
        reconciliation_generation=2,
        outcome=ReconciliationOutcome.FOUND,
        external_id="provider-id",
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

    assert not await repository.mark_unknown(operation_id, execution_generation=1)
    assert not await repository.mark_succeeded(
        operation_id,
        execution_generation=1,
        external_id="provider-id",
    )
    assert (
        await repository.resolve_reconciliation(
            operation_id,
            reconciliation_generation=1,
            outcome=ReconciliationOutcome.FOUND,
            external_id="provider-id",
        )
        is None
    )

    sql = [
        " ".join(
            str(call[0].compile(dialect=postgresql.dialect())).lower().split()  # type: ignore[attr-defined]
        )
        for call in session.calls
    ]
    assert "execution_expires_at > now()" in sql[0]
    assert "execution_expires_at > now()" in sql[1]
    assert "reconciliation_expires_at > now()" in sql[2]


@pytest.mark.asyncio
async def test_processor_scope_marker_cannot_survive_transaction_reuse() -> None:
    vault_id = uuid.uuid4()
    lease = DispatchLease(uuid.uuid4(), vault_id, 1)
    session = ScriptedAsyncSession([FakeResult()])
    repository = VaultProcessorRepository(session, vault_id)  # type: ignore[arg-type]
    await repository.initialize_scope()

    session.transaction = FakeTransaction()
    with pytest.raises(ProcessorScopeError):
        await repository.heartbeat(lease, lease_owner="worker", lease_for=timedelta(seconds=10))
