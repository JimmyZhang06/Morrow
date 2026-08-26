"""Authenticated composition for narrative and calendar-candidate routes."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header

from life_coach.api.routers.narratives import NarrativeGenerator, create_narrative_router
from life_coach.application.narrative_generation import GovernedNarrativeCreator, NarrativeRuntime
from life_coach.modules.narrative.service import NarrativeService
from life_coach.platform.auth import (
    AuthenticationDenied,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.errors import ProblemError, problem_type


@dataclass(frozen=True, slots=True)
class AuthorizedNarrativeRequest:
    vault_id: uuid.UUID
    service: NarrativeService


def build_authenticated_narrative_router(
    *, sessions: ProductionSessionFactory, runtime: NarrativeRuntime
) -> APIRouter:
    async def open_request(
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AsyncIterator[AuthorizedNarrativeRequest]:
        try:
            async with sessions.open(authorization=authorization, vault_id=vault_id) as authorized:
                yield AuthorizedNarrativeRequest(
                    vault_id=authorized.context.vault_id,
                    service=NarrativeService(authorized.session),
                )
        except AuthenticationDenied:
            raise ProblemError(
                type=problem_type("authentication-required"),
                title="需要登录",
                status=401,
                code="AUTHENTICATION_REQUIRED",
                safe_detail="请登录后重试。",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        except VaultMembershipDenied:
            raise ProblemError(
                type=problem_type("vault-unavailable"),
                title="空间不可用",
                status=404,
                code="VAULT_UNAVAILABLE",
                safe_detail="请求的空间不可用。",
            ) from None

    def get_service(
        context: Annotated[AuthorizedNarrativeRequest, Depends(open_request, scope="function")],
    ) -> NarrativeService:
        return context.service

    def get_vault_id(
        context: Annotated[AuthorizedNarrativeRequest, Depends(open_request, scope="function")],
    ) -> uuid.UUID:
        return context.vault_id

    def get_generator(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> NarrativeGenerator:
        return cast(
            NarrativeGenerator,
            GovernedNarrativeCreator(
                sessions=sessions, runtime=runtime, authorization=authorization
            ),
        )

    return create_narrative_router(
        get_service=get_service,
        get_generator=get_generator,
        get_vault_id=get_vault_id,
    )


__all__ = ["build_authenticated_narrative_router"]
