"""Public candidate generation endpoint anchored to one Source entry."""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated, Protocol

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, ConfigDict

from life_coach.api.routers.candidate_insights import CandidateInsightResponse
from life_coach.application.candidate_insight_command import (
    CandidateInsightGenerationResult,
    CandidateInsightGenerationStatus,
)
from life_coach.application.candidate_insight_entry import (
    EntryCandidateRevisionConflict,
    EntryCandidateSourceUnavailable,
)
from life_coach.application.model_runtime import ModelRunProviderUnavailable
from life_coach.platform.auth import AuthenticationDenied, VaultMembershipDenied
from life_coach.platform.errors import ProblemError, problem_type

_PRIVATE_NO_STORE = "private, no-store"


class EntryCandidateInsightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntryCandidateInsightCommand(Protocol):
    async def execute(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> CandidateInsightGenerationResult: ...


def parse_expected_revision(if_match: str) -> int:
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    try:
        revision = int(value)
    except ValueError:
        revision = 0
    if revision < 1:
        raise ProblemError(
            type=problem_type("invalid-entry-revision"),
            title="记录版本无效",
            status=HTTPStatus.BAD_REQUEST,
            code="INVALID_ENTRY_REVISION",
            safe_detail="请刷新记录后重试。",
        )
    return revision


def create_entry_candidate_insight_router(
    *,
    command: EntryCandidateInsightCommand,
    prefix: str = "/v1/entries",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["candidate-insights"])

    @router.post("/{entry_id}/candidate-insights", response_model=CandidateInsightResponse)
    async def generate_for_entry(
        entry_id: uuid.UUID,
        _payload: EntryCandidateInsightRequest,
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> CandidateInsightResponse:
        try:
            result = await command.execute(
                authorization=authorization,
                vault_id=vault_id,
                entry_id=entry_id,
                expected_revision=parse_expected_revision(if_match),
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
        except (VaultMembershipDenied, EntryCandidateSourceUnavailable):
            raise ProblemError(
                type=problem_type("entry-unavailable"),
                title="记录不可用",
                status=HTTPStatus.NOT_FOUND,
                code="ENTRY_UNAVAILABLE",
                safe_detail="请求的记录不可用。",
            ) from None
        except EntryCandidateRevisionConflict as exc:
            raise ProblemError(
                type=problem_type("revision-conflict"),
                title="记录版本已变化",
                status=HTTPStatus.CONFLICT,
                code="REVISION_CONFLICT",
                safe_detail="请刷新记录后重试。",
                current_revision=exc.current_revision,
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
    "EntryCandidateInsightCommand",
    "EntryCandidateInsightRequest",
    "create_entry_candidate_insight_router",
    "parse_expected_revision",
]
