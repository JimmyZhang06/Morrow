from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql

from life_coach.ai.contracts import ModelInputKind, RetentionPolicy, SensitivityLevel
from life_coach.jobs.payloads import VaultRequestFingerprint, canonical_request_hash
from life_coach.modules.model_runs.contracts import (
    CrossVaultModelRunError,
    ModelRunDispatchTicket,
    ModelRunIdempotencyConflict,
    ModelRunInputSpec,
    ModelRunReceiptSpec,
)
from life_coach.modules.model_runs.repository import ModelRunRepository

_UNSET = object()
_HMAC_KEY = b"test-only-model-run-hmac-key-material"
_POSTGRES_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def _fingerprint(vault_id: uuid.UUID, value: object) -> VaultRequestFingerprint:
    return canonical_request_hash(
        value,  # type: ignore[arg-type]
        vault_id=vault_id,
        hmac_key=_HMAC_KEY,
    )


def _spec(vault_id: uuid.UUID, *, request: int = 1) -> ModelRunReceiptSpec:
    return ModelRunReceiptSpec(
        vault_id=vault_id,
        task_type="candidate_insight",
        idempotency_key="modelrun:1001",
        request_hash=_fingerprint(vault_id, {"request": request}),
        task_definition_hash=_fingerprint(vault_id, {"task": "candidate_insight:v1"}),
        provider="provider:1001",
        model="model:1001",
        model_revision="rev-1",
        prompt_template_version="prompt-v1",
        schema_version="schema-v1",
        pipeline_version="pipeline-v1",
        consent_snapshot_id=f"consent:{'c' * 64}",
        policy_epoch=3,
        source_generation=7,
        actual_sensitivity=SensitivityLevel.SENSITIVE,
        data_residency="region:1001",
        retention_policy=RetentionPolicy.ZERO_RETENTION,
    )


def _inputs(vault_id: uuid.UUID) -> tuple[ModelRunInputSpec, ...]:
    return (
        ModelRunInputSpec(
            vault_id=vault_id,
            kind=ModelInputKind.SOURCE_FRAGMENT,
            object_id=uuid.uuid4(),
            content_fingerprint=_fingerprint(vault_id, {"fragment": 1}),
            ordinal=0,
        ),
        ModelRunInputSpec(
            vault_id=vault_id,
            kind=ModelInputKind.SOURCE_FRAGMENT,
            object_id=uuid.uuid4(),
            content_fingerprint=_fingerprint(vault_id, {"fragment": 2}),
            ordinal=1,
        ),
    )


def _existing(run_id: uuid.UUID, spec: ModelRunReceiptSpec) -> dict[str, object]:
    return {
        "id": run_id,
        "request_hash": spec.request_hash,
        "task_definition_hash": spec.task_definition_hash,
        "provider": spec.provider,
        "model": spec.model,
        "model_revision": spec.model_revision,
        "prompt_template_version": spec.prompt_template_version,
        "schema_version": spec.schema_version,
        "pipeline_version": spec.pipeline_version,
        "consent_snapshot_id": spec.consent_snapshot_id,
        "policy_epoch": spec.policy_epoch,
        "source_generation": spec.source_generation,
        "actual_sensitivity": spec.actual_sensitivity,
        "data_residency": spec.data_residency,
        "retention_policy": spec.retention_policy,
    }


class FakeResult:
    def __init__(
        self,
        *,
        scalar: object = _UNSET,
        mapping: Mapping[str, Any] | None = None,
        rows: Sequence[object] = (),
        scalars: Sequence[object] = (),
    ) -> None:
        self._scalar = scalar
        self._mapping = mapping
        self._rows = rows
        self._scalars = scalars

    def scalar_one_or_none(self) -> object | None:
        return None if self._scalar is _UNSET else self._scalar

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> Mapping[str, Any] | None:
        return self._mapping

    def scalars(self) -> Sequence[object]:
        return self._scalars

    def __iter__(self) -> Iterator[object]:
        return iter(self._rows)


class ScriptedAsyncSession:
    def __init__(self, results: Sequence[FakeResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[object, Mapping[str, object] | None]] = []

    async def execute(
        self,
        statement: object,
        params: Mapping[str, object] | None = None,
    ) -> FakeResult:
        self.calls.append((statement, params))
        if not self._results:
            raise AssertionError("unexpected database call")
        return self._results.pop(0)


def _repository(
    session: ScriptedAsyncSession,
    vault_id: uuid.UUID,
) -> ModelRunRepository:
    return ModelRunRepository(cast(Any, session), vault_id)


def _compiled(statement: object) -> Any:
    return cast(Any, statement).compile(dialect=_POSTGRES_DIALECT)


@pytest.mark.asyncio
async def test_prepare_creates_one_content_free_receipt_and_ordered_inputs() -> None:
    vault_id, run_id = uuid.uuid4(), uuid.uuid4()
    inputs = _inputs(vault_id)
    session = ScriptedAsyncSession([FakeResult(scalar=run_id), FakeResult()])

    write = await _repository(session, vault_id).prepare(_spec(vault_id), tuple(reversed(inputs)))

    assert write.run_id == run_id
    assert write.created
    assert len(session.calls) == 2
    run_insert = session.calls[0][0]
    input_insert = session.calls[1][0]
    run_sql = " ".join(str(_compiled(run_insert)).lower().split())
    input_params = _compiled(input_insert).params
    assert "on conflict (vault_id, task_type, idempotency_key) do nothing" in run_sql
    assert "prompt" not in {column.name for column in cast(Any, input_insert).table.columns}
    assert input_params["ordinal_m0"] == 0
    assert input_params["ordinal_m1"] == 1


@pytest.mark.asyncio
async def test_identical_prepare_replay_returns_existing_run_without_new_inputs() -> None:
    vault_id, run_id = uuid.uuid4(), uuid.uuid4()
    spec, inputs = _spec(vault_id), _inputs(vault_id)
    stored_rows = [
        SimpleNamespace(
            kind=item.kind,
            object_id=item.object_id,
            content_fingerprint=str(item.content_fingerprint),
            ordinal=item.ordinal,
        )
        for item in inputs
    ]
    session = ScriptedAsyncSession(
        [
            FakeResult(),
            FakeResult(mapping=_existing(run_id, spec)),
            FakeResult(rows=stored_rows),
        ]
    )

    write = await _repository(session, vault_id).prepare(spec, inputs)

    assert write.run_id == run_id
    assert not write.created
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_prepare_replay_with_different_request_is_safe_conflict() -> None:
    vault_id, run_id = uuid.uuid4(), uuid.uuid4()
    stored_spec, new_spec = _spec(vault_id, request=1), _spec(vault_id, request=2)
    session = ScriptedAsyncSession(
        [FakeResult(), FakeResult(mapping=_existing(run_id, stored_spec))]
    )

    with pytest.raises(ModelRunIdempotencyConflict) as exc_info:
        await _repository(session, vault_id).prepare(new_spec, ())

    assert str(stored_spec.request_hash) not in str(exc_info.value)
    assert str(new_spec.request_hash) not in str(exc_info.value)
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_prepare_rejects_receipt_or_input_from_another_vault_without_io() -> None:
    vault_a, vault_b = uuid.uuid4(), uuid.uuid4()
    session = ScriptedAsyncSession([])
    repository = _repository(session, vault_a)

    with pytest.raises(CrossVaultModelRunError):
        await repository.prepare(_spec(vault_b), ())
    with pytest.raises(CrossVaultModelRunError):
        await repository.prepare(_spec(vault_a), _inputs(vault_b))

    assert session.calls == []


@pytest.mark.asyncio
async def test_claim_dispatch_is_single_authorized_state_cas() -> None:
    vault_id, run_id = uuid.uuid4(), uuid.uuid4()
    session = ScriptedAsyncSession(
        [FakeResult(mapping={"id": run_id, "vault_id": vault_id, "dispatch_generation": 4})]
    )

    ticket = await _repository(session, vault_id).claim_dispatch(
        run_id,
        lease_for=timedelta(seconds=30),
    )

    assert ticket == ModelRunDispatchTicket(run_id, vault_id, 4)
    statement, params = session.calls[0]
    sql = " ".join(str(_compiled(statement)).lower().split())
    assert "model_run.state =" in sql
    assert "dispatch_generation=(model_run.dispatch_generation +" in sql
    assert params == {
        "run_id": run_id,
        "vault_id": vault_id,
        "lease_for": timedelta(seconds=30),
    }


@pytest.mark.asyncio
async def test_claim_replay_or_terminal_run_returns_none() -> None:
    vault_id = uuid.uuid4()
    session = ScriptedAsyncSession([FakeResult()])

    assert (
        await _repository(session, vault_id).claim_dispatch(
            uuid.uuid4(),
            lease_for=timedelta(seconds=5),
        )
        is None
    )


@pytest.mark.asyncio
async def test_stale_generation_and_terminal_replay_cannot_overwrite_outcome() -> None:
    vault_id, run_id = uuid.uuid4(), uuid.uuid4()
    ticket = ModelRunDispatchTicket(run_id, vault_id, 2)
    session = ScriptedAsyncSession([FakeResult(scalar=run_id), FakeResult(), FakeResult()])
    repository = _repository(session, vault_id)

    assert await repository.mark_succeeded(ticket, provider_request_id="request:1001")
    assert not await repository.mark_failed(
        ticket,
        safe_error_code="provider.failed",
        provider_request_id="request:1001",
    )
    assert not await repository.mark_unknown(
        ModelRunDispatchTicket(run_id, vault_id, 1),
        safe_error_code="provider.timeout",
    )

    for _, params in session.calls:
        assert params is not None
        assert params["dispatch_generation"] in {1, 2}


@pytest.mark.asyncio
async def test_finalize_rejects_cross_vault_ticket_and_untrusted_error_text() -> None:
    vault_id = uuid.uuid4()
    repository = _repository(ScriptedAsyncSession([]), vault_id)

    with pytest.raises(CrossVaultModelRunError):
        await repository.mark_succeeded(ModelRunDispatchTicket(uuid.uuid4(), uuid.uuid4(), 1))
    private_error = "provider echoed private diary body"
    with pytest.raises(ValueError) as exc_info:
        await repository.mark_failed(
            ModelRunDispatchTicket(uuid.uuid4(), vault_id, 1),
            safe_error_code=private_error,
        )
    assert private_error not in str(exc_info.value)


@pytest.mark.asyncio
async def test_pre_dispatch_terminal_is_authorized_only_and_cannot_be_overwritten() -> None:
    vault_id, run_id = uuid.uuid4(), uuid.uuid4()
    session = ScriptedAsyncSession([FakeResult(scalar=run_id), FakeResult()])
    repository = _repository(session, vault_id)

    assert await repository.mark_denied(run_id, safe_error_code="policy.revoked")
    assert not await repository.mark_canceled(run_id, safe_error_code="request.canceled")

    sql = " ".join(str(_compiled(session.calls[0][0])).lower().split())
    assert "model_run.state =" in sql
    assert "returning model_run.id" in sql


@pytest.mark.asyncio
async def test_expired_dispatch_recovery_marks_unknown_instead_of_redispatching() -> None:
    vault_id = uuid.uuid4()
    recovered = (uuid.uuid4(), uuid.uuid4())
    session = ScriptedAsyncSession([FakeResult(scalars=recovered)])

    assert await _repository(session, vault_id).recover_expired_dispatches() == recovered
    statement, params = session.calls[0]
    sql = " ".join(str(_compiled(statement)).lower().split())
    assert "dispatch_expires_at <= clock_timestamp()" in sql
    assert "state=" in sql
    assert params == {"vault_id": vault_id}
