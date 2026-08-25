"""FastAPI application factory and operational health endpoints."""

from __future__ import annotations

from asyncio import timeout
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Literal

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from life_coach import __version__
from life_coach.api.action_composition import build_authenticated_action_router
from life_coach.api.candidate_insight_composition import build_candidate_insight_router
from life_coach.api.candidate_runtime import build_candidate_runtime
from life_coach.api.memory_composition import build_authenticated_memory_router
from life_coach.api.source_composition import build_authenticated_sources_router
from life_coach.application.candidate_insight_command import CandidateInsightRuntime
from life_coach.application.source_entries import (
    LocalAesGcmSourceContentProtector,
    SourceContentProtector,
)
from life_coach.platform.auth import (
    AccessTokenAuthenticator,
    DeterministicDevelopmentAuthenticator,
    OidcIntrospectionAuthenticator,
    ProductionSessionFactory,
)
from life_coach.platform.database import (
    DatabaseReadinessProbe,
    build_async_engine,
    build_session_factory,
)
from life_coach.platform.errors import ProblemError, install_error_handlers, problem_type
from life_coach.platform.logging import EventLogger, RequestLoggingMiddleware, configure_logging
from life_coach.platform.settings import AppEnvironment, Settings

type ReadinessProbe = Callable[[], Awaitable[None]]


class HealthResponse(BaseModel):
    """Stable, intentionally minimal health response."""

    model_config = ConfigDict(frozen=True)

    status: Literal["live", "ready"]


class FeatureCapabilities(BaseModel):
    """User-facing API surfaces composed into this running application."""

    model_config = ConfigDict(frozen=True)

    entries: bool
    entry_revisions: bool
    entry_deletion: bool
    memory_review: bool
    memory_verdicts: bool
    candidate_insights: bool
    actions: bool
    model_run_receipts: bool


class CapabilitiesResponse(BaseModel):
    """Stable discovery response for first-party clients."""

    model_config = ConfigDict(frozen=True)

    api_version: Literal["v1"] = "v1"
    server_version: str
    features: FeatureCapabilities


def create_app(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    readiness_probe: ReadinessProbe | None = None,
    logger: EventLogger | None = None,
    authenticator: AccessTokenAuthenticator | None = None,
    source_content_protector: SourceContentProtector | None = None,
    candidate_insight_runtime: CandidateInsightRuntime | None = None,
) -> FastAPI:
    """Build an application with injectable infrastructure for isolated tests."""

    app_settings = settings or Settings()
    configure_logging(app_settings.log_level)

    active_authenticator = authenticator
    auth_values = (
        app_settings.auth_introspection_url,
        app_settings.auth_issuer,
        app_settings.auth_audience,
        app_settings.auth_client_id,
        app_settings.auth_client_secret,
    )
    if any(value is not None for value in auth_values) and not all(
        value is not None for value in auth_values
    ):
        raise ValueError("authentication configuration must be complete")
    if active_authenticator is None and app_settings.local_auth_enabled:
        if app_settings.env is AppEnvironment.PRODUCTION:  # defence in depth
            raise ValueError("production must not enable local authentication")
        assert app_settings.local_auth_principal_id is not None
        assert app_settings.local_auth_token is not None
        active_authenticator = DeterministicDevelopmentAuthenticator(
            principal_id=app_settings.local_auth_principal_id,
            access_token=app_settings.local_auth_token,
        )
    authentication_will_be_available = active_authenticator is not None or all(
        value is not None for value in auth_values
    )
    protected_api_enabled = (
        app_settings.source_api_enabled
        or app_settings.memory_api_enabled
        or app_settings.model_provider != "disabled"
    )
    if app_settings.env is AppEnvironment.PRODUCTION and not authentication_will_be_available:
        raise ValueError("production authentication configuration is required")
    if protected_api_enabled and not authentication_will_be_available:
        raise ValueError("protected API authentication configuration is required")

    active_source_protector = source_content_protector
    if protected_api_enabled and active_source_protector is None:
        if app_settings.env is AppEnvironment.PRODUCTION:
            raise ValueError("production protected APIs require a managed content protector")
        if app_settings.local_source_content_key is None:
            raise ValueError("protected APIs require a Source content protector")
        active_source_protector = LocalAesGcmSourceContentProtector(
            app_settings.local_source_content_key.get_secret_value().encode("utf-8")
        )

    active_engine = engine
    managed_engine: AsyncEngine | None = None
    if active_engine is None:
        active_engine = build_async_engine(app_settings)
        managed_engine = active_engine
    active_readiness_probe = (
        readiness_probe if readiness_probe is not None else DatabaseReadinessProbe(active_engine)
    )
    session_factory = build_session_factory(active_engine)

    managed_auth_client: httpx.AsyncClient | None = None
    if active_authenticator is None and all(value is not None for value in auth_values):
        assert app_settings.auth_introspection_url is not None
        assert app_settings.auth_issuer is not None
        assert app_settings.auth_audience is not None
        assert app_settings.auth_client_id is not None
        assert app_settings.auth_client_secret is not None
        managed_auth_client = httpx.AsyncClient(timeout=app_settings.auth_timeout_seconds)
        active_authenticator = OidcIntrospectionAuthenticator(
            http_client=managed_auth_client,
            endpoint=str(app_settings.auth_introspection_url),
            client_id=app_settings.auth_client_id,
            client_secret=app_settings.auth_client_secret,
            expected_issuer=app_settings.auth_issuer,
            expected_audience=app_settings.auth_audience,
        )
    if active_authenticator is None:  # pragma: no cover - guarded by configuration checks
        assert not protected_api_enabled

    production_sessions = (
        ProductionSessionFactory(
            session_factory=session_factory,
            authenticator=active_authenticator,
        )
        if active_authenticator is not None
        else None
    )
    candidate_composition = None
    active_candidate_runtime = candidate_insight_runtime
    if active_candidate_runtime is None and app_settings.model_provider != "disabled":
        if production_sessions is None or active_source_protector is None:
            raise ValueError("candidate runtime requires protected authenticated APIs")
        candidate_composition = build_candidate_runtime(
            settings=app_settings,
            sessions=production_sessions,
            protector=active_source_protector,
        )
        if candidate_composition is None:  # pragma: no cover - guarded by provider flag
            raise ValueError("candidate runtime composition is unavailable")
        active_candidate_runtime = candidate_composition.runtime

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if managed_engine is not None:
                await managed_engine.dispose()
            if managed_auth_client is not None:
                await managed_auth_client.aclose()
            if candidate_composition is not None and candidate_composition.http_client is not None:
                candidate_composition.http_client.close()

    app = FastAPI(
        title="Life Coach Backend",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.engine = active_engine
    app.state.session_factory = session_factory
    app.state.production_session_factory = production_sessions
    install_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware, logger=logger)

    if app_settings.source_api_enabled:
        assert production_sessions is not None
        assert active_source_protector is not None
        assert app_settings.source_api_hmac_key is not None
        app.include_router(
            build_authenticated_sources_router(
                sessions=production_sessions,
                protector=active_source_protector,
                hmac_key=app_settings.source_api_hmac_key.get_secret_value().encode("utf-8"),
            )
        )

    if app_settings.memory_api_enabled:
        assert production_sessions is not None
        assert active_source_protector is not None
        app.include_router(
            build_authenticated_memory_router(
                sessions=production_sessions,
                protector=active_source_protector,
            )
        )
        app.include_router(build_authenticated_action_router(sessions=production_sessions))

    if active_candidate_runtime is not None:
        app.include_router(
            build_candidate_insight_router(
                runtime=active_candidate_runtime,
                sessions=production_sessions,
            )
        )

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

    @app.get(
        "/health/capabilities",
        response_model=CapabilitiesResponse,
        tags=["health"],
    )
    async def health_capabilities() -> CapabilitiesResponse:
        """Report only API surfaces actually mounted in this process."""

        # FastAPI 0.141 keeps included routers behind a lazy ``_IncludedRouter``
        # wrapper, so scanning ``app.routes`` no longer reveals their leaf paths.
        # OpenAPI is the stable, fully-expanded description of the mounted public
        # surface and deliberately excludes implementation-only dependencies.
        paths = app.openapi().get("paths", {})

        def has_route(path: str, method: str) -> bool:
            operations = paths.get(path, {})
            return isinstance(operations, dict) and method.lower() in operations

        def has_prefix(prefix: str) -> bool:
            return any(str(path).startswith(prefix) for path in paths)

        return CapabilitiesResponse(
            server_version=__version__,
            features=FeatureCapabilities(
                entries=has_route("/v1/entries", "GET") and has_route("/v1/entries", "POST"),
                entry_revisions=has_route("/v1/entries/{entry_id}", "PATCH"),
                entry_deletion=has_route("/v1/entries/{entry_id}", "DELETE"),
                memory_review=has_route("/v1/memories", "GET")
                and has_route("/v1/memories/{memory_id}", "GET"),
                memory_verdicts=has_route("/v1/memories/{memory_id}/verdicts", "POST"),
                candidate_insights=has_route("/v1/candidate-insights", "POST"),
                actions=has_prefix("/v1/actions"),
                model_run_receipts=has_prefix("/v1/model-runs"),
            ),
        )

    return app
