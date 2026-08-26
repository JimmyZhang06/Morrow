"""Application-facing narrative and calendar-candidate persistence services."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.modules.narrative.models import (
    CalendarCandidate,
    CalendarCandidateState,
    CalendarCommandReceipt,
    NarrativeCitation,
    NarrativeGeneration,
    NarrativeGenerationKind,
    NarrativeProject,
    NarrativeTheme,
)
from life_coach.shared.database import utc_now


class NarrativeNotFoundError(LookupError):
    pass


class CalendarCandidateConflictError(RuntimeError):
    pass


class CalendarCandidateTransitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NarrativeCitationView:
    memory_id: uuid.UUID
    derived_object_id: uuid.UUID
    material_ordinal: int
    relation: str


@dataclass(frozen=True, slots=True)
class NarrativeThemeView:
    theme_id: uuid.UUID
    position: int
    title: str
    interpretation: str
    counterpoint: str
    uncovered_period: str
    citations: tuple[NarrativeCitationView, ...]


@dataclass(frozen=True, slots=True)
class NarrativeGenerationView:
    generation_id: uuid.UUID
    project_id: uuid.UUID
    model_run_id: uuid.UUID
    kind: NarrativeGenerationKind
    title: str
    body: str
    uncertainty: str
    state: str
    created_at: datetime
    themes: tuple[NarrativeThemeView, ...]
    citations: tuple[NarrativeCitationView, ...]


@dataclass(frozen=True, slots=True)
class CalendarCandidateView:
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


class NarrativeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_default_project(self, *, vault_id: uuid.UUID) -> NarrativeProject:
        existing = await self._session.scalar(
            select(NarrativeProject).where(
                NarrativeProject.vault_id == vault_id,
                NarrativeProject.is_default.is_(True),
            )
        )
        if existing is not None:
            return existing
        project = NarrativeProject(
            vault_id=vault_id,
            title="我的人生叙事",
            is_default=True,
            state="active",
        )
        self._session.add(project)
        await self._session.flush()
        return project

    async def get_project(self, *, vault_id: uuid.UUID, project_id: uuid.UUID) -> NarrativeProject:
        project = await self._session.scalar(
            select(NarrativeProject).where(
                NarrativeProject.vault_id == vault_id,
                NarrativeProject.id == project_id,
                NarrativeProject.state == "active",
            )
        )
        if project is None:
            raise NarrativeNotFoundError("narrative project is unavailable")
        return project

    async def get_generation(
        self, *, vault_id: uuid.UUID, generation_id: uuid.UUID
    ) -> NarrativeGenerationView:
        generation = await self._session.scalar(
            select(NarrativeGeneration).where(
                NarrativeGeneration.vault_id == vault_id,
                NarrativeGeneration.id == generation_id,
            )
        )
        if generation is None:
            raise NarrativeNotFoundError("narrative generation is unavailable")
        return await self._generation_view(generation)

    async def list_generations(
        self, *, vault_id: uuid.UUID, project_id: uuid.UUID
    ) -> tuple[NarrativeGenerationView, ...]:
        rows = (
            await self._session.scalars(
                select(NarrativeGeneration)
                .where(
                    NarrativeGeneration.vault_id == vault_id,
                    NarrativeGeneration.project_id == project_id,
                )
                .order_by(NarrativeGeneration.created_at.desc(), NarrativeGeneration.id.desc())
            )
        ).all()
        return tuple([await self._generation_view(row) for row in rows])

    async def _generation_view(self, generation: NarrativeGeneration) -> NarrativeGenerationView:
        themes = (
            await self._session.scalars(
                select(NarrativeTheme)
                .where(
                    NarrativeTheme.vault_id == generation.vault_id,
                    NarrativeTheme.generation_id == generation.id,
                )
                .order_by(NarrativeTheme.position)
            )
        ).all()
        citations = (
            await self._session.scalars(
                select(NarrativeCitation).where(
                    NarrativeCitation.vault_id == generation.vault_id,
                    NarrativeCitation.generation_id == generation.id,
                )
            )
        ).all()
        citation_views = {
            row.id: NarrativeCitationView(
                memory_id=row.memory_claim_id,
                derived_object_id=row.derived_object_id,
                material_ordinal=row.material_ordinal,
                relation=row.relation,
            )
            for row in citations
        }
        theme_views = tuple(
            NarrativeThemeView(
                theme_id=theme.id,
                position=theme.position,
                title=theme.title,
                interpretation=theme.interpretation,
                counterpoint=theme.counterpoint,
                uncovered_period=theme.uncovered_period,
                citations=tuple(
                    citation_views[row.id] for row in citations if row.theme_id == theme.id
                ),
            )
            for theme in themes
        )
        chapter_citations = tuple(
            citation_views[row.id] for row in citations if row.theme_id is None
        )
        return NarrativeGenerationView(
            generation_id=generation.id,
            project_id=generation.project_id,
            model_run_id=generation.model_run_id,
            kind=generation.kind,
            title=generation.title,
            body=generation.body,
            uncertainty=generation.uncertainty,
            state=generation.state,
            created_at=generation.created_at,
            themes=theme_views,
            citations=chapter_citations,
        )

    async def create_calendar_candidate(
        self,
        *,
        vault_id: uuid.UUID,
        generation_id: uuid.UUID,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        timezone: str,
        notes: str,
        idempotency_key: uuid.UUID,
    ) -> CalendarCandidateView:
        generation = await self._session.scalar(
            select(NarrativeGeneration).where(
                NarrativeGeneration.vault_id == vault_id,
                NarrativeGeneration.id == generation_id,
                NarrativeGeneration.kind == NarrativeGenerationKind.MEMOIR_CHAPTER,
            )
        )
        if generation is None:
            raise NarrativeNotFoundError("memoir chapter is unavailable")
        payload_hash = _calendar_hash(
            generation_id=generation_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=timezone,
            notes=notes,
        )
        replay = await self._receipt_replay(vault_id, idempotency_key, payload_hash)
        if replay is not None:
            return _calendar_view(replay)
        candidate = CalendarCandidate(
            vault_id=vault_id,
            generation_id=generation_id,
            title=title.strip(),
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=timezone.strip(),
            notes=notes.strip(),
            payload_hash=payload_hash,
            state=CalendarCandidateState.PROPOSED,
            revision=1,
        )
        self._session.add(candidate)
        await self._session.flush()
        self._session.add(
            CalendarCommandReceipt(
                vault_id=vault_id,
                idempotency_key=idempotency_key,
                candidate_id=candidate.id,
                command_kind="create",
                command_hash=payload_hash,
                created_at=utc_now(),
            )
        )
        await self._session.flush()
        return _calendar_view(candidate)

    async def transition_calendar_candidate(
        self,
        *,
        vault_id: uuid.UUID,
        candidate_id: uuid.UUID,
        target: CalendarCandidateState,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> CalendarCandidateView:
        candidate = await self._session.scalar(
            select(CalendarCandidate)
            .where(
                CalendarCandidate.vault_id == vault_id,
                CalendarCandidate.id == candidate_id,
            )
            .with_for_update()
        )
        if candidate is None:
            raise NarrativeNotFoundError("calendar candidate is unavailable")
        command_hash = hashlib.sha256(
            f"{candidate_id}:{target.value}:{expected_revision}".encode()
        ).hexdigest()
        replay = await self._receipt_replay(vault_id, idempotency_key, command_hash)
        if replay is not None:
            return _calendar_view(replay)
        if candidate.revision != expected_revision:
            raise CalendarCandidateConflictError("calendar candidate revision changed")
        if (
            target is CalendarCandidateState.CONFIRMED
            and candidate.state is CalendarCandidateState.PROPOSED
        ):
            candidate.state = target
            candidate.confirmed_at = utc_now()
        elif target is CalendarCandidateState.REVOKED and candidate.state in {
            CalendarCandidateState.PROPOSED,
            CalendarCandidateState.CONFIRMED,
        }:
            candidate.state = target
            candidate.revoked_at = utc_now()
        else:
            raise CalendarCandidateTransitionError("calendar candidate transition is unavailable")
        candidate.revision += 1
        self._session.add(
            CalendarCommandReceipt(
                vault_id=vault_id,
                idempotency_key=idempotency_key,
                candidate_id=candidate.id,
                command_kind=target.value,
                command_hash=command_hash,
                created_at=utc_now(),
            )
        )
        await self._session.flush()
        return _calendar_view(candidate)

    async def get_calendar_candidate(
        self, *, vault_id: uuid.UUID, candidate_id: uuid.UUID
    ) -> CalendarCandidateView:
        candidate = await self._session.scalar(
            select(CalendarCandidate).where(
                CalendarCandidate.vault_id == vault_id,
                CalendarCandidate.id == candidate_id,
            )
        )
        if candidate is None:
            raise NarrativeNotFoundError("calendar candidate is unavailable")
        return _calendar_view(candidate)

    async def list_calendar_candidates(
        self, *, vault_id: uuid.UUID
    ) -> tuple[CalendarCandidateView, ...]:
        rows = (
            await self._session.scalars(
                select(CalendarCandidate)
                .where(CalendarCandidate.vault_id == vault_id)
                .order_by(CalendarCandidate.created_at.desc())
            )
        ).all()
        return tuple(_calendar_view(row) for row in rows)

    async def _receipt_replay(
        self, vault_id: uuid.UUID, key: uuid.UUID, command_hash: str
    ) -> CalendarCandidate | None:
        receipt = await self._session.scalar(
            select(CalendarCommandReceipt).where(
                CalendarCommandReceipt.vault_id == vault_id,
                CalendarCommandReceipt.idempotency_key == key,
            )
        )
        if receipt is None:
            return None
        if receipt.command_hash != command_hash:
            raise CalendarCandidateConflictError("calendar idempotency key is already in use")
        return cast(
            CalendarCandidate | None,
            await self._session.scalar(
                select(CalendarCandidate).where(
                    CalendarCandidate.vault_id == vault_id,
                    CalendarCandidate.id == receipt.candidate_id,
                )
            ),
        )


def _calendar_hash(
    *,
    generation_id: uuid.UUID,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    timezone: str,
    notes: str,
) -> str:
    payload = json.dumps(
        {
            "generation_id": str(generation_id),
            "title": title.strip(),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "timezone": timezone.strip(),
            "notes": notes.strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _calendar_view(value: CalendarCandidate) -> CalendarCandidateView:
    return CalendarCandidateView(
        candidate_id=value.id,
        generation_id=value.generation_id,
        title=value.title,
        starts_at=value.starts_at,
        ends_at=value.ends_at,
        timezone=value.timezone,
        notes=value.notes,
        state=value.state,
        revision=value.revision,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


__all__ = [
    "CalendarCandidateConflictError",
    "CalendarCandidateTransitionError",
    "CalendarCandidateView",
    "NarrativeGenerationView",
    "NarrativeNotFoundError",
    "NarrativeService",
]
