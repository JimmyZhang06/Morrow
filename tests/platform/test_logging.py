"""Tests proving request logging records metadata, never private payloads."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import Request
from pydantic import SecretStr

from life_coach.api.app import create_app
from life_coach.platform.settings import AppEnvironment, Settings


class CaptureLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **event_fields: object) -> object:
        self.events.append({"event": event, **event_fields})
        return None

    def error(self, event: str, **event_fields: object) -> object:
        self.events.append({"event": event, **event_fields})
        return None


class FailingLogger:
    def info(self, event: str, **event_fields: object) -> object:
        raise RuntimeError("logger unavailable")

    def error(self, event: str, **event_fields: object) -> object:
        raise RuntimeError("logger unavailable")


def make_test_settings() -> Settings:
    return Settings(
        env=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+asyncpg://localhost/life_coach_test"),
    )


async def ready_probe() -> None:
    return None


async def test_request_logging_ignores_body_query_and_headers() -> None:
    logger = CaptureLogger()
    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe, logger=logger)

    @app.post("/_test/logging")
    async def consume_body(request: Request) -> dict[str, str]:
        await request.body()
        return {"value": "RESPONSE_SECRET"}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/_test/logging?search=QUERY_SECRET",
            content="BODY_SECRET",
            headers={"Authorization": "Bearer HEADER_SECRET"},
        )

    assert response.status_code == 200
    assert "RESPONSE_SECRET" in response.text
    serialized_events = json.dumps(logger.events)
    assert "BODY_SECRET" not in serialized_events
    assert "RESPONSE_SECRET" not in serialized_events
    assert "QUERY_SECRET" not in serialized_events
    assert "HEADER_SECRET" not in serialized_events
    assert logger.events[-1]["route"] == "/_test/logging"
    assert logging.getLogger("uvicorn.access").disabled is True


async def test_failed_request_logging_ignores_exception_message() -> None:
    logger = CaptureLogger()
    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe, logger=logger)

    @app.get("/_test/logging-error")
    async def fail() -> None:
        raise RuntimeError("EXCEPTION_SECRET")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/logging-error")

    assert response.status_code == 500
    serialized_events = json.dumps(logger.events)
    assert "EXCEPTION_SECRET" not in serialized_events
    assert logger.events[-1]["error_type"] == "RuntimeError"


async def test_exception_boundary_is_fail_closed_when_logger_fails() -> None:
    app = create_app(
        settings=make_test_settings(),
        readiness_probe=ready_probe,
        logger=FailingLogger(),
    )

    @app.get("/_test/failing-logger")
    async def fail() -> None:
        raise RuntimeError("ORIGINAL_PRIVATE_EXCEPTION")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/failing-logger")

    assert response.status_code == 500
    assert "ORIGINAL_PRIVATE_EXCEPTION" not in response.text


async def test_readiness_failure_is_logged_at_error_level() -> None:
    logger = CaptureLogger()

    async def unavailable_probe() -> None:
        raise RuntimeError("DATABASE_PRIVATE_ERROR")

    app = create_app(
        settings=make_test_settings(),
        readiness_probe=unavailable_probe,
        logger=logger,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert logger.events[-1]["event"] == "http.request.failed"
    assert "DATABASE_PRIVATE_ERROR" not in json.dumps(logger.events)
