"""Unit tests for PostgreSQL contracts using explicit test doubles."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.orm import Session

from life_coach.platform.database import (
    InvalidVaultId,
    VaultAsyncSession,
    VaultScopeConflict,
    VaultScopeRequired,
    VaultTransactionRequired,
    build_async_engine,
    build_session_factory,
    require_vault_scope,
    set_vault_scope,
    validate_vault_id,
)
from life_coach.platform.settings import AppEnvironment, Settings


def unbound_session() -> VaultAsyncSession:
    return VaultAsyncSession(autobegin=False, autoflush=False, expire_on_commit=False)


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
    session = unbound_session()
    execute = AsyncMock()
    vault_id = uuid4()

    with patch.object(session, "execute", execute):
        async with session.begin():
            returned_id = await set_vault_scope(session, vault_id)

    assert returned_id == vault_id
    execute.assert_awaited_once()
    await_call = execute.await_args
    assert await_call is not None
    statement, parameters = await_call.args
    sql = str(statement)
    assert sql == "SELECT set_config('app.vault_id', :vault_id, true)"
    assert parameters == {"vault_id": str(vault_id)}
    assert "SET LOCAL" not in sql
    assert str(vault_id) not in sql
    await session.close()


async def test_invalid_vault_id_never_reaches_database() -> None:
    session = unbound_session()
    execute = AsyncMock()

    with patch.object(session, "execute", execute):
        async with session.begin():
            with pytest.raises(InvalidVaultId):
                await set_vault_scope(session, "not-a-uuid")

    execute.assert_not_awaited()
    await session.close()


async def test_set_vault_scope_requires_existing_transaction() -> None:
    session = unbound_session()

    with pytest.raises(VaultTransactionRequired):
        await set_vault_scope(session, UUID("12345678-1234-5678-1234-567812345678"))

    await session.close()


async def test_session_queries_fail_closed_without_explicit_scoped_transaction() -> None:
    session = unbound_session()

    with pytest.raises(VaultTransactionRequired):
        await session.execute(text("SELECT 1"))

    async with session.begin():
        with pytest.raises(VaultScopeRequired):
            await session.execute(text("SELECT 1"))

    await session.close()


async def test_raw_connection_access_fails_closed_without_scope() -> None:
    session = unbound_session()

    with pytest.raises(VaultTransactionRequired):
        await session.connection()

    async with session.begin():
        with pytest.raises(VaultScopeRequired):
            await session.connection()

    await session.close()


async def test_run_sync_fails_closed_until_scope_is_bound() -> None:
    session = unbound_session()
    callback_calls = 0

    def callback(_session: Session) -> str:
        nonlocal callback_calls
        callback_calls += 1
        return "scoped"

    with pytest.raises(VaultTransactionRequired):
        await session.run_sync(callback)

    execute = AsyncMock()
    async with session.begin():
        with pytest.raises(VaultScopeRequired):
            await session.run_sync(callback)

        with patch.object(session, "execute", execute):
            await set_vault_scope(session, uuid4())

        assert await session.run_sync(callback) == "scoped"

    assert callback_calls == 1
    await session.close()


async def test_scope_statement_passes_the_real_session_guard() -> None:
    session = unbound_session()

    async with session.begin():
        with pytest.raises(UnboundExecutionError):
            await set_vault_scope(session, uuid4())
        with pytest.raises(VaultScopeRequired):
            require_vault_scope(session)

    await session.close()


async def test_same_transaction_scope_is_idempotent_but_cannot_be_rebound() -> None:
    session = unbound_session()
    execute = AsyncMock()
    first_vault = uuid4()

    with patch.object(session, "execute", execute):
        async with session.begin():
            assert await set_vault_scope(session, first_vault) == first_vault
            assert await set_vault_scope(session, first_vault) == first_vault
            assert require_vault_scope(session) == first_vault
            with pytest.raises(VaultScopeConflict, match="cannot be rebound"):
                await set_vault_scope(session, uuid4())

    execute.assert_awaited_once()
    await session.close()


async def test_failed_scope_setup_does_not_mark_transaction_as_scoped() -> None:
    session = unbound_session()
    execute = AsyncMock(side_effect=RuntimeError("database unavailable"))

    with patch.object(session, "execute", execute):
        async with session.begin():
            with pytest.raises(RuntimeError, match="database unavailable"):
                await set_vault_scope(session, uuid4())
            with pytest.raises(VaultScopeRequired):
                require_vault_scope(session)

    await session.close()


async def test_scope_does_not_carry_into_next_transaction() -> None:
    session = unbound_session()
    execute = AsyncMock()
    vault_id = uuid4()

    with patch.object(session, "execute", execute):
        async with session.begin():
            await set_vault_scope(session, vault_id)

        async with session.begin():
            with pytest.raises(VaultScopeRequired):
                require_vault_scope(session)
            await set_vault_scope(session, vault_id)

    assert execute.await_count == 2
    await session.close()


async def test_session_lifetime_cannot_switch_vaults_between_transactions() -> None:
    session = unbound_session()
    execute = AsyncMock()

    with patch.object(session, "execute", execute):
        async with session.begin():
            await set_vault_scope(session, uuid4())

        async with session.begin():
            with pytest.raises(VaultScopeConflict):
                await set_vault_scope(session, uuid4())

    execute.assert_awaited_once()
    await session.close()


async def test_first_scope_binding_is_rejected_inside_nested_transaction() -> None:
    session = unbound_session()

    async with session.begin(), session.begin_nested():
        with pytest.raises(VaultTransactionRequired):
            await set_vault_scope(session, uuid4())

    await session.close()


async def test_engine_hides_bound_parameters_from_database_errors() -> None:
    settings = Settings(
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
    )
    engine = build_async_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        assert engine.sync_engine.hide_parameters is True
        assert session_factory.kw["autobegin"] is False
        assert session_factory.class_ is VaultAsyncSession
    finally:
        await engine.dispose()
