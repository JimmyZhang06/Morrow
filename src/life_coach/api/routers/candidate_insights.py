"""Minimal authenticated HTTP boundary for candidate-insight generation."""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated, Protocol

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from life_coach.application.candidate_insight_command import (
    CandidateInsightGenerationResult,
    CandidateInsightGenerationStatus,
)
from life_coach.application.model_runtime import ModelRunProviderUnavailable
from life_coach.platform.auth import AuthenticationDenied, VaultMembershipDenied
from life_coach.platform.errors import ProblemError, problem_type

_PRIVATE_NO_STORE = "private, no-store"


class CandidateInsightCommand(Protocol):
    async def execute(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: uuid.UUID,
    ) -> CandidateInsightGenerationResult: ...


class CandidateInsightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fragment_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def require_unique_fragments(self) -> CandidateInsightRequest:
        if len(set(self.fragment_ids)) != len(self.fragment_ids):
            raise ValueError("candidate fragment identifiers must be unique")
        return self


class CandidateInsightResponse(BaseModel):
    """Only server-minted technical identities; never raw model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CandidateInsightGenerationStatus
    run_id: uuid.UUID
    memory_id: uuid.UUID | None = None
    derived_object_id: uuid.UUID | None = None


def create_candidate_insight_router(
    *,
    command: CandidateInsightCommand,
    prefix: str = "/v1/candidate-insights",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["candidate-insights"])

    @router.post("", response_model=CandidateInsightResponse)
    async def generate_candidate_insight(
        payload: CandidateInsightRequest,
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> CandidateInsightResponse:
        try:
            result = await command.execute(
                authorization=authorization,
                vault_id=vault_id,
                fragment_ids=payload.fragment_ids,
                idempotency_key=idempotency_key,
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
        except ModelRunProviderUnavailable:
            raise ProblemError(
                type=problem_type("model-provider-unavailable"),
                title="认识整理服务暂时无法连接",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                code="MODEL_PROVIDER_UNAVAILABLE",
                safe_detail="没有发送模型请求。请检查网络或后端代理设置后重新尝试。",
                headers={"Retry-After": "5"},
            ) from None
        if result.status is CandidateInsightGenerationStatus.PROCESSING:
            response.status_code = status.HTTP_202_ACCEPTED
        elif result.status is not CandidateInsightGenerationStatus.SUCCEEDED:
            response.status_code = status.HTTP_409_CONFLICT
        response.headers["Cache-Control"] = _PRIVATE_NO_STORE
        return CandidateInsightResponse.model_validate(result, from_attributes=True)

    return router


__all__ = [
    "CandidateInsightCommand",
    "CandidateInsightRequest",
    "CandidateInsightResponse",
    "create_candidate_insight_router",
]
