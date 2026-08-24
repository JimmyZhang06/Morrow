"""Production composition for authenticated, Vault-scoped Source entry routes."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from life_coach.api.routers.sources import create_sources_router
from life_coach.application.source_entries import (
    PostgresSourceEntryService,
    SourceContentProtector,
    SourceEntryService,
)
from life_coach.platform.auth import (
    AuthenticationDenied,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.errors import ProblemError, problem_type


@dataclass(frozen=True, slots=True)
class AuthorizedSourceRequest:
    vault_id: uuid.UUID
    service: SourceEntryService


def build_authenticated_sources_router(
    *,
    sessions: ProductionSessionFactory,
    protector: SourceContentProtector,
    hmac_key: bytes,
) -> APIRouter:
    """Bind one request to one authenticated membership and one committing transaction."""

    async def open_authorized_source(
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AsyncIterator[AuthorizedSourceRequest]:
        try:
            async with sessions.open(
                authorization=authorization,
                vault_id=vault_id,
            ) as authorized:
                yield AuthorizedSourceRequest(
                    vault_id=authorized.context.vault_id,
                    service=PostgresSourceEntryService(
                        session=authorized.session,
                        vault_id=authorized.context.vault_id,
                        protector=protector,
                        hmac_key=hmac_key,
                    ),
                )
        except AuthenticationDenied:
            raise ProblemError(
                type=problem_type("authentication-required"),
                title="需要登录",
                status=HTTPStatus.UNAUTHORIZED,
                code="AUTHENTICATION_REQUIRED",
                safe_detail="请登录后重试。",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        except VaultMembershipDenied:
            raise ProblemError(
                type=problem_type("vault-unavailable"),
                title="空间不可用",
                status=HTTPStatus.NOT_FOUND,
                code="VAULT_UNAVAILABLE",
                safe_detail="请求的空间不可用。",
            ) from None

    def get_vault_id(
        context: Annotated[
            AuthorizedSourceRequest,
            Depends(open_authorized_source, scope="function"),
        ],
    ) -> uuid.UUID:
        return context.vault_id

    def get_source_service(
        context: Annotated[
            AuthorizedSourceRequest,
            Depends(open_authorized_source, scope="function"),
        ],
    ) -> SourceEntryService:
        return context.service

    return create_sources_router(
        get_vault_id=get_vault_id,
        get_source_service=get_source_service,
    )


__all__ = ["AuthorizedSourceRequest", "build_authenticated_sources_router"]
