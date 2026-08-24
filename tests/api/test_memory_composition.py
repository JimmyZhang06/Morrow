"""ASGI contracts for authenticated Memory review composition."""

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
import life_coach.api.memory_composition as memory_composition
from life_coach.modules.identity.models import MembershipRole
from life_coach.modules.knowledge.exceptions import RevisionConflictError
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
from tests.knowledge.test_api import FakeMemoryService


class _InjectedAuthenticator:
    async def authenticate(self, _access_token: SecretStr) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            principal_id=uuid4(),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


class _UnusedProtector:
    def seal(self, **_kwargs: object) -> bytes:
        raise AssertionError("the fake Memory service must not seal content")

    def open(self, **_kwargs: object) -> str:
        raise AssertionError("the fake Memory service must not open content")


class _FakeProductionSessions:
    def __init__(self, vault_id: UUID) -> None:
        self.vault_id = vault_id
        self.principal_id = uuid4()
        self.events: list[str] = []
        self.membership_denied = False
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
        self.events.extend(("transaction.enter", "membership.check"))
        if self.membership_denied:
            self.events.append("transaction.rollback")
            raise VaultMembershipDenied("private membership detail")
        authorized = AuthorizedVaultSession(
            context=AuthorizedVaultContext(
                principal_id=self.principal_id,
                vault_id=requested_vault,
                role=MembershipRole.OWNER,
                membership_generation=1,
            ),
            session=cast(VaultAsyncSession, object()),
        )
        self.events.append("transaction.yield")
        try:
            yield authorized
        except BaseException:
            self.events.append("transaction.rollback")
            raise
        else:
            self.events.append("transaction.commit")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
        memory_api_enabled=True,
    )


async def _ready() -> None:
    return None


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions: _FakeProductionSessions,
    service: FakeMemoryService,
) -> FastAPI:
    monkeypatch.setattr(
        app_module,
        "ProductionSessionFactory",
        lambda **_kwargs: sessions,
    )

    def service_factory(_session: object, **_kwargs: object) -> FakeMemoryService:
        sessions.events.append("service.construct")
        return service

    monkeypatch.setattr(memory_composition, "AsyncMemoryService", service_factory)
    return app_module.create_app(
        settings=_settings(),
        readiness_probe=_ready,
        authenticator=_InjectedAuthenticator(),
        source_content_protector=_UnusedProtector(),
    )


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await app.state.engine.dispose()


def _headers(vault_id: UUID, *, authenticated: bool = True) -> dict[str, str]:
    result = {"X-Vault-ID": str(vault_id)}
    if authenticated:
        result["Authorization"] = "Bearer opaque-token"
    return result


async def test_create_app_mounts_memory_review_and_verdict_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    app = _build_app(
        monkeypatch,
        sessions=sessions,
        service=FakeMemoryService(),
    )

    paths = app.openapi()["paths"]
    assert "get" in paths["/v1/memory-inbox"]
    assert "get" in paths["/v1/memories/{memory_id}"]
    assert "post" in paths["/v1/memories/{memory_id}/verdicts"]
    await app.state.engine.dispose()


async def test_memory_request_reuses_one_function_scoped_authorized_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    service = FakeMemoryService()
    app = _build_app(monkeypatch, sessions=sessions, service=service)

    async with _client(app) as client:
        response = await client.get("/v1/memory-inbox", headers=_headers(vault_id))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert sessions.open_calls == 1
    assert sessions.events == [
        "authenticate",
        "transaction.enter",
        "membership.check",
        "transaction.yield",
        "service.construct",
        "transaction.commit",
    ]


async def test_memory_authentication_and_membership_fail_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    app = _build_app(monkeypatch, sessions=sessions, service=FakeMemoryService())

    async with _client(app) as client:
        unauthenticated = await client.get(
            "/v1/memory-inbox",
            headers=_headers(vault_id, authenticated=False),
        )
        sessions.membership_denied = True
        denied = await client.get("/v1/memory-inbox", headers=_headers(vault_id))

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    assert unauthenticated.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert denied.status_code == 404
    assert denied.json()["code"] == "VAULT_UNAVAILABLE"
    assert "private" not in denied.text


async def test_real_app_stale_verdict_uses_safe_problem_contract_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_id = uuid4()
    sessions = _FakeProductionSessions(vault_id)
    service = FakeMemoryService()
    service.failure = RevisionConflictError("private old statement")
    app = _build_app(monkeypatch, sessions=sessions, service=service)

    async with _client(app) as client:
        response = await client.post(
            f"/v1/memories/{service.memory_id}/verdicts",
            headers={**_headers(vault_id), "If-Match": service.etag},
            json={"verdict": "confirm"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "REVISION_CONFLICT"
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert response.headers["cache-control"] == "private, no-store"
    assert "private old statement" not in response.text
    assert sessions.events[-2:] == ["service.construct", "transaction.rollback"]
