"""FastAPI application factory and operational health endpoints."""

from __future__ import annotations

from asyncio import timeout
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from life_coach import __version__
from life_coach.platform.database import (
    DatabaseReadinessProbe,
    build_async_engine,
    build_session_factory,
)
from life_coach.platform.errors import ProblemError, install_error_handlers, problem_type
from life_coach.platform.logging import EventLogger, RequestLoggingMiddleware, configure_logging
from life_coach.platform.settings import Settings

type ReadinessProbe = Callable[[], Awaitable[None]]


class HealthResponse(BaseModel):
    """Stable, intentionally minimal health response."""

    model_config = ConfigDict(frozen=True)

    status: Literal["live", "ready"]


def create_app(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    readiness_probe: ReadinessProbe | None = None,
    logger: EventLogger | None = None,
) -> FastAPI:
    """Build an application with injectable infrastructure for isolated tests."""

    app_settings = settings or Settings()
    configure_logging(app_settings.log_level)

    active_engine = engine
    managed_engine: AsyncEngine | None = None
    if readiness_probe is None:
        if active_engine is None:
            active_engine = build_async_engine(app_settings)
            managed_engine = active_engine
        readiness_probe = DatabaseReadinessProbe(active_engine)
    active_readiness_probe = readiness_probe

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if managed_engine is not None:
                await managed_engine.dispose()

    app = FastAPI(
        title="Life Coach Backend",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.engine = active_engine
    app.state.session_factory = (
        build_session_factory(active_engine) if active_engine is not None else None
    )
    install_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware, logger=logger)

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def health_live() -> HealthResponse:
        return HealthResponse(status="live")

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def health_ready() -> HealthResponse:
        try:
            async with timeout(app_settings.readiness_timeout_seconds):
                await active_readiness_probe()
        except Exception:
            raise ProblemError(
                type=problem_type("service-not-ready"),
                title="服务尚未就绪",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                code="SERVICE_NOT_READY",
                safe_detail="依赖服务暂时不可用。",
            ) from None
        return HealthResponse(status="ready")

    return app
