"""FastAPI router factory for the user-governed Memory API."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from life_coach.modules.knowledge.contracts import (
    ClaimVersionView,
    EvidenceView,
    InboxItem,
    InboxPage,
    MemoryDetail,
    VerdictOutcome,
    VerdictView,
)
from life_coach.modules.knowledge.enums import (
    Attribution,
    ConfidenceBand,
    EpistemicType,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    ValidTimePrecision,
    VerdictType,
)
from life_coach.modules.knowledge.exceptions import (
    CorrectionSourceUnavailableError,
    InvalidLifecycleTransitionError,
    InvalidTemporalIntervalError,
    InvalidVerdictError,
    MemoryNotFoundError,
    PolicyViolationError,
    RevisionConflictError,
)
from life_coach.modules.knowledge.service import AsyncMemoryOperations


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemporalRangeResponse(ApiModel):
    from_: datetime = Field(alias="from")
    to: datetime | None
    precision: ValidTimePrecision
    original_expression: str | None
    timezone: str | None


class SystemRangeResponse(ApiModel):
    from_: datetime = Field(alias="from")
    to: datetime | None


class ClaimVersionResponse(ApiModel):
    derived_object_id: uuid.UUID
    version_no: int
    statement: str
    structured_payload: dict[str, Any]
    epistemic_type: EpistemicType
    attribution: Attribution
    uncertainty: str | None
    state: LifecycleState
    valid_time: TemporalRangeResponse
    system_time: SystemRangeResponse
    confidence_band: ConfidenceBand
    pipeline_version: str

    @classmethod
    def from_domain(cls, value: ClaimVersionView) -> ClaimVersionResponse:
        return cls(
            derived_object_id=value.derived_object_id,
            version_no=value.version_no,
            statement=value.statement,
            structured_payload=value.structured_payload,
            epistemic_type=value.epistemic_type,
            attribution=value.attribution,
            uncertainty=value.uncertainty,
            state=value.state,
            valid_time=TemporalRangeResponse.model_validate(
                {
                    "from": value.valid_from,
                    "to": value.valid_to,
                    "precision": value.valid_time_precision,
                    "original_expression": value.valid_time_original,
                    "timezone": value.valid_timezone,
                }
            ),
            system_time=SystemRangeResponse.model_validate(
                {"from": value.system_from, "to": value.system_to}
            ),
            confidence_band=value.confidence_band,
            pipeline_version=value.pipeline_version,
        )


class EvidenceResponse(ApiModel):
    id: uuid.UUID
    source_fragment_id: uuid.UUID
    relation: EvidenceRelation
    quote_start: int | None
    quote_end: int | None
    quote_hash: str
    extractor_reason: str
    strength_band: EvidenceStrength
    source_recorded_at: datetime | None

    @classmethod
    def from_domain(cls, value: EvidenceView) -> EvidenceResponse:
        return cls.model_validate(value, from_attributes=True)


class VerdictResponse(ApiModel):
    id: uuid.UUID
    target_derived_object_id: uuid.UUID
    sequence_no: int
    verdict: VerdictType
    correction_text: str | None
    reason: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, value: VerdictView) -> VerdictResponse:
        return cls.model_validate(value, from_attributes=True)


class MemoryDetailResponse(ApiModel):
    memory_id: uuid.UUID
    kind: MemoryClaimKind
    subject_entity_id: uuid.UUID | None
    version: ClaimVersionResponse
    history: list[ClaimVersionResponse]
    evidence: list[EvidenceResponse]
    counterevidence: list[EvidenceResponse]
    contextual_evidence: list[EvidenceResponse]
    verdicts: list[VerdictResponse]
    current_verdict: VerdictType | None
    etag: str
    allowed_uses: list[str]
    source_semantics: str

    @classmethod
    def from_domain(cls, value: MemoryDetail) -> MemoryDetailResponse:
        return cls(
            memory_id=value.memory_id,
            kind=value.kind,
            subject_entity_id=value.subject_entity_id,
            version=ClaimVersionResponse.from_domain(value.version),
            history=[ClaimVersionResponse.from_domain(item) for item in value.history],
            evidence=[EvidenceResponse.from_domain(item) for item in value.evidence],
            counterevidence=[EvidenceResponse.from_domain(item) for item in value.counterevidence],
            contextual_evidence=[
                EvidenceResponse.from_domain(item) for item in value.contextual_evidence
            ],
            verdicts=[VerdictResponse.from_domain(item) for item in value.verdicts],
            current_verdict=value.current_verdict,
            etag=value.etag,
            allowed_uses=list(value.allowed_uses),
            source_semantics=value.source_semantics,
        )


class InboxItemResponse(ApiModel):
    memory_id: uuid.UUID
    kind: MemoryClaimKind
    version: ClaimVersionResponse
    support_count: int
    counterevidence_count: int
    current_verdict: VerdictType | None
    etag: str

    @classmethod
    def from_domain(cls, value: InboxItem) -> InboxItemResponse:
        return cls(
            memory_id=value.memory_id,
            kind=value.kind,
            version=ClaimVersionResponse.from_domain(value.version),
            support_count=value.support_count,
            counterevidence_count=value.counterevidence_count,
            current_verdict=value.current_verdict,
            etag=value.etag,
        )


class InboxPageResponse(ApiModel):
    items: list[InboxItemResponse]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, value: InboxPage) -> InboxPageResponse:
        return cls(
            items=[InboxItemResponse.from_domain(item) for item in value.items],
            next_cursor=value.next_cursor,
        )


class VerdictRequest(ApiModel):
    verdict: VerdictType
    correction_text: str | None = Field(default=None, max_length=20_000)
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_correction_contract(self) -> VerdictRequest:
        correction = self.correction_text.strip() if self.correction_text else None
        if self.verdict is VerdictType.CORRECT and correction is None:
            raise ValueError("correction_text is required for a correct verdict")
        if self.verdict is not VerdictType.CORRECT and self.correction_text is not None:
            raise ValueError("correction_text is only accepted for a correct verdict")
        self.correction_text = correction
        return self


class VerdictOutcomeResponse(ApiModel):
    verdict_id: uuid.UUID
    memory_id: uuid.UUID
    target_derived_object_id: uuid.UUID
    current_derived_object_id: uuid.UUID
    state: LifecycleState
    version_no: int
    etag: str

    @classmethod
    def from_domain(cls, value: VerdictOutcome) -> VerdictOutcomeResponse:
        return cls.model_validate(value, from_attributes=True)


def _raise_problem(exc: Exception) -> NoReturn:
    if isinstance(exc, MemoryNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        code = "MEMORY_NOT_FOUND"
        title = "Memory not found"
        safe_detail = "The memory does not exist or is not available in this vault."
    elif isinstance(exc, RevisionConflictError):
        status_code = status.HTTP_409_CONFLICT
        code = "REVISION_CONFLICT"
        title = "Memory changed on another client"
        safe_detail = "Refresh the memory before applying this change."
    elif isinstance(exc, (InvalidLifecycleTransitionError, InvalidVerdictError)):
        status_code = status.HTTP_409_CONFLICT
        code = "INVALID_VERDICT_TRANSITION"
        title = "Verdict cannot be applied"
        safe_detail = "Refresh the memory and review its current state."
    elif isinstance(exc, InvalidTemporalIntervalError):
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        code = "INVALID_TEMPORAL_QUERY"
        title = "Memory time range is invalid"
        safe_detail = "Provide both real and system times using timezone-aware values."
    elif isinstance(exc, PolicyViolationError):
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        code = "MEMORY_POLICY_VIOLATION"
        title = "Memory request is not allowed"
        safe_detail = "This content cannot be handled as an ordinary memory."
    elif isinstance(exc, CorrectionSourceUnavailableError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        code = "SOURCE_INTEGRATION_UNAVAILABLE"
        title = "Correction is temporarily unavailable"
        safe_detail = "The correction could not be anchored to a source record."
    else:  # pragma: no cover - callers only pass the domain exceptions above
        raise exc
    raise HTTPException(
        status_code=status_code,
        detail={
            "type": f"https://product.example/problems/{code.lower().replace('_', '-')}",
            "title": title,
            "status": status_code,
            "code": code,
            "safe_detail": safe_detail,
        },
    ) from exc


def create_memory_router(
    *,
    get_service: Callable[..., AsyncMemoryOperations],
    get_vault_id: Callable[..., uuid.UUID],
    prefix: str = "/v1",
) -> APIRouter:
    """Create a router with request-scoped service and authenticated vault injection."""

    router = APIRouter(prefix=prefix, tags=["memories"])
    service_dependency = Depends(get_service)
    vault_dependency = Depends(get_vault_id)

    @router.get("/memory-inbox", response_model=InboxPageResponse)
    async def memory_inbox(
        service: AsyncMemoryOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> InboxPageResponse:
        try:
            return InboxPageResponse.from_domain(
                await service.list_inbox(vault_id=vault_id, limit=limit, cursor=cursor)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "INVALID_CURSOR", "safe_detail": "Invalid inbox cursor."},
            ) from exc

    @router.get("/memories/{memory_id}", response_model=MemoryDetailResponse)
    async def memory_detail(
        memory_id: uuid.UUID,
        response: Response,
        service: AsyncMemoryOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
        real_at: datetime | None = None,
        system_at: datetime | None = None,
    ) -> MemoryDetailResponse:
        try:
            detail = await service.get_detail(
                vault_id=vault_id,
                memory_id=memory_id,
                real_at=real_at,
                system_at=system_at,
            )
        except (
            InvalidTemporalIntervalError,
            MemoryNotFoundError,
            PolicyViolationError,
        ) as exc:
            _raise_problem(exc)
        response.headers["ETag"] = detail.etag
        return MemoryDetailResponse.from_domain(detail)

    @router.post(
        "/memories/{memory_id}/verdicts",
        response_model=VerdictOutcomeResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def submit_verdict(
        memory_id: uuid.UUID,
        payload: VerdictRequest,
        response: Response,
        if_match: str = Header(alias="If-Match"),
        service: AsyncMemoryOperations = service_dependency,
        vault_id: uuid.UUID = vault_dependency,
    ) -> VerdictOutcomeResponse:
        try:
            outcome = await service.record_verdict(
                vault_id=vault_id,
                memory_id=memory_id,
                verdict=payload.verdict,
                expected_etag=if_match,
                correction_text=payload.correction_text,
                reason=payload.reason,
            )
        except (
            CorrectionSourceUnavailableError,
            InvalidLifecycleTransitionError,
            InvalidVerdictError,
            MemoryNotFoundError,
            PolicyViolationError,
            RevisionConflictError,
        ) as exc:
            _raise_problem(exc)
        response.headers["ETag"] = outcome.etag
        return VerdictOutcomeResponse.from_domain(outcome)

    return router


__all__ = [
    "InboxPageResponse",
    "MemoryDetailResponse",
    "VerdictOutcomeResponse",
    "VerdictRequest",
    "create_memory_router",
]
