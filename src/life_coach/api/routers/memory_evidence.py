"""HTTP contract for live, authorized Memory evidence excerpts."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from typing import Protocol

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from life_coach.application.memory_evidence import MemoryEvidenceExcerpt
from life_coach.modules.knowledge.enums import DataClass, EvidenceRelation
from life_coach.modules.knowledge.exceptions import (
    EvidenceNotFoundError,
    EvidenceSourceUnavailableError,
)
from life_coach.platform.errors import ProblemError, problem_type


class AsyncMemoryEvidenceOperations(Protocol):
    async def get_excerpt(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        evidence_id: uuid.UUID,
    ) -> MemoryEvidenceExcerpt: ...


class MemoryEvidenceExcerptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    memory_id: uuid.UUID
    evidence_id: uuid.UUID
    relation: EvidenceRelation
    source_document_id: uuid.UUID
    source_revision_id: uuid.UUID
    source_fragment_id: uuid.UUID
    source_recorded_at: datetime
    source_data_class: DataClass
    excerpt: str = Field(min_length=1, repr=False)
    source_semantics: str = (
        "This excerpt is a user-recorded Source anchor; it does not by itself establish "
        "objective truth."
    )


def create_memory_evidence_router(
    *,
    get_service: Callable[..., AsyncMemoryEvidenceOperations],
    get_vault_id: Callable[..., uuid.UUID],
    prefix: str = "/v1",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["memories"])
    service_dependency = Depends(get_service)
    vault_dependency = Depends(get_vault_id)

    @router.get(
        "/memories/{memory_id}/evidence/{evidence_id}/excerpt",
        response_model=MemoryEvidenceExcerptResponse,
    )
    async def evidence_excerpt(
        memory_id: uuid.UUID,
        evidence_id: uuid.UUID,
        response: Response,
        service: AsyncMemoryEvidenceOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
    ) -> MemoryEvidenceExcerptResponse:
        try:
            result = await service.get_excerpt(
                vault_id=vault_id,
                memory_id=memory_id,
                evidence_id=evidence_id,
            )
        except EvidenceNotFoundError:
            raise ProblemError(
                type=problem_type("evidence-not-found"),
                title="Evidence not found",
                status=HTTPStatus.NOT_FOUND,
                code="EVIDENCE_NOT_FOUND",
                safe_detail="The evidence is not available in this memory.",
            ) from None
        except EvidenceSourceUnavailableError:
            raise ProblemError(
                type=problem_type("evidence-source-unavailable"),
                title="Evidence Source is unavailable",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                code="EVIDENCE_SOURCE_UNAVAILABLE",
                safe_detail="The current Source authorization could not be verified.",
            ) from None
        response.headers["Cache-Control"] = "private, no-store"
        return MemoryEvidenceExcerptResponse.model_validate(result, from_attributes=True)

    return router


__all__ = [
    "AsyncMemoryEvidenceOperations",
    "MemoryEvidenceExcerptResponse",
    "create_memory_evidence_router",
]
