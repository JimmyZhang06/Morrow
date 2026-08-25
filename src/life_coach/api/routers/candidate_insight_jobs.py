"""Public asynchronous candidate-insight job resource."""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated, Protocol

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, ConfigDict

from life_coach.api.routers.entry_candidate_insights import (
    EntryCandidateInsightRequest,
    parse_expected_revision,
)
from life_coach.application.candidate_insight_entry import (
    EntryCandidateRevisionConflict,
    EntryCandidateSourceUnavailable,
)
from life_coach.application.candidate_insight_jobs import (
    CandidateInsightJobProjection,
    CandidateInsightJobStatus,
)
from life_coach.jobs.contracts import IdempotencyConflict
from life_coach.platform.auth import AuthenticationDenied, VaultMembershipDenied
from life_coach.platform.errors import ProblemError, problem_type

_PRIVATE_NO_STORE = "private, no-store"


class CandidateInsightJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: uuid.UUID
    status: CandidateInsightJobStatus
    stage: str
    progress: int
    retryable: bool
    run_id: uuid.UUID | None = None
    memory_id: uuid.UUID | None = None
    derived_object_id: uuid.UUID | None = None


class CandidateInsightJobs(Protocol):
    async def enqueue(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> CandidateInsightJobProjection: ...

    async def get(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CandidateInsightJobProjection: ...

    async def cancel(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> CandidateInsightJobProjection: ...


def _response(projection: CandidateInsightJobProjection) -> CandidateInsightJobResponse:
    return CandidateInsightJobResponse.model_validate(projection, from_attributes=True)


def _raise_public_error(exc: Exception) -> None:
    if isinstance(exc, AuthenticationDenied):
        raise ProblemError(
            type=problem_type("authentication-required"),
            title="需要登录",
            status=HTTPStatus.UNAUTHORIZED,
            code="AUTHENTICATION_REQUIRED",
            safe_detail="请登录后重试。",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if isinstance(exc, (VaultMembershipDenied, EntryCandidateSourceUnavailable)):
        raise ProblemError(
            type=problem_type("candidate-job-unavailable"),
            title="整理任务不可用",
            status=HTTPStatus.NOT_FOUND,
            code="CANDIDATE_JOB_UNAVAILABLE",
            safe_detail="请求的记录或整理任务不可用。",
        ) from None
    if isinstance(exc, EntryCandidateRevisionConflict):
        raise ProblemError(
            type=problem_type("revision-conflict"),
            title="记录版本已变化",
            status=HTTPStatus.CONFLICT,
            code="REVISION_CONFLICT",
            safe_detail="请刷新记录后重试。",
            current_revision=exc.current_revision,
        ) from None
    if isinstance(exc, IdempotencyConflict):
        raise ProblemError(
            type=problem_type("idempotency-conflict"),
            title="请求标识已被使用",
            status=HTTPStatus.CONFLICT,
            code="IDEMPOTENCY_CONFLICT",
            safe_detail="请重新发起整理。",
        ) from None
    raise exc


def create_candidate_insight_job_router(
    *,
    jobs: CandidateInsightJobs,
) -> APIRouter:
    router = APIRouter(tags=["candidate-insights"])

    @router.post(
        "/v1/entries/{entry_id}/candidate-insights",
        response_model=CandidateInsightJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue(
        entry_id: uuid.UUID,
        _payload: EntryCandidateInsightRequest,
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        if_match: Annotated[str, Header(alias="If-Match")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> CandidateInsightJobResponse:
        try:
            projection = await jobs.enqueue(
                authorization=authorization,
                vault_id=vault_id,
                entry_id=entry_id,
                expected_revision=parse_expected_revision(if_match),
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            _raise_public_error(exc)
            raise AssertionError("unreachable") from None
        if projection.status in {
            CandidateInsightJobStatus.SUCCEEDED,
            CandidateInsightJobStatus.FAILED,
            CandidateInsightJobStatus.UNKNOWN,
            CandidateInsightJobStatus.DENIED,
            CandidateInsightJobStatus.CANCELED,
        }:
            response.status_code = status.HTTP_200_OK
        response.headers["Cache-Control"] = _PRIVATE_NO_STORE
        return _response(projection)

    @router.get(
        "/v1/candidate-insight-jobs/{job_id}",
        response_model=CandidateInsightJobResponse,
    )
    async def get_job(
        job_id: uuid.UUID,
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> CandidateInsightJobResponse:
        try:
            projection = await jobs.get(
                authorization=authorization,
                vault_id=vault_id,
                job_id=job_id,
            )
        except Exception as exc:
            _raise_public_error(exc)
            raise AssertionError("unreachable") from None
        response.headers["Cache-Control"] = _PRIVATE_NO_STORE
        return _response(projection)

    @router.delete(
        "/v1/candidate-insight-jobs/{job_id}",
        response_model=CandidateInsightJobResponse,
    )
    async def cancel_job(
        job_id: uuid.UUID,
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> CandidateInsightJobResponse:
        try:
            projection = await jobs.cancel(
                authorization=authorization,
                vault_id=vault_id,
                job_id=job_id,
            )
        except Exception as exc:
            _raise_public_error(exc)
            raise AssertionError("unreachable") from None
        response.headers["Cache-Control"] = _PRIVATE_NO_STORE
        return _response(projection)

    return router


__all__ = [
    "CandidateInsightJobResponse",
    "CandidateInsightJobs",
    "create_candidate_insight_job_router",
]
