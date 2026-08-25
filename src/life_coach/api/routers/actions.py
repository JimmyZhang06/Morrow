"""HTTP contract for the single reversible small-action loop."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict

from life_coach.modules.action.lifecycle import (
    ActionAuthenticationRequiredError,
    ActionGenerationUnavailableError,
    ActionIdempotencyConflictError,
    ActionNotFoundError,
    ActionRevisionConflictError,
    ActionVaultUnavailableError,
    InvalidActionTransitionError,
    MemoryNotEligibleForActionError,
    ReversibleActionPage,
    ReversibleActionState,
    ReversibleActionVerdict,
    ReversibleActionVerdictOutcome,
    ReversibleActionView,
)
from life_coach.modules.action.service import (
    AsyncReversibleActionOperations,
    make_action_etag,
)
from life_coach.platform.errors import ProblemError, problem_type

_PRIVATE_NO_STORE = "private, no-store"
_IDEMPOTENCY_HEADER = Header(alias="Idempotency-Key")


class ActionCreateRequest(BaseModel):
    """The first loop intentionally has no user- or model-controlled action payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReversibleActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: uuid.UUID
    memory_id: uuid.UUID
    source_derived_object_id: uuid.UUID
    model_run_id: uuid.UUID | None
    state: ReversibleActionState
    revision: int
    kind: str
    title: str
    description: str
    rationale: str
    exit_plan: str
    estimated_minutes: int
    is_reversible: bool
    template_version: str
    created_at: datetime
    updated_at: datetime
    etag: str

    @classmethod
    def from_domain(cls, value: ReversibleActionView) -> ReversibleActionResponse:
        return cls.model_validate(
            {
                **asdict(value),
                "etag": make_action_etag(value.action_id, value.revision),
            }
        )


class ReversibleActionPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ReversibleActionResponse]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, value: ReversibleActionPage) -> ReversibleActionPageResponse:
        return cls(
            items=[ReversibleActionResponse.from_domain(item) for item in value.items],
            next_cursor=value.next_cursor,
        )


class ActionVerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReversibleActionVerdict


class ActionVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict_id: uuid.UUID
    action_id: uuid.UUID
    state: ReversibleActionState
    revision: int
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: ReversibleActionVerdictOutcome) -> ActionVerdictResponse:
        return cls(
            verdict_id=value.verdict_id,
            action_id=value.action.action_id,
            state=value.action.state,
            revision=value.action.revision,
            updated_at=value.action.updated_at,
        )


def _mark_action(response: Response, action: ReversibleActionView) -> None:
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    response.headers["ETag"] = make_action_etag(action.action_id, action.revision)


def _parse_if_match(value: str, *, action_id: uuid.UUID) -> int:
    prefix = f'"action:{action_id}:'
    if not value.startswith(prefix) or not value.endswith('"'):
        raise ProblemError(
            type=problem_type("action-precondition-required"),
            title="Action precondition is invalid",
            status=status.HTTP_412_PRECONDITION_FAILED,
            code="ACTION_PRECONDITION_INVALID",
            safe_detail="Refresh the action before applying this change.",
        )
    revision_text = value[len(prefix) : -1]
    if not revision_text.isascii() or not revision_text.isdigit() or int(revision_text) < 1:
        raise ProblemError(
            type=problem_type("action-precondition-required"),
            title="Action precondition is invalid",
            status=status.HTTP_412_PRECONDITION_FAILED,
            code="ACTION_PRECONDITION_INVALID",
            safe_detail="Refresh the action before applying this change.",
        )
    return int(revision_text)


def _raise_problem(exc: Exception) -> NoReturn:
    if isinstance(exc, ActionAuthenticationRequiredError):
        raise ProblemError(
            type=problem_type("authentication-required"),
            title="需要登录",
            status=status.HTTP_401_UNAUTHORIZED,
            code="AUTHENTICATION_REQUIRED",
            safe_detail="请登录后重试。",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if isinstance(exc, ActionVaultUnavailableError):
        raise ProblemError(
            type=problem_type("vault-unavailable"),
            title="空间不可用",
            status=status.HTTP_404_NOT_FOUND,
            code="VAULT_UNAVAILABLE",
            safe_detail="请求的空间不可用。",
        ) from exc
    if isinstance(exc, ActionNotFoundError):
        raise ProblemError(
            type=problem_type("action-not-found"),
            title="Action not found",
            status=status.HTTP_404_NOT_FOUND,
            code="ACTION_NOT_FOUND",
            safe_detail="The action does not exist or is not available in this vault.",
        ) from exc
    if isinstance(exc, MemoryNotEligibleForActionError):
        raise ProblemError(
            type=problem_type("memory-not-actionable"),
            title="Memory is not ready for an action",
            status=status.HTTP_409_CONFLICT,
            code="MEMORY_NOT_ACTIONABLE",
            safe_detail="Confirm or correct the current memory before creating an action.",
        ) from exc
    if isinstance(exc, ActionRevisionConflictError):
        raise ProblemError(
            type=problem_type("action-revision-conflict"),
            title="Action changed on another client",
            status=status.HTTP_409_CONFLICT,
            code="ACTION_REVISION_CONFLICT",
            safe_detail="Refresh the action before applying this change.",
            current_revision=exc.current_revision,
        ) from exc
    if isinstance(exc, InvalidActionTransitionError):
        raise ProblemError(
            type=problem_type("invalid-action-transition"),
            title="Action verdict cannot be applied",
            status=status.HTTP_409_CONFLICT,
            code="INVALID_ACTION_TRANSITION",
            safe_detail="Refresh the action and review its current state.",
        ) from exc
    if isinstance(exc, ActionIdempotencyConflictError):
        raise ProblemError(
            type=problem_type("action-idempotency-conflict"),
            title="Idempotency key is already in use",
            status=status.HTTP_409_CONFLICT,
            code="ACTION_IDEMPOTENCY_CONFLICT",
            safe_detail="Use a new idempotency key for a different action command.",
        ) from exc
    if isinstance(exc, ActionGenerationUnavailableError):
        raise ProblemError(
            type=problem_type("action-generation-unavailable"),
            title="Action generation is temporarily unavailable",
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="ACTION_GENERATION_UNAVAILABLE",
            safe_detail=(
                "The AI could not prepare this small action. "
                "Retry when the model service is available."
            ),
        ) from exc
    raise exc


def create_action_router(
    *,
    get_service: Callable[..., AsyncReversibleActionOperations],
    get_vault_id: Callable[..., uuid.UUID],
    get_create_service: Callable[..., AsyncReversibleActionOperations] | None = None,
    get_create_vault_id: Callable[..., uuid.UUID] | None = None,
    prefix: str = "/v1",
) -> APIRouter:
    """Build the route contract without owning authentication or transaction lifetime."""

    router = APIRouter(prefix=prefix, tags=["actions"])
    service_dependency = Depends(get_service)
    vault_dependency = Depends(get_vault_id)
    create_service_dependency = Depends(get_create_service or get_service)
    create_vault_dependency = Depends(get_create_vault_id or get_vault_id)

    @router.post(
        "/memories/{memory_id}/actions",
        response_model=ReversibleActionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_action(
        memory_id: uuid.UUID,
        _payload: ActionCreateRequest,
        response: Response,
        idempotency_key: uuid.UUID = _IDEMPOTENCY_HEADER,
        service: AsyncReversibleActionOperations = create_service_dependency,
        vault_id: uuid.UUID = create_vault_dependency,
    ) -> ReversibleActionResponse:
        try:
            action = await service.create_for_memory(
                vault_id=vault_id,
                memory_id=memory_id,
                idempotency_key=idempotency_key,
            )
        except (
            ActionAuthenticationRequiredError,
            ActionIdempotencyConflictError,
            ActionGenerationUnavailableError,
            MemoryNotEligibleForActionError,
            ActionVaultUnavailableError,
        ) as exc:
            _raise_problem(exc)
        _mark_action(response, action)
        return ReversibleActionResponse.from_domain(action)

    @router.get("/actions/{action_id}", response_model=ReversibleActionResponse)
    async def get_action(
        action_id: uuid.UUID,
        response: Response,
        service: AsyncReversibleActionOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
    ) -> ReversibleActionResponse:
        try:
            action = await service.get(vault_id=vault_id, action_id=action_id)
        except ActionNotFoundError as exc:
            _raise_problem(exc)
        _mark_action(response, action)
        return ReversibleActionResponse.from_domain(action)

    @router.get("/actions", response_model=ReversibleActionPageResponse)
    async def list_actions(
        response: Response,
        service: AsyncReversibleActionOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> ReversibleActionPageResponse:
        try:
            page = await service.list(vault_id=vault_id, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise ProblemError(
                type=problem_type("invalid-action-cursor"),
                title="Action cursor is invalid",
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="INVALID_ACTION_CURSOR",
                safe_detail="Refresh the action history before continuing.",
            ) from exc
        response.headers["Cache-Control"] = _PRIVATE_NO_STORE
        return ReversibleActionPageResponse.from_domain(page)

    @router.post("/actions/{action_id}/verdicts", response_model=ActionVerdictResponse)
    async def record_action_verdict(
        action_id: uuid.UUID,
        payload: ActionVerdictRequest,
        response: Response,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: uuid.UUID = _IDEMPOTENCY_HEADER,
        service: AsyncReversibleActionOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
    ) -> ActionVerdictResponse:
        expected_revision = _parse_if_match(if_match, action_id=action_id)
        try:
            outcome = await service.record_verdict(
                vault_id=vault_id,
                action_id=action_id,
                verdict=payload.verdict,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        except (
            ActionIdempotencyConflictError,
            ActionNotFoundError,
            ActionRevisionConflictError,
            InvalidActionTransitionError,
        ) as exc:
            _raise_problem(exc)
        _mark_action(response, outcome.action)
        return ActionVerdictResponse.from_domain(outcome)

    return router


__all__ = [
    "ActionCreateRequest",
    "ActionVerdictRequest",
    "ActionVerdictResponse",
    "ReversibleActionPageResponse",
    "ReversibleActionResponse",
    "create_action_router",
]
