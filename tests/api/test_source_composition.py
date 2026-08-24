"""ASGI contract tests for the authenticated Source composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

import life_coach.api.app as app_module
import life_coach.api.source_composition as source_composition
from life_coach.application.source_entries import (
    AppendEntryRevisionCommand,
    CreateEntryCommand,
)
from life_coach.modules.identity.models import MembershipRole
from life_coach.modules.sources.exceptions import RevisionConflict
from life_coach.platform.auth import (
    AuthenticatedPrincipal,
    AuthenticationDenied,
    AuthorizedVaultContext,
    AuthorizedVaultSession,
    VaultMembershipDenied,
)
from life_coach.platform.database import VaultAsyncSession
from life_coach.platform.errors import TRACE_HEADER
from life_coach.platform.settings import AppEnvironment, Settings

_HMAC_KEY = "source-api-test-hmac-key-material-at-least-32-bytes"


class _InjectedAuthenticator:
    async def authenticate(self, _access_token: SecretStr) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            principal_id=uuid4(),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


class _UnusedProtector:
    """The fake Source service keeps these tests at the HTTP composition boundary."""

    def seal(self, **_kwargs: object) -> bytes:
        raise AssertionError("the fake Source service must not seal content")

    def open(self, **_kwargs: object) -> str:
        raise AssertionError("the fake Source service must not open content")


class _FakeSourceService:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.entry_id = uuid4()
        self.revision_id = uuid4()
        self.raise_revision_conflict = False

    async def create_entry(
        self,
        *,
        vault_id: UUID,
        command: CreateEntryCommand,
        idempotency_key: str,
    ) -> dict[str, object]:
        del vault_id, command, idempotency_key
        self.events.append("service.create")
        return {
            "id": self.entry_id,
            "revision": 1,
            "saved": True,
            "processing": {"state": "pending"},
        }

    async def list_entries(self, **_kwargs: object) -> dict[str, object]:
        self.events.append("service.list")
        return {"items": [], "next_cursor": None}

    async def get_entry(self, **_kwargs: object) -> object:
        raise AssertionError("not exercised by these composition tests")

    async def append_entry_revision(
        self,
        *,
        vault_id: UUID,
        entry_id: UUID,
        command: AppendEntryRevisionCommand,
        idempotency_key: str,
    ) -> dict[str, object]:
        del vault_id, entry_id, command, idempotency_key
        self.events.append("service.append")
        if self.raise_revision_conflict:
            raise RevisionConflict(expected_revision=1, current_revision=3)
        return {
            "id": self.entry_id,
            "revision": 2,
            "saved": True,
            "processing": {"state": "pending"},
        }

    async def delete_entry(self, **_kwargs: object) -> object:
        raise AssertionError("not exercised by these composition tests")


class _FakeProductionSessions:
    """Record the authorization and transaction lifecycle around one request."""

    def __init__(self, vault_id: UUID) -> None:
        self.vault_id = vault_id
        self.principal_id = uuid4()
        self.events: list[str] = []
        self.membership_denied = False
        self.commit_failure = False
        self.open_calls = 0

    @asynccontextmanager
    async def open(
        self,
        *,
        authorization: str | None,
        vault_id: UUID | str,
    ) -> AsyncIterator[AuthorizedVaultSession]:
        self.open_calls += 1
        self.events.append("authenticate")
        if authorization is None:
            raise AuthenticationDenied("private authentication detail")

        requested_vault = UUID(str(vault_id))
        self.events.append("transaction.enter")
        self.events.append("membership.check")
        if self.membership_denied:
            self.events.append("transaction.rollback")
            raise VaultMembershipDenied("private membership detail")

        context = AuthorizedVaultContext(
            principal_id=self.principal_id,
            vault_id=requested_vault,
            role=MembershipRole.OWNER,
            membership_generation=1,
        )
        authorized = AuthorizedVaultSession(
            context=context,
            session=cast(VaultAsyncSession, object()),
        )
        self.events.append("transaction.yield")
        try:
            yield authorized
        except BaseException:
            self.events.append("transaction.rollback")
            raise
        else:
            self.events.append("transaction.commit.attempt")
            if self.commit_failure:
                self.events.append("transaction.rollback")
                raise RuntimeError("private commit failure")
            self.events.append("transaction.commit")


def _source_settings() -> Settings:
    return Settings(
        _env_file=None,
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
        source_api_enabled=True,
        source_api_hmac_key=SecretStr(_HMAC_KEY),
    )


async def _ready() -> None:
    return None


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: _FakeProductionSessions,
    service: _FakeSourceService,
) -> FastAPI:
    constructed_sessions: list[object] = []

    def session_factory(**_kwargs: object) -> _FakeProductionSessions:
        constructed_sessions.append(sessions)
        return sessions

    def service_factory(**kwargs: object) -> _FakeSourceService:
        assert kwargs["vault_id"] == sessions.vault_id
        assert kwargs["session"] is not None
        sessions.events.append("service.construct")
        return service

    monkeypatch.setattr(app_module, "ProductionSessionFactory", session_factory)
    monkeypatch.setattr(
        source_composition,
        "PostgresSourceEntryService",
        service_factory,
    )
    app = app_module.create_app(
        settings=_source_settings(),
        readiness_probe=_ready,
        authenticator=_InjectedAuthenticator(),
        source_content_protector=_UnusedProtector(),
    )
    assert constructed_sessions == [sessions]
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await app.state.engine.dispose()


def _create_headers(vault_id: UUID, *, authenticated: bool = True) -> dict[str, str]:
    headers = {
        "X-Vault-ID": str(vault_id),
        "Idempotency-Key": "entry-create-1",
    }
    if authenticated:
        headers["Authorization"] = "Bearer opaque-token"
    return headers


def _create_payload() -> dict[str, str]:
    return {
        "content": "今天我发现自己更愿意承担一个小挑战。",
        "client_id": "client-entry-1",
    }


async def test_create_app_mounts_the_authenticated_source_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    app = _build_app(
        monkeypatch,
        sessions=sessions,
        service=_FakeSourceService(sessions.events),
    )

    paths = app.openapi()["paths"]

    assert {"get", "post"} <= paths["/v1/entries"].keys()
    assert {"get", "patch", "delete"} <= paths["/v1/entries/{entry_id}"].keys()
    await app.state.engine.dispose()


async def test_missing_bearer_returns_safe_401_without_opening_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    app = _build_app(
        monkeypatch,
        sessions=sessions,
        service=_FakeSourceService(sessions.events),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/entries",
            headers=_create_headers(vault_id, authenticated=False),
            json=_create_payload(),
        )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert sessions.open_calls == 1
    assert sessions.events == ["authenticate"]


async def test_membership_denial_returns_non_enumerating_404_and_never_builds_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    sessions.membership_denied = True
    app = _build_app(
        monkeypatch,
        sessions=sessions,
        service=_FakeSourceService(sessions.events),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/entries",
            headers=_create_headers(vault_id),
            json=_create_payload(),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "VAULT_UNAVAILABLE"
    assert sessions.events == [
        "authenticate",
        "transaction.enter",
        "membership.check",
        "transaction.rollback",
    ]


async def test_one_request_reuses_one_authorized_transaction_for_both_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    app = _build_app(
        monkeypatch,
        sessions=sessions,
        service=_FakeSourceService(sessions.events),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/entries",
            headers=_create_headers(vault_id),
            json=_create_payload(),
        )

    assert response.status_code == 201
    assert sessions.open_calls == 1
    assert sessions.events == [
        "authenticate",
        "transaction.enter",
        "membership.check",
        "transaction.yield",
        "service.construct",
        "service.create",
        "transaction.commit.attempt",
        "transaction.commit",
    ]


async def test_function_scoped_dependency_turns_commit_failure_into_500_not_201(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    sessions.commit_failure = True
    app = _build_app(
        monkeypatch,
        sessions=sessions,
        service=_FakeSourceService(sessions.events),
    )

    async with _client(app) as client:
        response = await client.post(
            "/v1/entries",
            headers=_create_headers(vault_id),
            json=_create_payload(),
        )

    # With FastAPI's default request-scoped yield cleanup, the 201 response would
    # already have started before this commit exception. This proves the explicit
    # function scope closes the transaction before any success response is sent.
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_SERVER_ERROR"
    assert sessions.events[-3:] == [
        "service.create",
        "transaction.commit.attempt",
        "transaction.rollback",
    ]


async def test_real_app_revision_conflict_preserves_safe_current_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    service = _FakeSourceService(sessions.events)
    service.raise_revision_conflict = True
    app = _build_app(monkeypatch, sessions=sessions, service=service)

    async with _client(app) as client:
        response = await client.patch(
            f"/v1/entries/{service.entry_id}",
            headers={
                "Authorization": "Bearer opaque-token",
                "X-Vault-ID": str(vault_id),
                "Idempotency-Key": "entry-append-1",
                "If-Match": '"1"',
            },
            json={"content": "这是新的版本"},
        )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "REVISION_CONFLICT"
    assert response.json()["current_revision"] == 3
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert sessions.events[-2:] == ["service.append", "transaction.rollback"]
