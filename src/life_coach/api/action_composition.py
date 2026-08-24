"""Authenticated, Vault-scoped composition for reversible action routes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from life_coach.api.routers.actions import create_action_router
from life_coach.modules.action.service import (
    AsyncReversibleActionOperations,
    AsyncReversibleActionService,
)
from life_coach.platform.auth import (
    AuthenticationDenied,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.errors import ProblemError, problem_type


@dataclass(frozen=True, slots=True)
class AuthorizedActionRequest:
    vault_id: uuid.UUID
    service: AsyncReversibleActionOperations


def build_authenticated_action_router(*, sessions: ProductionSessionFactory) -> APIRouter:
    """Bind all action commands to one authenticated committing transaction."""

    async def open_authorized_action(
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AsyncIterator[AuthorizedActionRequest]:
        try:
            async with sessions.open(
                authorization=authorization,
                vault_id=vault_id,
            ) as authorized:
                yield AuthorizedActionRequest(
                    vault_id=authorized.context.vault_id,
                    service=AsyncReversibleActionService(authorized.session),
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
            AuthorizedActionRequest,
            Depends(open_authorized_action, scope="function"),
        ],
    ) -> uuid.UUID:
        return context.vault_id

    def get_action_service(
        context: Annotated[
            AuthorizedActionRequest,
            Depends(open_authorized_action, scope="function"),
        ],
    ) -> AsyncReversibleActionOperations:
        return context.service

    return create_action_router(
        get_service=get_action_service,
        get_vault_id=get_vault_id,
    )


__all__ = ["AuthorizedActionRequest", "build_authenticated_action_router"]
