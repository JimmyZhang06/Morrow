"""Transaction-boundary tests for governed model runtime orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict

from life_coach.ai.contracts import (
    ModelInputKind,
    ModelInputRef,
    RetentionPolicy,
    SensitivityLevel,
)
from life_coach.ai.fakes import DeterministicFakeProvider
from life_coach.ai.provider import (
    ModelGateway,
    ProviderExecutionError,
    StructuredOutputValidationError,
    UntrustedModelInput,
)
from life_coach.application.model_gateway import (
    AuthorizedSourceFragment,
    GovernedModelGateway,
    ModelInvocationDenied,
    ModelTaskDefinition,
    PreparedModelInvocation,
    SourceAuthoritySnapshot,
)
from life_coach.application.model_runtime import (
    GovernedModelRuntime,
    ModelResultContext,
    ModelResultRejected,
    ModelRunFinalizationConflict,
    ModelRunFingerprintFactory,
    ModelRunProviderOutcomeUnknown,
    ModelRunResultDiscarded,
    ModelRunResultPersistenceError,
    ModelRunTimeout,
)
from life_coach.modules.consent.models import ConsentPurpose
from life_coach.modules.consent.provider_policy import ProviderPolicy
from life_coach.modules.identity.models import DataClass, MembershipRole
from life_coach.modules.identity.service import VaultSnapshot
from life_coach.modules.model_runs.contracts import (
    ModelRunArtifactSpec,
    ModelRunDispatchTicket,
    ModelRunInputSpec,
    ModelRunReceiptSpec,
    ModelRunWrite,
)
from life_coach.platform.auth import (
    AuthenticatedPrincipal,
    AuthorizedVaultContext,
    AuthorizedVaultSession,
    VaultMembershipDenied,
)

_PRIVATE_TEXT = "private diary text that must not enter runtime receipts"
_HMAC_KEY = b"test-only-governed-runtime-hmac-key"
_IDEMPOTENCY_KEY = "modelrun:0123456789abcdef"


class RuntimeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _Session:
    def __init__(self, owner: _Sessions, ordinal: int) -> None:
        self.owner = owner
        self.ordinal = ordinal
        self.pending: list[tuple[uuid.UUID, str, str | None]] = []
        self.pending_results: list[tuple[ModelResultContext, BaseModel]] = []

    async def run_sync(self, operation: Any) -> Any:
        assert self.owner.active_transactions == 1
        return operation(cast(Any, object()))


class _Sessions:
    def __init__(self, *, vault_id: uuid.UUID, principal_id: uuid.UUID) -> None:
        self.vault_id = vault_id
        self.principal = AuthenticatedPrincipal(
            principal_id=principal_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self.context = AuthorizedVaultContext(
            principal_id=principal_id,
            vault_id=vault_id,
            role=MembershipRole.OWNER,
            membership_generation=3,
        )
        self.events: list[str] = []
        self.active_transactions = 0
        self.commit_count = 0
        self.states: dict[uuid.UUID, str] = {}
        self.errors: dict[uuid.UUID, str] = {}
        self.persisted_results: list[tuple[ModelResultContext, BaseModel]] = []
        self.after_commit: Any = None
        self.deny_member_on_open: int | None = None

    async def authenticate(self, authorization: str | None) -> AuthenticatedPrincipal:
        assert authorization == "Bearer opaque-token"
        self.events.append("authenticate")
        return self.principal

    @asynccontextmanager
    async def open_for_principal(
        self,
        *,
        principal: AuthenticatedPrincipal,
        vault_id: uuid.UUID | str,
        expected_membership_generation: int | None = None,
    ) -> AsyncIterator[AuthorizedVaultSession]:
        assert principal == self.principal
        assert uuid.UUID(str(vault_id)) == self.vault_id
        if expected_membership_generation is not None:
            assert expected_membership_generation == self.context.membership_generation
        ordinal = self.commit_count + 1
        session = _Session(self, ordinal)
        self.active_transactions += 1
        self.events.append(f"tx{ordinal}.open")
        if self.deny_member_on_open == ordinal:
            self.events.append(f"tx{ordinal}.rollback")
            self.active_transactions -= 1
            self.events.append(f"tx{ordinal}.close")
            raise VaultMembershipDenied("membership revoked")
        try:
            yield AuthorizedVaultSession(
                context=self.context,
                session=cast(Any, session),
            )
        except BaseException:
            self.events.append(f"tx{ordinal}.rollback")
            raise
        else:
            for run_id, state, error_code in session.pending:
                self.states[run_id] = state
                if error_code is not None:
                    self.errors[run_id] = error_code
            self.persisted_results.extend(session.pending_results)
            self.commit_count += 1
            self.events.append(f"tx{ordinal}.commit")
            if self.after_commit is not None:
                self.after_commit(self.commit_count)
        finally:
            self.active_transactions -= 1
            self.events.append(f"tx{ordinal}.close")

    @asynccontextmanager
    async def open_for_model_run_bookkeeping(
        self,
        *,
        vault_id: uuid.UUID | str,
    ) -> AsyncIterator[Any]:
        assert uuid.UUID(str(vault_id)) == self.vault_id
        ordinal = self.commit_count + 1
        session = _Session(self, ordinal)
        self.active_transactions += 1
        self.events.append(f"bookkeeping{ordinal}.open")
        try:
            yield _Receipts(session, self, uuid.UUID(int=0))
        except BaseException:
            self.events.append(f"bookkeeping{ordinal}.rollback")
            raise
        else:
            for run_id, state, error_code in session.pending:
                self.states[run_id] = state
                if error_code is not None:
                    self.errors[run_id] = error_code
            self.persisted_results.extend(session.pending_results)
            self.commit_count += 1
            self.events.append(f"bookkeeping{ordinal}.commit")
        finally:
            self.active_transactions -= 1
            self.events.append(f"bookkeeping{ordinal}.close")


class _Receipts:
    def __init__(self, session: _Session, owner: _Sessions, run_id: uuid.UUID) -> None:
        self.session = session
        self.owner = owner
        self.run_id = run_id
        self.specs: list[ModelRunReceiptSpec] = []
        self.inputs: list[tuple[ModelRunInputSpec, ...]] = []
        self.allow_success = True

    async def attach_artifact(
        self,
        ticket: ModelRunDispatchTicket,
        artifact: ModelRunArtifactSpec,
    ) -> Any:
        self.owner.events.append("receipt.attach_artifact")
        assert self.owner.states.get(ticket.run_id) == "dispatching"
        return object()

    async def prepare(
        self,
        spec: ModelRunReceiptSpec,
        inputs: tuple[ModelRunInputSpec, ...],
    ) -> ModelRunWrite:
        self.owner.events.append("receipt.prepare")
        self.specs.append(spec)
        self.inputs.append(inputs)
        self.session.pending.append((self.run_id, "authorized", None))
        return ModelRunWrite(run_id=self.run_id, created=True)

    async def claim_dispatch(
        self,
        run_id: uuid.UUID,
        *,
        lease_for: timedelta,
    ) -> ModelRunDispatchTicket | None:
        self.owner.events.append("receipt.claim")
        assert lease_for > timedelta(0)
        if self.owner.states.get(run_id) != "authorized":
            return None
        self.session.pending.append((run_id, "dispatching", None))
        return ModelRunDispatchTicket(run_id, self.owner.vault_id, 1)

    async def mark_succeeded(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        provider_request_id: str | None = None,
    ) -> bool:
        del provider_request_id
        self.owner.events.append("receipt.succeeded")
        if not self.allow_success or self.owner.states.get(ticket.run_id) != "dispatching":
            return False
        self.session.pending.append((ticket.run_id, "succeeded", None))
        return True

    async def mark_failed(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
        provider_request_id: str | None = None,
    ) -> bool:
        del provider_request_id
        self.owner.events.append("receipt.failed")
        if self.owner.states.get(ticket.run_id) != "dispatching":
            return False
        self.session.pending.append((ticket.run_id, "failed", safe_error_code))
        return True

    async def mark_unknown(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
        provider_request_id: str | None = None,
    ) -> bool:
        del provider_request_id
        self.owner.events.append("receipt.unknown")
        if self.owner.states.get(ticket.run_id) != "dispatching":
            return False
        self.session.pending.append((ticket.run_id, "unknown", safe_error_code))
        return True

    async def mark_denied(self, run_id: uuid.UUID, *, safe_error_code: str) -> bool:
        self.owner.events.append("receipt.denied")
        if self.owner.states.get(run_id) != "authorized":
            return False
        self.session.pending.append((run_id, "denied", safe_error_code))
        return True


class _Gateway:
    def __init__(
        self,
        *,
        sessions: _Sessions,
        prepared: PreparedModelInvocation,
        outcome: BaseModel | BaseException,
    ) -> None:
        self.sessions = sessions
        self.prepared = prepared
        self.outcome = outcome
        self.authority_current = True
        self.invoke_count = 0

    def prepare(self, **_kwargs: object) -> PreparedModelInvocation:
        assert self.sessions.active_transactions == 1
        self.sessions.events.append("gateway.prepare")
        return self.prepared

    def assert_current(self, **_kwargs: object) -> None:
        assert self.sessions.active_transactions == 1
        self.sessions.events.append("gateway.assert_current")
        if not self.authority_current:
            raise ModelInvocationDenied("authority changed")

    def invoke(self, **_kwargs: object) -> BaseModel:
        self.sessions.events.append("gateway.invoke")
        assert self.sessions.active_transactions == 0
        self.invoke_count += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _BlockingGateway(_Gateway):
    def __init__(
        self,
        *,
        sessions: _Sessions,
        prepared: PreparedModelInvocation,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(
            sessions=sessions,
            prepared=prepared,
            outcome=RuntimeOutput(value="unused"),
        )
        self.entered = entered
        self.release = release

    def invoke(self, **_kwargs: object) -> BaseModel:
        self.sessions.events.append("gateway.invoke")
        assert self.sessions.active_transactions == 0
        self.invoke_count += 1
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("blocking provider test did not release the provider")
        return RuntimeOutput(value="late")


class _Persister:
    def __init__(
        self,
        sessions: _Sessions,
        artifact: ModelRunArtifactSpec | None = None,
    ) -> None:
        self.sessions = sessions
        self.artifact = artifact
        self.calls: list[tuple[ModelResultContext, BaseModel]] = []

    async def persist(
        self,
        session: Any,
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> ModelRunArtifactSpec | None:
        assert self.sessions.active_transactions == 1
        self.sessions.events.append("persister.persist")
        self.calls.append((context, result))
        cast(_Session, session).pending_results.append((context, result))
        return self.artifact


class _FailingPersister(_Persister):
    async def persist(
        self,
        _session: Any,
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> None:
        await super().persist(_session, context=context, result=result)
        raise RuntimeError(_PRIVATE_TEXT)


class _RejectingPersister(_Persister):
    async def persist(
        self,
        _session: Any,
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> None:
        del context, result
        self.sessions.events.append("persister.reject")
        raise ModelResultRejected("output contract rejected")


def _prepared(
    vault_id: uuid.UUID,
    fragment_id: uuid.UUID,
    *,
    latency_ms: int = 200,
) -> PreparedModelInvocation:
    task = ModelTaskDefinition(
        task_type="claim_extraction",
        consent_purpose=ConsentPurpose.LONG_TERM_INFERENCE,
        provider="zero-retention-provider",
        model="structured-model",
        model_revision="revision-1",
        prompt_template_version="claim-v1",
        schema_version="1",
        pipeline_version="pipeline-v1",
        required_capabilities=frozenset({"structured_output"}),
        data_residency="eu",
        retention_policy=RetentionPolicy.ZERO_RETENTION,
        provider_retention_days=0,
        provider_training_use_enabled=False,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        output_type=RuntimeOutput,
        latency_budget_ms=latency_ms,
        cost_budget=Decimal("0.01"),
    )
    fragment = AuthorizedSourceFragment(
        document_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        fragment_id=fragment_id,
        recorded_at=datetime.now(UTC),
        data_class=DataClass.NORMAL,
        text=_PRIVATE_TEXT,
        text_hash=hashlib.sha256(_PRIVATE_TEXT.encode()).hexdigest(),
    )
    snapshot = SourceAuthoritySnapshot(
        vault=VaultSnapshot(vault_id=vault_id, policy_epoch=3, source_generation=5),
        purpose=ConsentPurpose.LONG_TERM_INFERENCE,
        consent_snapshot_id=f"consent:{'c' * 64}",
        consent_snapshot_uuid=uuid.uuid4(),
        consent_record_ids=(uuid.uuid4(),),
        provider_policy=ProviderPolicy(
            allowed_providers=("zero-retention-provider",),
            processing_regions=("eu",),
            max_retention_days=0,
        ),
        fragments=(fragment,),
        actual_sensitivity=SensitivityLevel.NORMAL,
    )
    return PreparedModelInvocation(
        task=task,
        snapshot=snapshot,
        input_refs=(
            ModelInputRef(
                vault_id=str(vault_id),
                kind=ModelInputKind.SOURCE_FRAGMENT,
                object_id=str(fragment_id),
            ),
        ),
        model_input=UntrustedModelInput.from_text(
            _PRIVATE_TEXT,
            source_refs=(str(fragment_id),),
        ),
    )


def _runtime(
    *,
    sessions: _Sessions,
    gateway: _Gateway,
    receipts: list[_Receipts],
    run_id: uuid.UUID,
    persister: _Persister | None = None,
) -> tuple[GovernedModelRuntime, _Persister]:
    sink = persister or _Persister(sessions)

    def receipt_factory(session: Any, _vault_id: uuid.UUID) -> _Receipts:
        receipt = _Receipts(cast(_Session, session), sessions, run_id)
        receipts.append(receipt)
        return receipt

    return (
        GovernedModelRuntime(
            sessions=sessions,
            gateway=cast(Any, gateway),
            fingerprints=ModelRunFingerprintFactory(_HMAC_KEY),
            result_persister=sink,
            receipt_factory=cast(Any, receipt_factory),
        ),
        sink,
    )


async def _run(
    runtime: GovernedModelRuntime,
    vault_id: uuid.UUID,
    fragment_id: uuid.UUID,
) -> BaseModel:
    return await runtime.run(
        authorization="Bearer opaque-token",
        vault_id=vault_id,
        task_type="claim_extraction",
        fragment_ids=(fragment_id,),
        idempotency_key=_IDEMPOTENCY_KEY,
    )


@pytest.mark.asyncio
async def test_success_commits_prepare_and_dispatch_before_sessionless_provider_io() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="candidate"),
    )
    receipts: list[_Receipts] = []
    runtime, persister = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=receipts,
        run_id=run_id,
    )

    result = await _run(runtime, vault_id, fragment_id)

    assert result == RuntimeOutput(value="candidate")
    assert sessions.states[run_id] == "succeeded"
    assert gateway.invoke_count == 1
    assert persister.calls[0][0].run_id == run_id
    assert sessions.persisted_results == persister.calls
    assert sessions.events.index("tx1.commit") < sessions.events.index("gateway.invoke")
    assert sessions.events.index("tx2.commit") < sessions.events.index("gateway.invoke")
    assert sessions.events.index("gateway.invoke") < sessions.events.index("tx3.open")
    assert [receipt.session.ordinal for receipt in receipts] == [1, 2, 3]
    assert str(receipts[0].specs[0].request_hash).startswith("hmac-sha256:v1:")
    assert _PRIVATE_TEXT not in repr(receipts[0].specs)
    assert _PRIVATE_TEXT not in repr(receipts[0].inputs)


@pytest.mark.asyncio
async def test_success_attaches_persisted_artifact_before_receipt_finalization() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="candidate"),
    )
    persister = _Persister(
        sessions,
        ModelRunArtifactSpec(
            vault_id=vault_id,
            derived_object_id=uuid.uuid4(),
            memory_claim_id=uuid.uuid4(),
        ),
    )
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
        persister=persister,
    )

    await _run(runtime, vault_id, fragment_id)

    assert sessions.events.index("persister.persist") < sessions.events.index(
        "receipt.attach_artifact"
    )
    assert sessions.events.index("receipt.attach_artifact") < sessions.events.index(
        "receipt.succeeded"
    )


@pytest.mark.asyncio
async def test_authority_change_after_prepare_commit_denies_without_provider_io() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="must not escape"),
    )
    sessions.after_commit = lambda count: setattr(gateway, "authority_current", count != 1)
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )

    with pytest.raises(ModelInvocationDenied, match="changed before model dispatch"):
        await _run(runtime, vault_id, fragment_id)

    assert gateway.invoke_count == 0
    assert sessions.states[run_id] == "denied"
    assert sessions.errors[run_id] == "authorization.changed_before_dispatch"


@pytest.mark.asyncio
async def test_provider_exception_is_unknown_and_never_retried() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=ProviderExecutionError("zero-retention-provider", 0),
    )
    runtime, persister = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )

    with pytest.raises(ModelRunProviderOutcomeUnknown):
        await _run(runtime, vault_id, fragment_id)

    assert gateway.invoke_count == 1
    assert sessions.states[run_id] == "unknown"
    assert sessions.errors[run_id] == "provider.execution_unknown"
    assert persister.calls == []


@pytest.mark.asyncio
async def test_schema_failure_is_a_known_failed_outcome() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=StructuredOutputValidationError(
            output_type=RuntimeOutput,
            issues=(),
            attempts=2,
        ),
    )
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )

    with pytest.raises(StructuredOutputValidationError):
        await _run(runtime, vault_id, fragment_id)

    assert sessions.states[run_id] == "failed"
    assert sessions.errors[run_id] == "output.schema_rejected"


@pytest.mark.asyncio
async def test_bounded_repair_calls_share_the_persisted_logical_run_id() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    prepared = _prepared(vault_id, fragment_id)
    provider = DeterministicFakeProvider(
        [{"value": 123}, {"value": "repaired"}],
        provider_id="zero-retention-provider",
        data_residencies={"eu"},
        retention_policies={RetentionPolicy.ZERO_RETENTION},
    )
    kernel = GovernedModelGateway(
        gateway=ModelGateway([provider], max_repair_attempts=1),
        source_authority=cast(Any, object()),
        tasks=(prepared.task,),
    )
    gateway = _Gateway(
        sessions=sessions,
        prepared=prepared,
        outcome=RuntimeOutput(value="unused"),
    )

    def invoke_with_kernel(**kwargs: object) -> BaseModel:
        sessions.events.append("gateway.invoke")
        assert sessions.active_transactions == 0
        gateway.invoke_count += 1
        return kernel.invoke(
            prepared=prepared,
            run_id=cast(uuid.UUID, kwargs["run_id"]),
        )

    gateway.invoke = invoke_with_kernel  # type: ignore[method-assign]
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )

    result = await _run(runtime, vault_id, fragment_id)

    assert result == RuntimeOutput(value="repaired")
    assert provider.call_count == 2
    assert {call.run_spec.run_id for call in provider.calls} == {str(run_id)}
    assert sessions.states[run_id] == "succeeded"


@pytest.mark.asyncio
async def test_timeout_marks_unknown_while_provider_thread_has_no_transaction() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    entered, release = threading.Event(), threading.Event()
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _BlockingGateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id, latency_ms=20),
        entered=entered,
        release=release,
    )
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )

    try:
        with pytest.raises(ModelRunTimeout):
            await _run(runtime, vault_id, fragment_id)
    finally:
        release.set()

    assert entered.is_set()
    assert gateway.invoke_count == 1
    assert sessions.states[run_id] == "unknown"
    assert sessions.errors[run_id] == "provider.timeout"


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_is_shielded_to_unknown_then_propagated() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    entered, release = threading.Event(), threading.Event()
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _BlockingGateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id, latency_ms=1_000),
        entered=entered,
        release=release,
    )
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )
    task = asyncio.create_task(_run(runtime, vault_id, fragment_id))
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()

    assert sessions.states[run_id] == "unknown"
    assert sessions.errors[run_id] == "caller.canceled_after_dispatch"


@pytest.mark.asyncio
async def test_stale_success_finalize_neither_persists_nor_returns_model_output() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="discarded"),
    )
    receipts: list[_Receipts] = []
    runtime, persister = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=receipts,
        run_id=run_id,
    )
    original_factory = runtime._receipt_factory

    def stale_factory(session: Any, vault: uuid.UUID) -> _Receipts:
        receipt = cast(_Receipts, original_factory(session, vault))
        if cast(_Session, session).ordinal == 3:
            receipt.allow_success = False
        return receipt

    runtime._receipt_factory = stale_factory

    with pytest.raises(ModelRunFinalizationConflict):
        await _run(runtime, vault_id, fragment_id)

    assert sessions.states[run_id] == "dispatching"
    assert len(persister.calls) == 1
    assert sessions.persisted_results == []
    assert "tx3.rollback" in sessions.events


@pytest.mark.asyncio
async def test_membership_revoked_after_provider_discards_result_and_settles_receipt() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    sessions.deny_member_on_open = 3
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="discarded"),
    )
    runtime, persister = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
    )

    with pytest.raises(ModelRunResultDiscarded, match="authorization changed"):
        await _run(runtime, vault_id, fragment_id)

    assert gateway.invoke_count == 1
    assert sessions.states[run_id] == "failed"
    assert sessions.errors[run_id] == "authorization.changed_after_dispatch"
    assert sessions.persisted_results == []
    assert persister.calls == []
    assert "bookkeeping3.commit" in sessions.events


@pytest.mark.asyncio
async def test_persister_exception_rolls_back_candidate_and_success_receipt_together() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="candidate"),
    )
    persister = _FailingPersister(sessions)
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
        persister=persister,
    )

    with pytest.raises(ModelRunResultPersistenceError) as exc_info:
        await _run(runtime, vault_id, fragment_id)

    assert _PRIVATE_TEXT not in str(exc_info.value)
    assert sessions.states[run_id] == "dispatching"
    assert sessions.persisted_results == []
    assert "tx3.rollback" in sessions.events


@pytest.mark.asyncio
async def test_deterministic_result_rejection_commits_failed_without_artifact() -> None:
    vault_id, principal_id, fragment_id, run_id = (uuid.uuid4() for _ in range(4))
    sessions = _Sessions(vault_id=vault_id, principal_id=principal_id)
    gateway = _Gateway(
        sessions=sessions,
        prepared=_prepared(vault_id, fragment_id),
        outcome=RuntimeOutput(value="candidate"),
    )
    runtime, _ = _runtime(
        sessions=sessions,
        gateway=gateway,
        receipts=[],
        run_id=run_id,
        persister=_RejectingPersister(sessions),
    )

    with pytest.raises(ModelRunResultDiscarded, match="rejected by the durable sink"):
        await _run(runtime, vault_id, fragment_id)

    assert sessions.states[run_id] == "failed"
    assert sessions.errors[run_id] == "output.persistence_rejected"
    assert sessions.persisted_results == []
