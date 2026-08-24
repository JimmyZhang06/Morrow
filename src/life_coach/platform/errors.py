"""Safe RFC 7807-style API problem responses."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

PROBLEM_BASE_URL = "https://life-coach.example/problems"
TRACE_HEADER = "X-Trace-ID"
TRACE_SCOPE_KEY = "life_coach.platform.trace_id"
_STANDARD_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)


class ProblemDetails(BaseModel):
    """Public error fields; no raw exception or request data is permitted."""

    model_config = ConfigDict(frozen=True)

    type: str
    title: str
    status: int
    code: str
    trace_id: str
    safe_detail: str
    current_revision: int | None = None


class ProblemError(Exception):
    """An expected API failure containing only explicitly safe client text."""

    def __init__(
        self,
        *,
        type: str,
        title: str,
        status: int,
        code: str,
        safe_detail: str,
        current_revision: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.type = type
        self.title = title
        self.status = status
        self.code = code
        self.safe_detail = safe_detail
        self.current_revision = current_revision
        self.headers = dict(headers or {})


def problem_type(slug: str) -> str:
    """Build a stable problem type URL from an internal constant slug."""

    return f"{PROBLEM_BASE_URL}/{slug}"


def _request_trace_id(request: Request) -> str:
    trace_id = request.scope.get(TRACE_SCOPE_KEY)
    if isinstance(trace_id, str):
        request.state.trace_id = trace_id
        return trace_id

    trace_id = str(uuid4())
    request.scope[TRACE_SCOPE_KEY] = trace_id
    request.state.trace_id = trace_id
    return trace_id


def _problem_response(
    request: Request,
    *,
    type: str,
    title: str,
    status: int,
    code: str,
    safe_detail: str,
    current_revision: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    trace_id = _request_trace_id(request)
    problem = ProblemDetails(
        type=type,
        title=title,
        status=status,
        code=code,
        trace_id=trace_id,
        safe_detail=safe_detail,
        current_revision=current_revision,
    )
    response_headers = dict(headers or {})
    response_headers[TRACE_HEADER] = trace_id
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
        headers=response_headers,
    )


def _safe_http_headers(
    status: int,
    headers: Mapping[str, str] | None,
) -> dict[str, str]:
    """Preserve protocol semantics without reflecting arbitrary header values."""

    source_headers = {name.lower(): value for name, value in (headers or {}).items()}
    safe_headers: dict[str, str] = {}
    if status == HTTPStatus.METHOD_NOT_ALLOWED:
        methods = [method.strip().upper() for method in source_headers.get("allow", "").split(",")]
        if methods and all(method in _STANDARD_HTTP_METHODS for method in methods):
            safe_headers["Allow"] = ", ".join(methods)
    elif status == HTTPStatus.UNAUTHORIZED:
        safe_headers["WWW-Authenticate"] = "Bearer"
    elif status == HTTPStatus.TOO_MANY_REQUESTS:
        retry_after = source_headers.get("retry-after", "")
        if retry_after.isascii() and retry_after.isdigit():
            safe_headers["Retry-After"] = str(min(int(retry_after), 86_400))
    return safe_headers


async def problem_error_handler(request: Request, exc: Exception) -> Response:
    """Render an expected problem without inspecting an exception cause."""

    if not isinstance(exc, ProblemError):
        return await unexpected_error_handler(request, exc)
    return _problem_response(
        request,
        type=exc.type,
        title=exc.title,
        status=exc.status,
        code=exc.code,
        safe_detail=exc.safe_detail,
        current_revision=exc.current_revision,
        headers=_safe_http_headers(exc.status, exc.headers),
    )


async def validation_error_handler(
    request: Request,
    _exc: Exception,
) -> Response:
    """Hide validation inputs and contexts, which may contain private text."""

    return _problem_response(
        request,
        type=problem_type("request-validation"),
        title="请求格式无效",
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="REQUEST_VALIDATION_ERROR",
        safe_detail="请检查请求字段后重试。",
    )


async def http_error_handler(request: Request, exc: Exception) -> Response:
    """Map HTTP failures without reflecting FastAPI's potentially unsafe detail."""

    if not isinstance(exc, HTTPException):
        return await unexpected_error_handler(request, exc)
    status = exc.status_code
    if status == HTTPStatus.NOT_FOUND:
        title = "资源不存在"
        code = "NOT_FOUND"
        safe_detail = "请求的资源不存在。"
        slug = "not-found"
    elif status == HTTPStatus.METHOD_NOT_ALLOWED:
        title = "请求方法不受支持"
        code = "METHOD_NOT_ALLOWED"
        safe_detail = "该资源不支持此请求方法。"
        slug = "method-not-allowed"
    else:
        title = "请求未完成"
        code = f"HTTP_{status}"
        safe_detail = "请求无法完成。"
        slug = "http-error"

    return _problem_response(
        request,
        type=problem_type(slug),
        title=title,
        status=status,
        code=code,
        safe_detail=safe_detail,
        headers=_safe_http_headers(status, exc.headers),
    )


async def unexpected_error_handler(request: Request, _exc: Exception) -> Response:
    """Return a stable generic response without serializing exception details."""

    return _problem_response(
        request,
        type=problem_type("internal-server-error"),
        title="服务内部错误",
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="INTERNAL_SERVER_ERROR",
        safe_detail="服务暂时无法完成请求。",
    )


def install_error_handlers(app: FastAPI) -> None:
    """Install handlers for expected HTTP failures.

    Unexpected exceptions are terminated by ``RequestLoggingMiddleware`` inside
    Starlette's re-raising server error layer, preventing unsafe server traceback
    logging after a response has been sent.
    """

    app.add_exception_handler(ProblemError, problem_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
