"""Health and safe problem response contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from pydantic import BaseModel, SecretStr
from starlette.types import ASGIApp

from life_coach.api.app import create_app
from life_coach.application.candidate_insight_command import CandidateInsightRuntime
from life_coach.platform.auth import AuthenticatedPrincipal
from life_coach.platform.errors import TRACE_HEADER
from life_coach.platform.settings import AppEnvironment, Settings


class _TestAuthenticator:
    async def authenticate(self, _access_token: SecretStr) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            principal_id=UUID("12345678-1234-5678-1234-567812345678"),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def make_test_settings() -> Settings:
    return Settings(
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
    )


def make_production_settings() -> Settings:
    return Settings(
        env=AppEnvironment.PRODUCTION,
        database_url=SecretStr("postgresql+asyncpg://db.internal/life_coach?ssl=verify-full"),
        object_store_endpoint="https://objects.internal",
    )


@asynccontextmanager
async def client_for_app(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_liveness_does_not_call_readiness_probe() -> None:
    async def forbidden_probe() -> None:
        raise AssertionError("liveness must not touch the database")

    app = create_app(settings=make_test_settings(), readiness_probe=forbidden_probe)

    async with client_for_app(app) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}
    assert UUID(response.headers[TRACE_HEADER])


async def test_readiness_calls_injected_probe() -> None:
    calls = 0

    async def ready_probe() -> None:
        nonlocal calls
        calls += 1

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    async with client_for_app(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert calls == 1


async def test_capabilities_report_only_mounted_api_surfaces() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    async with client_for_app(app) as client:
        response = await client.get("/health/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "api_version": "v1",
        "server_version": "0.1.0",
        "features": {
            "entries": False,
            "entry_revisions": False,
            "entry_deletion": False,
            "memory_review": False,
            "memory_verdicts": False,
            "candidate_insights": False,
            "actions": False,
            "model_run_receipts": False,
        },
    }


async def test_create_app_mounts_injected_candidate_generation_runtime() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(
        settings=make_test_settings(),
        readiness_probe=ready_probe,
        candidate_insight_runtime=cast(CandidateInsightRuntime, object()),
    )

    async with client_for_app(app) as client:
        response = await client.get("/health/capabilities")

    assert response.status_code == 200
    assert response.json()["features"]["candidate_insights"] is True
    assert "/v1/candidate-insights" in app.openapi()["paths"]


async def test_capabilities_detect_routers_added_by_composition_root() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.get("/v1/entries")
    async def list_entries() -> None:
        return None

    @app.post("/v1/entries")
    async def create_entry() -> None:
        return None

    @app.patch("/v1/entries/{entry_id}")
    async def revise_entry(entry_id: str) -> None:
        return None

    @app.delete("/v1/entries/{entry_id}")
    async def delete_entry(entry_id: str) -> None:
        return None

    @app.get("/v1/memory-inbox")
    async def memory_inbox() -> None:
        return None

    @app.get("/v1/memories/{memory_id}")
    async def memory_detail(memory_id: str) -> None:
        return None

    @app.post("/v1/memories/{memory_id}/verdicts")
    async def submit_verdict(memory_id: str) -> None:
        return None

    @app.post("/v1/candidate-insights")
    async def candidate_insight() -> None:
        return None

    async with client_for_app(app) as client:
        response = await client.get("/health/capabilities")

    assert response.status_code == 200
    assert response.json()["features"] == {
        "entries": True,
        "entry_revisions": True,
        "entry_deletion": True,
        "memory_review": True,
        "memory_verdicts": True,
        "candidate_insights": True,
        "actions": False,
        "model_run_receipts": False,
    }


async def test_injected_readiness_probe_keeps_database_infrastructure() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    try:
        assert app.state.engine is not None
        assert app.state.session_factory is not None
        assert app.state.session_factory.kw["bind"] is app.state.engine
    finally:
        await app.state.engine.dispose()


async def test_production_app_wires_the_authorized_session_factory() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(
        settings=make_production_settings(),
        readiness_probe=ready_probe,
        authenticator=_TestAuthenticator(),
    )
    try:
        assert app.state.production_session_factory is not None
    finally:
        await app.state.engine.dispose()


async def test_development_app_wires_explicit_local_authentication() -> None:
    async def ready_probe() -> None:
        return None

    settings = Settings(
        env=AppEnvironment.DEVELOPMENT,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach"),
        local_auth_enabled=True,
        local_auth_principal_id=UUID("12345678-1234-5678-1234-567812345678"),
        local_auth_token=SecretStr("local-token-with-enough-entropy"),
    )
    app = create_app(settings=settings, readiness_probe=ready_probe)
    try:
        assert app.state.production_session_factory is not None
    finally:
        await app.state.engine.dispose()


def test_production_app_refuses_to_boot_without_authentication() -> None:
    with pytest.raises(ValueError, match="authentication configuration is required"):
        create_app(settings=make_production_settings())


async def test_readiness_failure_is_safe_rfc_7807_problem() -> None:
    leaked_detail = "postgresql://private:SUPERSECRET@db/private-diary"

    async def failing_probe() -> None:
        raise RuntimeError(leaked_detail)

    app = create_app(settings=make_test_settings(), readiness_probe=failing_probe)

    async with client_for_app(app) as client:
        response = await client.get("/health/ready")

    body = response.json()
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body == {
        "type": "https://life-coach.example/problems/service-not-ready",
        "title": "服务尚未就绪",
        "status": 503,
        "code": "SERVICE_NOT_READY",
        "trace_id": response.headers[TRACE_HEADER],
        "safe_detail": "依赖服务暂时不可用。",
    }
    assert "SUPERSECRET" not in response.text
    assert "private-diary" not in response.text
    assert "RuntimeError" not in response.text


async def test_readiness_timeout_returns_same_safe_problem() -> None:
    async def blocked_probe() -> None:
        await asyncio.Event().wait()

    settings = Settings(
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
        readiness_timeout_seconds=0.01,
    )
    app = create_app(settings=settings, readiness_probe=blocked_probe)

    async with client_for_app(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_NOT_READY"


async def test_unexpected_exception_never_reflects_exception_text() -> None:
    leaked_detail = "a private journal sentence that must stay hidden"

    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError(leaked_detail)

    async with client_for_app(app) as client:
        response = await client.get("/_test/boom")

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_SERVER_ERROR"
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert leaked_detail not in response.text
    assert "RuntimeError" not in response.text


async def test_unexpected_exception_is_consumed_by_safe_asgi_boundary() -> None:
    leaked_detail = "private text must never reach the ASGI server logger"

    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.get("/_test/asgi-boundary")
    async def fail_inside_boundary() -> None:
        raise RuntimeError(leaked_detail)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/asgi-boundary")

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_SERVER_ERROR"
    assert leaked_detail not in response.text


async def test_validation_problem_does_not_echo_invalid_input() -> None:
    leaked_input = "private journal content in an invalid field"

    class Payload(BaseModel):
        count: int

    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.post("/_test/validate")
    async def validate_payload(_payload: Payload) -> dict[str, bool]:
        return {"ok": True}

    async with client_for_app(app) as client:
        response = await client.post("/_test/validate", json={"count": leaked_input})

    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert leaked_input not in response.text


async def test_http_exception_detail_is_not_reflected() -> None:
    leaked_detail = "supplier response containing a private quote"

    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.get("/_test/http-error")
    async def fail_with_http_error() -> None:
        raise HTTPException(status_code=409, detail=leaked_detail)

    async with client_for_app(app) as client:
        response = await client.get("/_test/http-error")

    assert response.status_code == 409
    assert response.json()["code"] == "HTTP_409"
    assert leaked_detail not in response.text


async def test_problem_trace_id_ignores_business_state_forgery() -> None:
    from fastapi import Request

    forged_trace_id = "business-forged-trace"

    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.get("/_test/forged-problem-trace")
    async def fail_with_forged_trace(request: Request) -> None:
        request.state.trace_id = forged_trace_id
        raise HTTPException(status_code=409, detail="safe")

    async with client_for_app(app) as client:
        response = await client.get("/_test/forged-problem-trace")

    authoritative_trace_id = response.headers[TRACE_HEADER]
    assert UUID(authoritative_trace_id)
    assert authoritative_trace_id != forged_trace_id
    assert response.json()["trace_id"] == authoritative_trace_id


async def test_framework_404_uses_problem_format_without_echoing_path() -> None:
    leaked_path = "PRIVATE_PATH_SEGMENT"

    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    async with client_for_app(app) as client:
        response = await client.get(f"/missing/{leaked_path}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "NOT_FOUND"
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert leaked_path not in response.text


async def test_method_not_allowed_preserves_sanitized_allow_header() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    async with client_for_app(app) as client:
        response = await client.post("/health/live")

    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert "GET" in response.headers["Allow"]


async def test_authentication_header_is_fixed_and_untrusted_headers_are_dropped() -> None:
    async def ready_probe() -> None:
        return None

    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe)

    @app.get("/_test/auth-error")
    async def fail_authentication() -> None:
        raise HTTPException(
            status_code=401,
            detail="PRIVATE_AUTH_DETAIL",
            headers={
                "WWW-Authenticate": "HEADER_SECRET",
                "X-Untrusted": "SECOND_HEADER_SECRET",
            },
        )

    async with client_for_app(app) as client:
        response = await client.get("/_test/auth-error")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "X-Untrusted" not in response.headers
    assert "PRIVATE_AUTH_DETAIL" not in response.text
    assert "HEADER_SECRET" not in str(response.headers)
