"""Framework-neutral command and view contracts for the Knowledge domain."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from .enums import (
    Attribution,
    ConfidenceBand,
    DataClass,
    EpistemicType,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    ValidTimePrecision,
    VerdictType,
)

SOURCE_EVIDENCE_SEMANTICS = (
    "Source evidence establishes that the user recorded the material; "
    "it does not by itself establish objective truth."
)


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    source_fragment_id: uuid.UUID
    relation: EvidenceRelation
    quote_hash: str
    extractor_reason: str
    strength_band: EvidenceStrength = EvidenceStrength.MODERATE
    quote_start: int | None = None
    quote_end: int | None = None
    source_recorded_at: datetime | None = None
    model_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ClaimProposal:
    kind: MemoryClaimKind
    canonical_text: str
    epistemic_type: EpistemicType
    attribution: Attribution
    valid_from: datetime
    structured_payload: dict[str, Any] = field(default_factory=dict)
    uncertainty_text: str | None = None
    valid_to: datetime | None = None
    valid_time_precision: ValidTimePrecision = ValidTimePrecision.UNKNOWN
    valid_time_original: str | None = None
    valid_timezone: str | None = None
    confidence_band: ConfidenceBand = ConfidenceBand.MEDIUM
    pipeline_version: str = "manual-v1"
    model_run_id: uuid.UUID | None = None
    subject_entity_id: uuid.UUID | None = None
    data_class: DataClass = DataClass.NORMAL
    created_by: str = "knowledge-pipeline"
    evidence: tuple[EvidenceAnchor, ...] = ()


@dataclass(frozen=True, slots=True)
class CorrectionSourceAnchor:
    source_fragment_id: uuid.UUID
    recorded_at: datetime


class CorrectionSourceRecorder(Protocol):
    """Port implemented by Source so correction Source + ClaimVersion share a UoW."""

    def record_correction(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        correction_text: str,
        data_class: DataClass,
        recorded_at: datetime,
    ) -> CorrectionSourceAnchor: ...


@dataclass(frozen=True, slots=True)
class EvidenceView:
    id: uuid.UUID
    source_fragment_id: uuid.UUID
    relation: EvidenceRelation
    quote_start: int | None
    quote_end: int | None
    quote_hash: str
    extractor_reason: str
    strength_band: EvidenceStrength
    source_recorded_at: datetime | None


@dataclass(frozen=True, slots=True)
class VerdictView:
    id: uuid.UUID
    target_derived_object_id: uuid.UUID
    sequence_no: int
    verdict: VerdictType
    correction_text: str | None
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimVersionView:
    derived_object_id: uuid.UUID
    version_no: int
    statement: str
    structured_payload: dict[str, Any]
    epistemic_type: EpistemicType
    attribution: Attribution
    uncertainty: str | None
    state: LifecycleState
    valid_from: datetime
    valid_to: datetime | None
    valid_time_precision: ValidTimePrecision
    valid_time_original: str | None
    valid_timezone: str | None
    system_from: datetime
    system_to: datetime | None
    confidence_band: ConfidenceBand
    pipeline_version: str


@dataclass(frozen=True, slots=True)
class MemoryDetail:
    memory_id: uuid.UUID
    kind: MemoryClaimKind
    subject_entity_id: uuid.UUID | None
    version: ClaimVersionView
    history: tuple[ClaimVersionView, ...]
    evidence: tuple[EvidenceView, ...]
    counterevidence: tuple[EvidenceView, ...]
    contextual_evidence: tuple[EvidenceView, ...]
    verdicts: tuple[VerdictView, ...]
    current_verdict: VerdictType | None
    etag: str
    allowed_uses: tuple[str, ...]
    source_semantics: str = SOURCE_EVIDENCE_SEMANTICS


@dataclass(frozen=True, slots=True)
class InboxItem:
    memory_id: uuid.UUID
    kind: MemoryClaimKind
    version: ClaimVersionView
    support_count: int
    counterevidence_count: int
    current_verdict: VerdictType | None
    etag: str


@dataclass(frozen=True, slots=True)
class InboxPage:
    items: tuple[InboxItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class VerdictOutcome:
    verdict_id: uuid.UUID
    memory_id: uuid.UUID
    target_derived_object_id: uuid.UUID
    current_derived_object_id: uuid.UUID
    state: LifecycleState
    version_no: int
    etag: str
