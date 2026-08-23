"""Tests proving request logging records metadata, never private payloads."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import httpx
from fastapi import Request
from pydantic import SecretStr
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Message, Scope

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


async def test_trace_header_overrides_business_forgery_and_matches_log() -> None:
    logger = CaptureLogger()
    forged_trace_id = "business-forged-trace"
    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe, logger=logger)

    @app.get("/_test/forged-trace")
    async def forged_trace(request: Request) -> JSONResponse:
        request.state.trace_id = forged_trace_id
        return JSONResponse(
            {"ok": True},
            headers={"X-Trace-ID": forged_trace_id},
        )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/forged-trace")

    authoritative_trace_id = response.headers["X-Trace-ID"]
    assert UUID(authoritative_trace_id)
    assert authoritative_trace_id != forged_trace_id
    assert logger.events[-1]["trace_id"] == authoritative_trace_id


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


async def test_stream_failure_never_emits_clean_end_of_body() -> None:
    logger = CaptureLogger()
    app = create_app(settings=make_test_settings(), readiness_probe=ready_probe, logger=logger)

    async def failing_stream() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("STREAM_PRIVATE_ERROR")

    @app.get("/_test/failing-stream")
    async def stream_response() -> StreamingResponse:
        return StreamingResponse(failing_stream(), media_type="text/plain")

    scope = cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "method": "GET",
            "root_path": "",
            "path": "/_test/failing-stream",
            "raw_path": b"/_test/failing-stream",
            "query_string": b"",
            "headers": [],
            "state": {},
        },
    )
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)

    body_messages = [message for message in messages if message["type"] == "http.response.body"]
    assert any(
        message.get("body") == b"partial" and message.get("more_body") is True
        for message in body_messages
    )
    assert all(message.get("more_body", False) is True for message in body_messages)
    assert "STREAM_PRIVATE_ERROR" not in json.dumps(logger.events)
