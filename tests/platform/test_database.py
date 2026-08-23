"""Unit tests for PostgreSQL contracts using explicit test doubles."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.platform.database import (
    InvalidVaultId,
    VaultTransactionRequired,
    build_async_engine,
    set_vault_scope,
    validate_vault_id,
)
from life_coach.platform.settings import AppEnvironment, Settings


def transactional_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction = Mock(return_value=object())
    return session


@pytest.mark.parametrize("value", ["", "not-a-uuid", "' ; SET app.vault_id = 'other"])
def test_validate_vault_id_rejects_invalid_values_without_reflection(value: str) -> None:
    with pytest.raises(InvalidVaultId) as exc_info:
        validate_vault_id(value)

    if value:
        assert value not in str(exc_info.value)


def test_validate_vault_id_accepts_uuid_objects_and_strings() -> None:
    vault_id = uuid4()

    assert validate_vault_id(vault_id) == vault_id
    assert validate_vault_id(str(vault_id)) == vault_id


async def test_set_vault_scope_uses_parameterized_transaction_local_config() -> None:
    session = transactional_session()
    vault_id = uuid4()

    returned_id = await set_vault_scope(session, vault_id)

    assert returned_id == vault_id
    session.execute.assert_awaited_once()
    statement, parameters = session.execute.await_args.args
    sql = str(statement)
    assert sql == "SELECT set_config('app.vault_id', :vault_id, true)"
    assert parameters == {"vault_id": str(vault_id)}
    assert "SET LOCAL" not in sql
    assert str(vault_id) not in sql


async def test_invalid_vault_id_never_reaches_database() -> None:
    session = transactional_session()

    with pytest.raises(InvalidVaultId):
        await set_vault_scope(session, "not-a-uuid")

    session.execute.assert_not_awaited()


async def test_set_vault_scope_requires_existing_transaction() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction = Mock(return_value=None)

    with pytest.raises(VaultTransactionRequired):
        await set_vault_scope(session, UUID("12345678-1234-5678-1234-567812345678"))

    session.execute.assert_not_awaited()


async def test_engine_hides_bound_parameters_from_database_errors() -> None:
    settings = Settings(
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
    )
    engine = build_async_engine(settings)

    try:
        assert engine.sync_engine.hide_parameters is True
    finally:
        await engine.dispose()
