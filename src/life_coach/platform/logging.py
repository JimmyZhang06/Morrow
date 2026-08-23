"""Structured request logging that never reads request or response bodies."""

from __future__ import annotations

import logging
from contextlib import suppress
from time import perf_counter
from typing import Protocol, cast
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from life_coach.platform.errors import TRACE_HEADER, unexpected_error_handler
from life_coach.platform.settings import LogLevel

HTTP_INTERNAL_SERVER_ERROR = 500

_LOG_LEVEL_VALUES: dict[LogLevel, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class EventLogger(Protocol):
    """Narrow logger contract that keeps the middleware easy to test."""

    def info(self, event: str, **event_fields: object) -> object: ...

    def error(self, event: str, **event_fields: object) -> object: ...


def configure_logging(level: LogLevel) -> None:
    """Configure JSON logs with no exception or payload renderers."""

    numeric_level = _LOG_LEVEL_VALUES[level]
    logging.basicConfig(level=numeric_level, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


class RequestLoggingMiddleware:
    """Log safe request metadata while leaving all payload streams untouched."""

    def __init__(self, app: ASGIApp, logger: EventLogger | None = None) -> None:
        self._app = app
        self._logger = logger or cast(EventLogger, structlog.get_logger("life_coach.http"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started_at = perf_counter()
        trace_id = str(uuid4())
        state = scope.setdefault("state", {})
        state["trace_id"] = trace_id
        status_code = HTTP_INTERNAL_SERVER_ERROR
        response_started = False
        response_completed = False

        async def send_with_trace_id(message: Message) -> None:
            nonlocal response_completed, response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                if TRACE_HEADER not in headers:
                    headers.append(TRACE_HEADER, trace_id)
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_completed = True
            await send(message)

        try:
            await self._app(scope, receive, send_with_trace_id)
        except Exception as exc:
            with suppress(Exception):
                self._write_event(
                    scope=scope,
                    trace_id=trace_id,
                    status_code=HTTP_INTERNAL_SERVER_ERROR,
                    started_at=started_at,
                    error_type=type(exc).__name__,
                )
            with suppress(Exception):
                if not response_started:
                    request = Request(scope, receive=receive)
                    response = await unexpected_error_handler(request, exc)
                    await response(scope, receive, send_with_trace_id)
                elif not response_completed:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        else:
            with suppress(Exception):
                self._write_event(
                    scope=scope,
                    trace_id=trace_id,
                    status_code=status_code,
                    started_at=started_at,
                )

    def _write_event(
        self,
        *,
        scope: Scope,
        trace_id: str,
        status_code: int,
        started_at: float,
        error_type: str | None = None,
    ) -> None:
        method = scope.get("method")
        safe_method = method if isinstance(method, str) else "UNKNOWN"
        route = scope.get("route")
        route_path = getattr(route, "path", None)
        safe_route = route_path if isinstance(route_path, str) else "<unmatched>"
        fields: dict[str, object] = {
            "method": safe_method,
            "route": safe_route,
            "status_code": status_code,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
            "trace_id": trace_id,
        }
        if error_type is None and status_code < HTTP_INTERNAL_SERVER_ERROR:
            self._logger.info("http.request.completed", **fields)
        else:
            if error_type is not None:
                fields["error_type"] = error_type
            self._logger.error("http.request.failed", **fields)
