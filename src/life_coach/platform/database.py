"""Async database construction and transaction-local vault isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from life_coach.platform.settings import Settings

type AsyncSessionFactory = async_sessionmaker[AsyncSession]

_SET_VAULT_SCOPE = text("SELECT set_config('app.vault_id', :vault_id, true)")
_READINESS_QUERY = text("SELECT 1")


class InvalidVaultId(ValueError):
    """Raised when a value cannot safely identify a vault."""


class VaultTransactionRequired(RuntimeError):
    """Raised when transaction-local scope is requested outside a transaction."""


def validate_vault_id(value: UUID | str) -> UUID:
    """Return a canonical UUID without including invalid input in error text."""

    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise InvalidVaultId("vault_id must be a valid UUID") from None


def build_async_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide SQLAlchemy async engine."""

    return create_async_engine(
        settings.database_dsn,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def build_session_factory(engine: AsyncEngine) -> AsyncSessionFactory:
    """Create sessions with explicit transaction and expiration semantics."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )


async def set_vault_scope(session: AsyncSession, vault_id: UUID | str) -> UUID:
    """Apply the vault GUC to the current transaction only.

    Callers must already have entered ``session.begin()``. The literal third
    argument to ``set_config`` is ``true``, PostgreSQL's transaction-local mode;
    session-level ``SET`` is never used.
    """

    normalized_vault_id = validate_vault_id(vault_id)
    if not session.in_transaction():
        raise VaultTransactionRequired("vault scope requires an active transaction")

    await session.execute(_SET_VAULT_SCOPE, {"vault_id": str(normalized_vault_id)})
    return normalized_vault_id


@asynccontextmanager
async def vault_transaction(
    session_factory: AsyncSessionFactory,
    vault_id: UUID | str,
) -> AsyncIterator[AsyncSession]:
    """Yield a session whose active transaction is scoped to one validated vault."""

    normalized_vault_id = validate_vault_id(vault_id)
    async with session_factory() as session, session.begin():
        await set_vault_scope(session, normalized_vault_id)
        yield session


class DatabaseReadinessProbe:
    """Small callable adapter used by the readiness endpoint."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def __call__(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(_READINESS_QUERY)
