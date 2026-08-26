"""Authenticated HTTP contract for life-line, memoir, and calendar candidates."""
# ruff: noqa: RUF001

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from life_coach.application.narrative_generation import (
    NarrativeGenerationUnavailable,
    NarrativeMaterialUnavailable,
    NarrativeProjectUnavailable,
)
from life_coach.modules.narrative.models import CalendarCandidateState, NarrativeGenerationKind
from life_coach.modules.narrative.service import (
    CalendarCandidateConflictError,
    CalendarCandidateTransitionError,
    CalendarCandidateView,
    NarrativeGenerationView,
    NarrativeNotFoundError,
    NarrativeService,
)
from life_coach.platform.errors import ProblemError, problem_type

_PRIVATE = "private, no-store"
_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
_Timezone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class NarrativeGenerator(Protocol):
    async def generate(
        self,
        *,
        vault_id: uuid.UUID,
        project_id: uuid.UUID,
        kind: Literal["life_line", "memoir_chapter"],
        idempotency_key: uuid.UUID,
    ) -> NarrativeGenerationView: ...


class NarrativeProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    project_id: uuid.UUID
    title: str
    scope_from: datetime | None
    scope_to: datetime | None
    state: str


class NarrativeCitationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    memory_id: uuid.UUID
    derived_object_id: uuid.UUID
    material_ordinal: int
    relation: str


class NarrativeThemeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    theme_id: uuid.UUID
    position: int
    title: str
    interpretation: str
    counterpoint: str
    uncovered_period: str
    citations: list[NarrativeCitationResponse]


class NarrativeGenerationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    generation_id: uuid.UUID
    project_id: uuid.UUID
    model_run_id: uuid.UUID
    kind: NarrativeGenerationKind
    title: str
    body: str
    uncertainty: str
    state: str
    created_at: datetime
    themes: list[NarrativeThemeResponse]
    citations: list[NarrativeCitationResponse]

    @classmethod
    def from_view(cls, value: NarrativeGenerationView) -> NarrativeGenerationResponse:
        return cls.model_validate(value, from_attributes=True)


class NarrativeGenerationPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[NarrativeGenerationResponse]


class CalendarCandidateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: _Title
    starts_at: datetime
    ends_at: datetime
    timezone: _Timezone
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_interval(self) -> CalendarCandidateCreateRequest:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("calendar times must include timezone offsets")
        if self.starts_at >= self.ends_at:
            raise ValueError("calendar event must end after it starts")
        if (self.ends_at - self.starts_at).total_seconds() > 24 * 60 * 60:
            raise ValueError("calendar candidate cannot exceed 24 hours")
        return self


class CalendarCandidateTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target: Literal["confirmed", "revoked"]


class CalendarCandidateResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    candidate_id: uuid.UUID
    generation_id: uuid.UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    notes: str
    state: CalendarCandidateState
    revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, value: CalendarCandidateView) -> CalendarCandidateResponse:
        return cls.model_validate(value, from_attributes=True)


class CalendarCandidatePage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[CalendarCandidateResponse]


def _problem(exc: Exception) -> ProblemError:
    if isinstance(exc, NarrativeNotFoundError | NarrativeProjectUnavailable):
        return ProblemError(
            type=problem_type("narrative-not-found"),
            title="叙事项目不可用",
            status=status.HTTP_404_NOT_FOUND,
            code="NARRATIVE_NOT_FOUND",
            safe_detail="这项叙事内容不存在或当前不可访问。",
        )
    if isinstance(exc, NarrativeMaterialUnavailable):
        return ProblemError(
            type=problem_type("narrative-material-unavailable"),
            title="可用材料不足",
            status=status.HTTP_409_CONFLICT,
            code="NARRATIVE_MATERIAL_UNAVAILABLE",
            safe_detail="请先确认至少一条带有原文依据的认识。",
        )
    if isinstance(exc, NarrativeGenerationUnavailable):
        return ProblemError(
            type=problem_type("narrative-generation-unavailable"),
            title="AI 暂时无法整理",
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="NARRATIVE_GENERATION_UNAVAILABLE",
            safe_detail="这次没有保存草稿，请稍后重试。",
        )
    if isinstance(exc, CalendarCandidateConflictError | CalendarCandidateTransitionError):
        return ProblemError(
            type=problem_type("calendar-candidate-conflict"),
            title="日历候选已经变化",
            status=status.HTTP_409_CONFLICT,
            code="CALENDAR_CANDIDATE_CONFLICT",
            safe_detail="请刷新候选后再确认。",
        )
    raise exc


def create_narrative_router(
    *,
    get_service: Callable[..., NarrativeService],
    get_generator: Callable[..., NarrativeGenerator],
    get_vault_id: Callable[..., uuid.UUID],
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["narratives"])
    service_dep = Depends(get_service)
    generator_dep = Depends(get_generator)
    vault_dep = Depends(get_vault_id)

    @router.post("/narratives/default", response_model=NarrativeProjectResponse)
    async def default_project(
        response: Response,
        service: NarrativeService = service_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> NarrativeProjectResponse:
        project = await service.get_or_create_default_project(vault_id=vault_id)
        response.headers["Cache-Control"] = _PRIVATE
        return NarrativeProjectResponse(
            project_id=project.id,
            title=project.title,
            scope_from=project.scope_from,
            scope_to=project.scope_to,
            state=project.state,
        )

    @router.get("/narratives/{project_id}/generations", response_model=NarrativeGenerationPage)
    async def generations(
        project_id: uuid.UUID,
        response: Response,
        service: NarrativeService = service_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> NarrativeGenerationPage:
        try:
            await service.get_project(vault_id=vault_id, project_id=project_id)
            items = await service.list_generations(vault_id=vault_id, project_id=project_id)
        except NarrativeNotFoundError as exc:
            raise _problem(exc) from exc
        response.headers["Cache-Control"] = _PRIVATE
        return NarrativeGenerationPage(
            items=[NarrativeGenerationResponse.from_view(item) for item in items]
        )

    @router.post(
        "/narratives/{project_id}/life-lines",
        response_model=NarrativeGenerationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def generate_life_line(
        project_id: uuid.UUID,
        response: Response,
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        generator: NarrativeGenerator = generator_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> NarrativeGenerationResponse:
        try:
            result = await generator.generate(
                vault_id=vault_id,
                project_id=project_id,
                kind="life_line",
                idempotency_key=idempotency_key,
            )
        except (
            NarrativeProjectUnavailable,
            NarrativeMaterialUnavailable,
            NarrativeGenerationUnavailable,
        ) as exc:
            raise _problem(exc) from exc
        response.headers["Cache-Control"] = _PRIVATE
        return NarrativeGenerationResponse.from_view(result)

    @router.post(
        "/narratives/{project_id}/memoir-chapters",
        response_model=NarrativeGenerationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def generate_memoir(
        project_id: uuid.UUID,
        response: Response,
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        generator: NarrativeGenerator = generator_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> NarrativeGenerationResponse:
        try:
            result = await generator.generate(
                vault_id=vault_id,
                project_id=project_id,
                kind="memoir_chapter",
                idempotency_key=idempotency_key,
            )
        except (
            NarrativeProjectUnavailable,
            NarrativeMaterialUnavailable,
            NarrativeGenerationUnavailable,
        ) as exc:
            raise _problem(exc) from exc
        response.headers["Cache-Control"] = _PRIVATE
        return NarrativeGenerationResponse.from_view(result)

    @router.post(
        "/narrative-generations/{generation_id}/calendar-candidates",
        response_model=CalendarCandidateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_calendar(
        generation_id: uuid.UUID,
        payload: CalendarCandidateCreateRequest,
        response: Response,
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        service: NarrativeService = service_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> CalendarCandidateResponse:
        try:
            result = await service.create_calendar_candidate(
                vault_id=vault_id,
                generation_id=generation_id,
                title=payload.title,
                starts_at=payload.starts_at,
                ends_at=payload.ends_at,
                timezone=payload.timezone,
                notes=payload.notes,
                idempotency_key=idempotency_key,
            )
        except (NarrativeNotFoundError, CalendarCandidateConflictError) as exc:
            raise _problem(exc) from exc
        response.headers["Cache-Control"] = _PRIVATE
        return CalendarCandidateResponse.from_view(result)

    @router.get("/calendar-candidates", response_model=CalendarCandidatePage)
    async def list_calendar(
        response: Response,
        service: NarrativeService = service_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> CalendarCandidatePage:
        items = await service.list_calendar_candidates(vault_id=vault_id)
        response.headers["Cache-Control"] = _PRIVATE
        return CalendarCandidatePage(
            items=[CalendarCandidateResponse.from_view(item) for item in items]
        )

    @router.post(
        "/calendar-candidates/{candidate_id}/transitions", response_model=CalendarCandidateResponse
    )
    async def transition_calendar(
        candidate_id: uuid.UUID,
        payload: CalendarCandidateTransitionRequest,
        response: Response,
        if_match: Annotated[int, Header(alias="If-Match")],
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
        service: NarrativeService = service_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> CalendarCandidateResponse:
        try:
            result = await service.transition_calendar_candidate(
                vault_id=vault_id,
                candidate_id=candidate_id,
                target=CalendarCandidateState(payload.target),
                expected_revision=if_match,
                idempotency_key=idempotency_key,
            )
        except (
            NarrativeNotFoundError,
            CalendarCandidateConflictError,
            CalendarCandidateTransitionError,
        ) as exc:
            raise _problem(exc) from exc
        response.headers["Cache-Control"] = _PRIVATE
        return CalendarCandidateResponse.from_view(result)

    @router.get("/calendar-candidates/{candidate_id}.ics")
    async def export_calendar(
        candidate_id: uuid.UUID,
        service: NarrativeService = service_dep,
        vault_id: uuid.UUID = vault_dep,
    ) -> Response:
        try:
            candidate = await service.get_calendar_candidate(
                vault_id=vault_id, candidate_id=candidate_id
            )
        except NarrativeNotFoundError as exc:
            raise _problem(exc) from exc
        if candidate.state is not CalendarCandidateState.CONFIRMED:
            raise _problem(CalendarCandidateTransitionError("calendar candidate is not confirmed"))
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        start = candidate.starts_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        end = candidate.ends_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        title = (
            candidate.title.replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace(";", "\\;")
            .replace("\n", "\\n")
        )
        body = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Vistora//Private Calendar Candidate//CN",
                "BEGIN:VEVENT",
                f"UID:{candidate.candidate_id}@vistora.local",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{start}",
                f"DTEND:{end}",
                f"SUMMARY:{title}",
                "END:VEVENT",
                "END:VCALENDAR",
                "",
            ]
        )
        return Response(
            content=body,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Cache-Control": _PRIVATE,
                "Content-Disposition": (
                    f'attachment; filename="vistora-{candidate.candidate_id}.ics"'
                ),
            },
        )

    return router


__all__ = ["create_narrative_router"]
