"""Async database construction and transaction-local vault isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Concatenate, ParamSpec, TypeVar
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import CoreExecuteOptionsParameter
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction, SessionTransactionOrigin
from sqlalchemy.orm.session import _BindArguments

from life_coach.platform.settings import Settings

_SET_VAULT_SCOPE = text("SELECT set_config('app.vault_id', :vault_id, true)")
_READINESS_QUERY = text("SELECT 1")
_VAULT_BINDING_KEY = object()
_SESSION_VAULT_KEY = object()
_SCOPE_SETUP_KEY = object()
_SCOPE_SETUP_SENTINEL = object()
_P = ParamSpec("_P")
_T = TypeVar("_T")


class InvalidVaultId(ValueError):
    """Raised when a value cannot safely identify a vault."""


class VaultTransactionRequired(RuntimeError):
    """Raised when transaction-local scope is requested outside a transaction."""


class VaultScopeRequired(RuntimeError):
    """Raised when database work is attempted before binding a vault."""


class VaultScopeConflict(RuntimeError):
    """Raised when code attempts to rebind a session or transaction."""


@dataclass(frozen=True, slots=True)
class _VaultBinding:
    transaction: SessionTransaction
    vault_id: UUID


class VaultSyncSession(Session):
    """Synchronous session guarded by transaction-local vault state."""

    def connection(
        self,
        bind_arguments: _BindArguments | None = None,
        execution_options: CoreExecuteOptionsParameter | None = None,
    ) -> Connection:
        """Prevent raw connection access before transaction scope is bound."""

        setup_is_active = self.info.get(_SCOPE_SETUP_KEY) is _SCOPE_SETUP_SENTINEL
        if not setup_is_active:
            _require_sync_vault_scope(self)
        return super().connection(bind_arguments, execution_options)


class VaultAsyncSession(AsyncSession):
    """Async facade whose ORM work is fail-closed without a vault scope."""

    sync_session_class = VaultSyncSession

    async def connection(
        self,
        bind_arguments: _BindArguments | None = None,
        execution_options: CoreExecuteOptionsParameter | None = None,
        **kw: Any,
    ) -> AsyncConnection:
        """Prevent async raw connection access before vault scope is bound."""

        require_vault_scope(self)
        return await super().connection(bind_arguments, execution_options, **kw)

    async def run_sync(
        self,
        fn: Callable[Concatenate[Session, _P], _T],
        *arg: _P.args,
        **kw: _P.kwargs,
    ) -> _T:
        """Run synchronous session work only after vault scope is bound."""

        require_vault_scope(self)
        return await super().run_sync(fn, *arg, **kw)


type AsyncSessionFactory = async_sessionmaker[VaultAsyncSession]


def _explicit_root_transaction(session: Session) -> SessionTransaction:
    transaction = session.get_transaction()
    if (
        transaction is None
        or not transaction.is_active
        or transaction.origin is not SessionTransactionOrigin.BEGIN
    ):
        raise VaultTransactionRequired("vault scope requires an explicit active transaction")
    return transaction


def _binding_for_transaction(
    session: Session,
    transaction: SessionTransaction,
) -> _VaultBinding | None:
    binding = session.info.get(_VAULT_BINDING_KEY)
    if isinstance(binding, _VaultBinding) and binding.transaction is transaction:
        return binding
    return None


def _require_sync_vault_scope(session: Session) -> UUID:
    transaction = _explicit_root_transaction(session)
    binding = _binding_for_transaction(session, transaction)
    if binding is None:
        raise VaultScopeRequired("database work requires a bound vault scope")
    return binding.vault_id


def require_vault_scope(session: VaultAsyncSession) -> UUID:
    """Return the current vault or fail before unscoped database work."""

    return _require_sync_vault_scope(session.sync_session)


def _guard_orm_execute(state: ORMExecuteState) -> None:
    setup_is_active = (
        state.session.info.get(_SCOPE_SETUP_KEY) is _SCOPE_SETUP_SENTINEL
        and state.statement is _SET_VAULT_SCOPE
    )
    if not setup_is_active:
        _require_sync_vault_scope(state.session)


def _guard_before_flush(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    _require_sync_vault_scope(session)


event.listen(VaultSyncSession, "do_orm_execute", _guard_orm_execute)
event.listen(VaultSyncSession, "before_flush", _guard_before_flush)


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
        class_=VaultAsyncSession,
        autobegin=False,
        autoflush=False,
        expire_on_commit=False,
    )


async def set_vault_scope(session: VaultAsyncSession, vault_id: UUID | str) -> UUID:
    """Apply the vault GUC to the current transaction only.

    Callers must already have entered ``session.begin()``. The literal third
    argument to ``set_config`` is ``true``, PostgreSQL's transaction-local mode;
    session-level ``SET`` is never used.
    """

    normalized_vault_id = validate_vault_id(vault_id)
    sync_session = session.sync_session
    transaction = _explicit_root_transaction(sync_session)
    binding = _binding_for_transaction(sync_session, transaction)
    if binding is not None:
        if binding.vault_id != normalized_vault_id:
            raise VaultScopeConflict("vault scope cannot be rebound")
        return normalized_vault_id

    if sync_session.get_nested_transaction() is not None:
        raise VaultTransactionRequired("vault scope must be set before a nested transaction")

    session_vault = sync_session.info.get(_SESSION_VAULT_KEY)
    if isinstance(session_vault, UUID) and session_vault != normalized_vault_id:
        raise VaultScopeConflict("session cannot be rebound to another vault")

    sync_session.info[_SCOPE_SETUP_KEY] = _SCOPE_SETUP_SENTINEL
    try:
        await session.execute(_SET_VAULT_SCOPE, {"vault_id": str(normalized_vault_id)})
    finally:
        sync_session.info.pop(_SCOPE_SETUP_KEY, None)

    if _explicit_root_transaction(sync_session) is not transaction:
        raise VaultTransactionRequired("vault transaction ended while binding scope")
    sync_session.info[_VAULT_BINDING_KEY] = _VaultBinding(transaction, normalized_vault_id)
    sync_session.info[_SESSION_VAULT_KEY] = normalized_vault_id
    return normalized_vault_id


@asynccontextmanager
async def vault_transaction(
    session_factory: AsyncSessionFactory,
    vault_id: UUID | str,
) -> AsyncIterator[VaultAsyncSession]:
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
