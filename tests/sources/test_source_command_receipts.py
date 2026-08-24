from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from life_coach.jobs.contracts import IdempotencyConflict
from life_coach.jobs.payloads import (
    InvalidRequestHashError,
    VaultRequestFingerprint,
    canonical_request_hash,
)
from life_coach.modules.sources.command_receipts import (
    CrossVaultSourceCommandError,
    SourceCommandReceiptRepository,
    SourceCommandReceiptSpec,
)
from life_coach.modules.sources.models import SourceCommandReceipt

_HMAC_KEY = b"source-command-test-hmac-key-material"
_POSTGRES_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]
_UNSET = object()


def _fingerprint(vault_id: uuid.UUID, value: object) -> VaultRequestFingerprint:
    return canonical_request_hash(
        value,  # type: ignore[arg-type]
        vault_id=vault_id,
        hmac_key=_HMAC_KEY,
    )


def _spec(vault_id: uuid.UUID, *, request: int = 1) -> SourceCommandReceiptSpec:
    return SourceCommandReceiptSpec(
        vault_id=vault_id,
        operation="source.create",
        client_key_hash=_fingerprint(vault_id, {"client_key": "opaque"}),
        request_hash=_fingerprint(vault_id, {"request": request}),
        resource_id=uuid.uuid4(),
        resource_revision_id=uuid.uuid4(),
        result_revision_no=1,
        source_generation=3,
        policy_epoch=2,
    )


def _receipt(spec: SourceCommandReceiptSpec) -> SourceCommandReceipt:
    return SourceCommandReceipt(
        id=uuid.uuid4(),
        vault_id=spec.vault_id,
        operation=spec.operation,
        client_key_hash=str(spec.client_key_hash),
        request_hash=str(spec.request_hash),
        resource_id=spec.resource_id,
        resource_revision_id=spec.resource_revision_id,
        result_revision_no=spec.result_revision_no,
        source_generation=spec.source_generation,
        policy_epoch=spec.policy_epoch,
    )


class FakeScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def one_or_none(self) -> object | None:
        return self._value


class FakeExecuteResult:
    def __init__(self, scalar: object = _UNSET) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object | None:
        return None if self._scalar is _UNSET else self._scalar


class ScriptedAsyncSession:
    def __init__(
        self,
        *,
        execute_results: Sequence[FakeExecuteResult] = (),
        scalar_results: Sequence[FakeScalarResult] = (),
    ) -> None:
        self._execute_results = list(execute_results)
        self._scalar_results = list(scalar_results)
        self.execute_calls: list[object] = []
        self.scalars_calls: list[object] = []

    async def execute(self, statement: object) -> FakeExecuteResult:
        self.execute_calls.append(statement)
        if not self._execute_results:
            raise AssertionError("unexpected execute call")
        return self._execute_results.pop(0)

    async def scalars(self, statement: object) -> FakeScalarResult:
        self.scalars_calls.append(statement)
        if not self._scalar_results:
            raise AssertionError("unexpected scalars call")
        return self._scalar_results.pop(0)


def _repository(
    session: ScriptedAsyncSession,
    vault_id: uuid.UUID,
) -> SourceCommandReceiptRepository:
    return SourceCommandReceiptRepository(cast(Any, session), vault_id)


def _compiled(statement: object) -> str:
    return " ".join(
        str(statement.compile(dialect=_POSTGRES_DIALECT)).lower().split()  # type: ignore[attr-defined]
    )


def test_source_command_receipt_schema_is_content_free_and_vault_scoped() -> None:
    table = SourceCommandReceipt.__table__
    column_names = set(table.columns.keys())

    assert column_names == {
        "id",
        "vault_id",
        "operation",
        "client_key_hash",
        "request_hash",
        "resource_id",
        "resource_revision_id",
        "result_revision_no",
        "source_generation",
        "policy_epoch",
        "created_at",
    }
    assert not column_names.intersection(
        {"content", "plaintext", "ciphertext", "title", "payload", "idempotency_key"}
    )
    assert ("vault_id", "operation", "client_key_hash") in {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if hasattr(constraint, "columns")
    }


def test_source_command_receipt_postgresql_constraints_bind_vault_and_hashes() -> None:
    ddl = " ".join(
        str(CreateTable(SourceCommandReceipt.__table__).compile(dialect=_POSTGRES_DIALECT))
        .lower()
        .split()
    )

    assert "unique (vault_id, operation, client_key_hash)" in ddl
    assert "foreign key(vault_id, resource_id)" in ddl
    assert "references source_document (vault_id, id)" in ddl
    assert "foreign key(vault_id, resource_id, resource_revision_id)" in ddl
    assert "references source_revision (vault_id, document_id, id)" in ddl
    assert "deferrable initially deferred" in ddl
    assert "length(client_key_hash) = 79" in ddl
    assert "client_key_hash like 'hmac-sha256:v1:%%'" in ddl
    assert "length(request_hash) = 79" in ddl
    assert "request_hash like 'hmac-sha256:v1:%%'" in ddl
    assert "result_revision_no is null or result_revision_no >= 1" in ddl
    assert "source_generation >= 0" in ddl
    assert "policy_epoch >= 0" in ddl


def test_source_command_spec_and_row_never_contain_raw_key_or_plaintext() -> None:
    vault_id = uuid.uuid4()
    raw_key = "offline-device-private-idempotency-key"
    plaintext = "private diary content"
    spec = SourceCommandReceiptSpec(
        vault_id=vault_id,
        operation="source.create",
        client_key_hash=_fingerprint(vault_id, {"key": raw_key}),
        request_hash=_fingerprint(vault_id, {"content": plaintext}),
        resource_id=uuid.uuid4(),
        resource_revision_id=uuid.uuid4(),
        result_revision_no=1,
        source_generation=1,
        policy_epoch=0,
    )
    row = _receipt(spec)

    for representation in (
        repr(spec),
        repr(row),
        str(spec.client_key_hash),
        str(spec.request_hash),
    ):
        assert raw_key not in representation
        assert plaintext not in representation
    assert row.client_key_hash.startswith("hmac-sha256:v1:")
    assert row.request_hash.startswith("hmac-sha256:v1:")


@pytest.mark.parametrize(
    "changes",
    [
        {"result_revision_no": 0},
        {"source_generation": -1},
        {"policy_epoch": -1},
        {"operation": "not allowed whitespace"},
    ],
)
def test_source_command_spec_rejects_invalid_technical_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(_spec(uuid.uuid4()), **changes)


def test_source_command_spec_rejects_unminted_or_cross_vault_fingerprints() -> None:
    vault_id = uuid.uuid4()
    other_vault = uuid.uuid4()
    spec = _spec(vault_id)

    with pytest.raises(InvalidRequestHashError):
        replace(spec, request_hash=cast(Any, "hmac-sha256:v1:" + "0" * 64))
    with pytest.raises(InvalidRequestHashError):
        replace(spec, client_key_hash=_fingerprint(other_vault, {"key": "same"}))


@pytest.mark.asyncio
async def test_receipt_find_returns_content_free_replay_projection() -> None:
    vault_id = uuid.uuid4()
    spec = _spec(vault_id)
    existing = _receipt(spec)
    session = ScriptedAsyncSession(scalar_results=[FakeScalarResult(existing)])

    found = await _repository(session, vault_id).find(
        operation=spec.operation,
        client_key_hash=spec.client_key_hash,
        request_hash=spec.request_hash,
    )

    assert found is not None
    assert found.created is False
    assert found.receipt_id == existing.id
    assert found.resource_id == spec.resource_id
    assert found.resource_revision_id == spec.resource_revision_id
    assert not hasattr(found, "content")
    assert not hasattr(found, "idempotency_key")
    sql = _compiled(session.scalars_calls[0])
    assert "source_command_receipt.vault_id" in sql
    assert "source_command_receipt.operation" in sql
    assert "source_command_receipt.client_key_hash" in sql


@pytest.mark.asyncio
async def test_receipt_find_rejects_same_key_for_a_different_request() -> None:
    vault_id = uuid.uuid4()
    existing_spec = _spec(vault_id, request=1)
    requested_spec = _spec(vault_id, request=2)
    session = ScriptedAsyncSession(scalar_results=[FakeScalarResult(_receipt(existing_spec))])

    with pytest.raises(IdempotencyConflict, match="different request"):
        await _repository(session, vault_id).find(
            operation=existing_spec.operation,
            client_key_hash=existing_spec.client_key_hash,
            request_hash=requested_spec.request_hash,
        )


@pytest.mark.asyncio
async def test_receipt_reserve_uses_scoped_conflict_target_and_returns_inserted_binding() -> None:
    vault_id = uuid.uuid4()
    spec = _spec(vault_id)
    inserted = _receipt(spec)
    session = ScriptedAsyncSession(execute_results=[FakeExecuteResult(inserted)])

    reservation = await _repository(session, vault_id).reserve(spec)

    assert reservation.created is True
    assert reservation.resource_id == spec.resource_id
    assert reservation.resource_revision_id == spec.resource_revision_id
    sql = _compiled(session.execute_calls[0])
    assert "on conflict (vault_id, operation, client_key_hash) do nothing" in sql
    assert "returning" in sql


@pytest.mark.asyncio
async def test_receipt_repository_rejects_cross_vault_reservation_before_database_io() -> None:
    repository_vault = uuid.uuid4()
    session = ScriptedAsyncSession()

    with pytest.raises(CrossVaultSourceCommandError):
        await _repository(session, repository_vault).reserve(_spec(uuid.uuid4()))

    assert session.execute_calls == []
    assert session.scalars_calls == []
