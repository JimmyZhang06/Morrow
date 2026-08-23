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
    AuthorizationPurpose,
    ClaimVersionOrigin,
    ConfidenceBand,
    CorrectionMode,
    DataClass,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    SafetyDecision,
    SourceEvidenceStatus,
    TechnicalActor,
    ValidTimePrecision,
    VerdictType,
)

SOURCE_EVIDENCE_SEMANTICS = (
    "Source evidence establishes that the user recorded the material; "
    "it does not by itself establish objective truth."
)


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    """Untrusted extraction candidate; Source must verify every claimed span field."""

    source_fragment_id: uuid.UUID
    relation: EvidenceRelation
    quote_hash: str
    extractor_reason: EvidenceExtractionReason
    quote_start: int
    quote_end: int
    strength_band: EvidenceStrength = EvidenceStrength.MODERATE
    model_run_id: uuid.UUID | None = None
    created_by: TechnicalActor = TechnicalActor.KNOWLEDGE_PIPELINE


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceAnchor:
    """Authoritative Source result; only this shape may reach EvidenceLink."""

    vault_id: uuid.UUID
    source_document_id: uuid.UUID
    source_revision_id: uuid.UUID
    source_fragment_id: uuid.UUID
    relation: EvidenceRelation
    quote_start: int
    quote_end: int
    quote_hash: str
    extractor_reason: EvidenceExtractionReason
    strength_band: EvidenceStrength
    source_recorded_at: datetime
    source_data_class: DataClass
    source_content_fingerprint: str
    authorization_snapshot_id: uuid.UUID
    policy_epoch: int
    source_generation: int
    verified_at: datetime
    created_by: TechnicalActor
    model_run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class EvidenceSourceReference:
    evidence_id: uuid.UUID
    source_document_id: uuid.UUID
    source_revision_id: uuid.UUID
    source_fragment_id: uuid.UUID
    quote_start: int
    quote_end: int
    quote_hash: str
    authorization_snapshot_id: uuid.UUID
    policy_epoch: int
    source_generation: int


@dataclass(frozen=True, slots=True)
class EvidenceSourceState:
    evidence_id: uuid.UUID
    status: SourceEvidenceStatus
    authorization_snapshot_id: uuid.UUID
    policy_epoch: int
    source_generation: int
    data_class: DataClass
    checked_at: datetime


class EvidenceSourceVerifier(Protocol):
    """Port implemented by Source at its trusted plaintext/consent boundary."""

    def verify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        anchor: EvidenceAnchor,
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> VerifiedEvidenceAnchor: ...

    def resolve_current(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        references: tuple[EvidenceSourceReference, ...],
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> tuple[EvidenceSourceState, ...]: ...


@dataclass(frozen=True, slots=True)
class SourceStateChange:
    status: SourceEvidenceStatus
    occurred_at: datetime
    source_document_id: uuid.UUID | None = None
    source_revision_id: uuid.UUID | None = None
    source_fragment_id: uuid.UUID | None = None
    policy_epoch: int = 0
    source_generation: int = 0


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    assessment_id: uuid.UUID
    vault_id: uuid.UUID
    decision: SafetyDecision
    data_class: DataClass
    allows_proactive: bool


class MemorySafetyClassifier(Protocol):
    def classify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        texts: tuple[str, ...],
        at: datetime,
    ) -> SafetyAssessment: ...


@dataclass(frozen=True, slots=True)
class AuthorizationSnapshot:
    snapshot_id: uuid.UUID
    vault_id: uuid.UUID
    purpose: AuthorizationPurpose
    policy_epoch: int
    source_generation: int
    data_class: DataClass
    allows_read: bool
    allows_proactive: bool


class MemoryAuthorizationVerifier(Protocol):
    def authorize(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        purpose: AuthorizationPurpose,
        data_class: DataClass,
        policy_epoch: int,
        source_generation: int,
        at: datetime,
    ) -> AuthorizationSnapshot: ...


@dataclass(frozen=True, slots=True)
class VerifiedSubjectEntity:
    verification_id: uuid.UUID
    vault_id: uuid.UUID
    entity_id: uuid.UUID
    data_class: DataClass


class SubjectEntityVerifier(Protocol):
    def verify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        entity_id: uuid.UUID,
        at: datetime,
    ) -> VerifiedSubjectEntity: ...


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
    data_class: DataClass = DataClass.SENSITIVE
    created_by: TechnicalActor = TechnicalActor.KNOWLEDGE_PIPELINE
    evidence: tuple[EvidenceAnchor, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplacementValidTime:
    valid_from: datetime
    valid_to: datetime | None
    precision: ValidTimePrecision
    original_expression: str | None
    timezone: str | None


@dataclass(frozen=True, slots=True)
class CorrectionReplacement:
    statement: str
    mode: CorrectionMode
    valid_time: ReplacementValidTime | None = None
    uncertainty_text: str | None = None
    confidence_band: ConfidenceBand = ConfidenceBand.MEDIUM


@dataclass(frozen=True, slots=True)
class CorrectionSourceAnchor:
    source_fragment_id: uuid.UUID


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
    source_document_id: uuid.UUID
    source_revision_id: uuid.UUID
    source_fragment_id: uuid.UUID
    relation: EvidenceRelation
    quote_start: int | None
    quote_end: int | None
    quote_hash: str
    extractor_reason: EvidenceExtractionReason
    strength_band: EvidenceStrength
    source_recorded_at: datetime | None
    source_data_class: DataClass
    normalized_fingerprint: str
    authorization_snapshot_id: uuid.UUID
    policy_epoch: int
    source_generation: int


@dataclass(frozen=True, slots=True)
class VerdictView:
    id: uuid.UUID
    target_derived_object_id: uuid.UUID
    sequence_no: int
    verdict: VerdictType
    correction_text: str | None
    replacement: CorrectionReplacement | None
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
    data_class: DataClass
    origin: ClaimVersionOrigin
    correction_mode: CorrectionMode | None
    supersedes_derived_object_id: uuid.UUID | None
    origin_verdict_id: uuid.UUID | None
    normalized_fingerprint: str


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
    governance_verdict: VerdictType | None
    data_class: DataClass
    authorization_snapshot: AuthorizationSnapshot | None
    is_current: bool
    etag: str | None
    snapshot_token: str | None
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
