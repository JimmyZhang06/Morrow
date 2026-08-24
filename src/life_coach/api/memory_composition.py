"""Production composition for authenticated, Vault-scoped Memory review routes."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from life_coach.api.routers.memories import create_memory_router
from life_coach.application.model_gateway import (
    KnowledgeAuthorizationSnapshotAdapter,
    KnowledgeEvidenceAuthorityAdapter,
    SourceConsentAuthority,
)
from life_coach.application.source_entries import (
    ProtectedCorrectionSourceRecorder,
    ProtectedSourceFragmentPlaintextReader,
    SourceContentProtector,
)
from life_coach.modules.knowledge.service import AsyncMemoryOperations, AsyncMemoryService
from life_coach.platform.auth import (
    AuthenticationDenied,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.errors import ProblemError, problem_type


@dataclass(frozen=True, slots=True)
class AuthorizedMemoryRequest:
    vault_id: uuid.UUID
    service: AsyncMemoryOperations


def build_authenticated_memory_router(
    *,
    sessions: ProductionSessionFactory,
    protector: SourceContentProtector,
) -> APIRouter:
    """Bind Memory review and correction to one authorized committing transaction."""

    plaintext_reader = ProtectedSourceFragmentPlaintextReader(protector)
    source_authority = SourceConsentAuthority(plaintext_reader)
    evidence_authority = KnowledgeEvidenceAuthorityAdapter(source_authority)
    authorization_authority = KnowledgeAuthorizationSnapshotAdapter()
    correction_recorder = ProtectedCorrectionSourceRecorder(protector)

    async def open_authorized_memory(
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AsyncIterator[AuthorizedMemoryRequest]:
        try:
            async with sessions.open(
                authorization=authorization,
                vault_id=vault_id,
            ) as authorized:
                yield AuthorizedMemoryRequest(
                    vault_id=authorized.context.vault_id,
                    service=AsyncMemoryService(
                        authorized.session,
                        evidence_source_verifier=evidence_authority,
                        correction_source_recorder=correction_recorder,
                        authorization_verifier=authorization_authority,
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
            AuthorizedMemoryRequest,
            Depends(open_authorized_memory, scope="function"),
        ],
    ) -> uuid.UUID:
        return context.vault_id

    def get_memory_service(
        context: Annotated[
            AuthorizedMemoryRequest,
            Depends(open_authorized_memory, scope="function"),
        ],
    ) -> AsyncMemoryOperations:
        return context.service

    return create_memory_router(
        get_service=get_memory_service,
        get_vault_id=get_vault_id,
    )


__all__ = ["AuthorizedMemoryRequest", "build_authenticated_memory_router"]
