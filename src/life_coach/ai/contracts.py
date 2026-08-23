"""Provider-neutral contracts for AI extraction, memory, and retrieval pipelines.

The models in this module deliberately contain no provider SDK types and perform no
I/O.  They encode the trust boundary between user-authored Sources, rebuildable AI
candidates, evidence verification, policy gating, and short-lived context packs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SourceText = Annotated[str, StringConstraints(min_length=1)]


class ContractModel(BaseModel):
    """Base for fail-closed contracts used at model and pipeline boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        validate_default=True,
    )


class SensitivityLevel(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    HIGHLY_SENSITIVE = "highly_sensitive"

    @property
    def rank(self) -> int:
        return {
            SensitivityLevel.NORMAL: 0,
            SensitivityLevel.SENSITIVE: 1,
            SensitivityLevel.HIGHLY_SENSITIVE: 2,
        }[self]


class RetentionPolicy(StrEnum):
    ZERO_RETENTION = "zero_retention"
    TRANSIENT = "transient"
    SHORT_TERM = "short_term"
    PROVIDER_MANAGED = "provider_managed"

    @property
    def rank(self) -> int:
        return {
            RetentionPolicy.ZERO_RETENTION: 0,
            RetentionPolicy.TRANSIENT: 1,
            RetentionPolicy.SHORT_TERM: 2,
            RetentionPolicy.PROVIDER_MANAGED: 3,
        }[self]


class FallbackPolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    DEFER = "defer"
    USE_APPROVED_PROVIDER = "use_approved_provider"


class SourceKind(StrEnum):
    SOURCE = "source"
    ARTIFACT = "artifact"


class ClaimKind(StrEnum):
    EXPLICIT_FACT = "explicit_fact"
    PREFERENCE = "preference"
    EMOTION = "emotion"
    THOUGHT = "thought"
    CONCERN = "concern"
    IDEA = "idea"
    WISH = "wish"
    GOAL = "goal"
    COMMITMENT = "commitment"
    VALUE = "value"
    RELATIONSHIP = "relationship"
    SELF_DESCRIPTION = "self_description"
    PATTERN_HYPOTHESIS = "pattern_hypothesis"


class DerivationType(StrEnum):
    EXPLICIT = "explicit"
    PARAPHRASE = "paraphrase"
    INFERENCE = "inference"


class Attribution(StrEnum):
    SELF_REPORT = "self_report"
    QUOTED_OTHER = "quoted_other"
    IMPORTED_RECORD = "imported_record"
    MODEL_HYPOTHESIS = "model_hypothesis"


class ClaimSafetyFlag(StrEnum):
    ARTIFACT_SOURCE = "artifact_source"
    DIAGNOSTIC_LANGUAGE = "diagnostic_language"
    PERSONALITY_INFERENCE = "personality_inference"
    SAFETY_RISK_LABEL = "safety_risk_label"
    HIGHLY_SENSITIVE = "highly_sensitive"
    UNTRUSTED_INSTRUCTION = "untrusted_instruction"


class TimePrecision(StrEnum):
    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    RANGE = "range"
    UNKNOWN = "unknown"


class EntityKind(StrEnum):
    SELF = "self"
    PERSON = "person"
    ORGANIZATION = "organization"
    PLACE = "place"
    PROJECT = "project"
    CONCEPT = "concept"


class EntityResolutionState(StrEnum):
    UNRESOLVED = "unresolved"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    SPLIT = "split"


class EntityResolutionSignal(StrEnum):
    ALIAS = "alias"
    RELATIONSHIP = "relationship"
    TIME = "time"
    LOCAL_CONTEXT = "local_context"


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXTUALIZES = "contextualizes"


class EvidenceStrength(StrEnum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class EvidenceStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NEEDS_REVIEW = "needs_review"


class EvidenceIssueCode(StrEnum):
    MISSING_SPAN = "missing_span"
    SPAN_MISMATCH = "span_mismatch"
    ARTIFACT_IS_NOT_EVIDENCE = "artifact_is_not_evidence"
    SUBJECT_MISMATCH = "subject_mismatch"
    NEGATION_OMITTED = "negation_omitted"
    CONDITION_OMITTED = "condition_omitted"
    TIME_MISMATCH = "time_mismatch"
    ATTRIBUTION_MISMATCH = "attribution_mismatch"
    UNCERTAINTY_OMITTED = "uncertainty_omitted"
    SOURCE_UNAVAILABLE = "source_unavailable"
    COUNTEREVIDENCE_FOUND = "counterevidence_found"
    CLAIM_TEXT_MISMATCH = "claim_text_mismatch"
    MEMORY_REQUEST_MISMATCH = "memory_request_mismatch"
    SEMANTIC_REVIEW_REQUIRED = "semantic_review_required"


class MemoryDisposition(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    NON_PERSISTENT = "non_persistent"


class MemoryGateReason(StrEnum):
    EXPLICIT_LOW_SENSITIVITY = "explicit_low_sensitivity"
    EXPLICIT_MEMORY_REQUEST = "explicit_memory_request"
    USER_CONFIRMED = "user_confirmed"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_REJECTED = "evidence_rejected"
    ARTIFACT_SOURCE = "artifact_source"
    DIAGNOSTIC_LANGUAGE = "diagnostic_language"
    SAFETY_RISK_LABEL = "safety_risk_label"
    EPISODIC_ONLY = "episodic_only"
    WISH_IS_NOT_COMMITMENT = "wish_is_not_commitment"
    CONFIRMATION_REQUIRED = "confirmation_required"
    MULTI_SOURCE_CONFIRMATION_REQUIRED = "multi_source_confirmation_required"
    SENSITIVE_PERMISSION_REQUIRED = "sensitive_permission_required"
    HIGHLY_SENSITIVE = "highly_sensitive"
    POLICY_FORBIDS_STORAGE = "policy_forbids_storage"
    UNTRUSTED_INSTRUCTION = "untrusted_instruction"


class RetrievalIntent(StrEnum):
    RECENT_CONTEXT = "recent_context"
    FACT_LOOKUP = "fact_lookup"
    CURRENT_SELF_MODEL = "current_self_model"
    HISTORICAL_SELF_MODEL = "historical_self_model"
    PATTERN_REFLECTION = "pattern_reflection"
    ACTION_SUPPORT = "action_support"
    NARRATIVE_RESEARCH = "narrative_research"
    SOURCE_SEARCH = "source_search"


class RetrievalSignal(StrEnum):
    LEXICAL = "lexical"
    VECTOR = "vector"
    STRUCTURED = "structured"
    ENTITY = "entity"


class IndexPolicy(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    BOTH = "both"
    NONE = "none"


class ClaimLifecycleState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class ModelInputKind(StrEnum):
    SOURCE_REVISION = "source_revision"
    SOURCE_FRAGMENT = "source_fragment"
    DERIVED_OBJECT = "derived_object"


class SchemaRef(ContractModel):
    name: NonEmptyStr
    version: NonEmptyStr
    json_schema: dict[str, JsonValue] | None = None


class ModelTaskPolicy(ContractModel):
    task_type: NonEmptyStr
    required_capabilities: frozenset[NonEmptyStr] = frozenset()
    allowed_providers: frozenset[NonEmptyStr] = Field(min_length=1)
    data_residency: frozenset[NonEmptyStr] = Field(min_length=1)
    retention_policy: RetentionPolicy
    max_sensitivity: SensitivityLevel
    input_schema: SchemaRef
    output_schema: SchemaRef
    latency_budget_ms: int = Field(gt=0)
    cost_budget: Decimal = Field(ge=0)
    fallback_policy: FallbackPolicy = FallbackPolicy.FAIL_CLOSED


class ModelInputRef(ContractModel):
    vault_id: NonEmptyStr
    kind: ModelInputKind
    object_id: NonEmptyStr


class ModelRunSpec(ContractModel):
    run_id: NonEmptyStr
    vault_id: NonEmptyStr
    policy: ModelTaskPolicy
    provider: NonEmptyStr
    model: NonEmptyStr
    model_revision: NonEmptyStr
    prompt_template_version: NonEmptyStr
    schema_version: NonEmptyStr
    pipeline_version: NonEmptyStr
    consent_snapshot_id: NonEmptyStr
    policy_epoch: int = Field(ge=0)
    source_generation: int = Field(ge=0)
    actual_sensitivity: SensitivityLevel
    data_residency: NonEmptyStr
    retention_policy: RetentionPolicy
    input_refs: tuple[ModelInputRef, ...] = ()

    @model_validator(mode="after")
    def enforce_policy(self) -> Self:
        if self.provider not in self.policy.allowed_providers:
            raise ValueError("provider is not allowed by the task policy")
        if self.data_residency not in self.policy.data_residency:
            raise ValueError("data residency is not allowed by the task policy")
        if self.retention_policy.rank > self.policy.retention_policy.rank:
            raise ValueError("run retention exceeds the task policy")
        if self.schema_version != self.policy.output_schema.version:
            raise ValueError("run schema version differs from the output contract")
        if self.actual_sensitivity.rank > self.policy.max_sensitivity.rank:
            raise ValueError("input sensitivity exceeds the task policy")
        if any(ref.vault_id != self.vault_id for ref in self.input_refs):
            raise ValueError("all model input references must belong to the run vault")
        return self


class SourceSpan(ContractModel):
    vault_id: NonEmptyStr
    source_document_id: NonEmptyStr
    source_revision_id: NonEmptyStr
    source_fragment_id: NonEmptyStr
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quote: SourceText
    quote_hash: NonEmptyStr | None = None
    field_path: NonEmptyStr | None = None
    source_kind: SourceKind = SourceKind.SOURCE

    @model_validator(mode="after")
    def validate_offsets(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.char_end - self.char_start != len(self.quote):
            raise ValueError("source span offsets must exactly cover quote")
        return self


def _source_span_identity(
    span: SourceSpan,
) -> tuple[str, str, str, str, int, int, str, str | None, SourceKind]:
    return (
        span.vault_id,
        span.source_document_id,
        span.source_revision_id,
        span.source_fragment_id,
        span.char_start,
        span.char_end,
        span.quote,
        span.quote_hash,
        span.source_kind,
    )


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must include a timezone")
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be stored in UTC")
    return value


class TemporalRange(ContractModel):
    precision: TimePrecision
    original_expression: NonEmptyStr | None = None
    earliest: datetime | None = None
    latest: datetime | None = None
    is_relative: bool = False
    reference_timestamp: datetime | None = None
    timezone: NonEmptyStr | None = None

    @field_validator("earliest", "latest", "reference_timestamp")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None, info: object) -> datetime | None:
        field_name = getattr(info, "field_name", "timestamp")
        return _aware(value, field_name)

    @model_validator(mode="after")
    def preserve_precision(self) -> Self:
        if self.precision is TimePrecision.UNKNOWN:
            if self.earliest is not None or self.latest is not None:
                raise ValueError("unknown time precision cannot contain invented bounds")
            if self.original_expression is None:
                raise ValueError("unknown time precision must preserve its original expression")
            if self.timezone is None:
                raise ValueError("unknown time precision must preserve its historical timezone")
        elif self.earliest is None:
            raise ValueError("known time precision requires an earliest bound")
        if self.latest is not None and self.earliest is not None and self.latest <= self.earliest:
            raise ValueError("latest must be greater than earliest")
        if (
            self.precision
            in {
                TimePrecision.DAY,
                TimePrecision.MONTH,
                TimePrecision.YEAR,
                TimePrecision.RANGE,
            }
            and self.latest is None
        ):
            raise ValueError("imprecise calendar time requires half-open bounds")
        if (
            self.precision not in {TimePrecision.EXACT, TimePrecision.UNKNOWN}
            and self.original_expression is None
        ):
            raise ValueError("imprecise time must preserve its original expression")
        if (
            self.precision not in {TimePrecision.EXACT, TimePrecision.UNKNOWN}
            and self.timezone is None
        ):
            raise ValueError("imprecise time must preserve its historical timezone")
        if self.earliest is not None and self.latest is not None:
            duration = self.latest - self.earliest
            if self.precision is TimePrecision.DAY and not timedelta(
                hours=20
            ) <= duration <= timedelta(hours=28):
                raise ValueError("day precision must cover one local calendar day")
            if self.precision is TimePrecision.MONTH and not timedelta(
                days=27
            ) <= duration <= timedelta(days=32):
                raise ValueError("month precision must cover one local calendar month")
            if self.precision is TimePrecision.YEAR and not timedelta(
                days=364
            ) <= duration <= timedelta(days=367):
                raise ValueError("year precision must cover one local calendar year")
        if self.is_relative and (
            self.reference_timestamp is None
            or self.timezone is None
            or self.original_expression is None
        ):
            raise ValueError(
                "relative time requires reference timestamp, timezone, and original text"
            )
        return self


class ClaimTemporalContext(ContractModel):
    captured_at: datetime
    capture_timezone: NonEmptyStr
    event_time: TemporalRange | None = None
    valid_time: TemporalRange | None = None

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value: datetime) -> datetime:
        checked = _aware(value, "captured_at")
        assert checked is not None
        return checked


class EntityCandidate(ContractModel):
    mention: NonEmptyStr
    mention_span: SourceSpan
    kind: EntityKind
    canonical_label: NonEmptyStr | None = None
    entity_id: NonEmptyStr | None = None
    resolution_state: EntityResolutionState = EntityResolutionState.UNRESOLVED
    signals: frozenset[EntityResolutionSignal] = frozenset()
    confidence_reason: NonEmptyStr
    requires_user_confirmation: bool = True

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.mention != self.mention_span.quote:
            raise ValueError("entity mention must exactly match its Source span")
        if self.resolution_state is EntityResolutionState.CONFIRMED and self.entity_id is None:
            raise ValueError("a confirmed entity requires entity_id")
        if self.resolution_state is EntityResolutionState.PROPOSED and self.entity_id is None:
            raise ValueError("a proposed entity requires entity_id")
        if (
            self.resolution_state is EntityResolutionState.PROPOSED
            and not self.requires_user_confirmation
        ):
            raise ValueError("proposed entity resolution requires user confirmation")
        if (
            self.resolution_state is EntityResolutionState.CONFIRMED
            and self.requires_user_confirmation
        ):
            raise ValueError("confirmed entity resolution cannot still require confirmation")
        return self


_FORBIDDEN_PAYLOAD_KEYS = {
    "diagnosis",
    "diagnostic",
    "personality_disorder",
    "suicide_risk_score",
    "risk_score",
    "tool_call",
    "tool_calls",
    "function_call",
    "command",
}
_DIAGNOSTIC_TEXT = re.compile(
    r"(?:[我你他她](?:可能)?(?:得了|患有|患上)(?:抑郁症?|焦虑症?|创伤后应激障碍|"
    r"双相情感障碍|[^\s\uFF0C\u3002]{1,12}(?:症|障碍|精神疾病))|"
    r"你(?:可能)?(?:患有|有).{0,12}(?:症|障碍)|诊断为|人格障碍|"
    r"自杀风险(?:分数|等级)|\b(?:diagnos(?:is|ed)|personality disorder|"
    r"you (?:may |might )?have .{0,24}(?:disorder|depression|ptsd|bipolar)|"
    r"suicide risk score|DSM-5|ICD-11)\b)",
    re.IGNORECASE,
)
_SAFETY_RISK_TEXT = re.compile(r"(?:自杀风险(?:分数|等级)|\bsuicide risk (?:score|level)\b)", re.I)
_HIGHLY_SENSITIVE_TEXT = re.compile(
    r"(?:创伤|性侵|强奸|性行为|性生活|性取向|怀孕|流产|抑郁|焦虑症|"
    r"精神疾病|健康|医疗|疾病|病史|病历|住院|手术|治疗|用药|癌症|艾滋|"
    r"财务|负债|欠债|收入|工资|薪资|存款|资产|银行卡|账户余额|税务|信用|"
    r"犯罪|违法|吸毒|毒品|家暴|虐待|"
    r"\b(?:trauma|sexual assault|rape|sex life|sexual orientation|pregnan(?:t|cy)|"
    r"abortion|depression|mental illness|health|medical|disease|hospital|surgery|"
    r"medication|cancer|hiv|debt|salary|income|savings|asset|bank account|tax|"
    r"crime|illegal drug|domestic abuse)\b)",
    re.IGNORECASE,
)
_UNTRUSTED_INSTRUCTION_TEXT = re.compile(
    r"(?:忽略(?:系统|之前|以上).{0,12}(?:提示|指令)|调用工具|"
    r"\bignore (?:the )?(?:system|previous) (?:prompt|instructions?)\b|"
    r"\b(?:update_user_profile|tool_call|function_call)\b)",
    re.IGNORECASE,
)


def _find_forbidden_payload_key(value: JsonValue, path: str = "structured_payload") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.strip().lower()
            child_path = f"{path}.{key}"
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                return child_path
            found = _find_forbidden_payload_key(child, child_path)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_forbidden_payload_key(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


class CandidateClaim(ContractModel):
    candidate_id: NonEmptyStr
    vault_id: NonEmptyStr
    claim_kind: ClaimKind
    canonical_text: NonEmptyStr
    structured_payload: dict[str, JsonValue] = Field(default_factory=dict)
    derivation: DerivationType
    attribution: Attribution
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    temporal_context: ClaimTemporalContext | None = None
    entity_candidates: tuple[EntityCandidate, ...] = ()
    uncertainty_text: NonEmptyStr | None = None
    negated: bool = False
    conditional: bool = False
    explicit_memory_request: bool = False
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    safety_flags: frozenset[ClaimSafetyFlag] = frozenset()

    @model_validator(mode="after")
    def validate_claim_boundary(self) -> Self:
        if any(span.vault_id != self.vault_id for span in self.source_spans):
            raise ValueError("all source spans must belong to the claim vault")
        if any(entity.mention_span.vault_id != self.vault_id for entity in self.entity_candidates):
            raise ValueError("all entity mentions must belong to the claim vault")
        claim_span_keys = {_source_span_identity(span) for span in self.source_spans}
        if any(
            _source_span_identity(entity.mention_span) not in claim_span_keys
            for entity in self.entity_candidates
        ):
            raise ValueError("entity mention spans must also be claim Source spans")
        forbidden = _find_forbidden_payload_key(self.structured_payload)
        if forbidden is not None:
            raise ValueError(f"forbidden executable or diagnostic field: {forbidden}")
        uncovered_payload_fields = {
            f"structured_payload.{key}"
            for key in self.structured_payload
            if not any(span.field_path == f"structured_payload.{key}" for span in self.source_spans)
        }
        if uncovered_payload_fields:
            missing = ", ".join(sorted(uncovered_payload_fields))
            raise ValueError(f"structured payload fields require Source spans: {missing}")

        flags = set(self.safety_flags)
        all_spans = (
            *self.source_spans,
            *(entity.mention_span for entity in self.entity_candidates),
        )
        scan_text = "\n".join(
            (
                self.canonical_text,
                *(span.quote for span in all_spans),
                json.dumps(
                    self.structured_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        if any(span.source_kind is SourceKind.ARTIFACT for span in all_spans):
            flags.add(ClaimSafetyFlag.ARTIFACT_SOURCE)
        if self.sensitivity is SensitivityLevel.HIGHLY_SENSITIVE:
            flags.add(ClaimSafetyFlag.HIGHLY_SENSITIVE)
        if _HIGHLY_SENSITIVE_TEXT.search(scan_text):
            flags.add(ClaimSafetyFlag.HIGHLY_SENSITIVE)
        if _DIAGNOSTIC_TEXT.search(scan_text):
            flags.add(ClaimSafetyFlag.DIAGNOSTIC_LANGUAGE)
        if _SAFETY_RISK_TEXT.search(scan_text):
            flags.add(ClaimSafetyFlag.SAFETY_RISK_LABEL)
        if _UNTRUSTED_INSTRUCTION_TEXT.search(scan_text):
            flags.add(ClaimSafetyFlag.UNTRUSTED_INSTRUCTION)
        if self.derivation is DerivationType.INFERENCE and self.claim_kind in {
            ClaimKind.RELATIONSHIP,
            ClaimKind.SELF_DESCRIPTION,
            ClaimKind.PATTERN_HYPOTHESIS,
        }:
            flags.add(ClaimSafetyFlag.PERSONALITY_INFERENCE)
        object.__setattr__(self, "safety_flags", frozenset(flags))
        return self

    @property
    def uses_only_primary_sources(self) -> bool:
        return all(
            span.source_kind is SourceKind.SOURCE
            for span in (
                *self.source_spans,
                *(entity.mention_span for entity in self.entity_candidates),
            )
        )

    @property
    def evidence_fingerprint(self) -> str:
        payload = self.model_dump(
            mode="json",
            round_trip=True,
            exclude={"safety_flags", "entity_candidates"},
        )
        payload["safety_flags"] = sorted(flag.value for flag in self.safety_flags)
        payload["entity_candidates"] = [
            {
                **entity.model_dump(mode="json", round_trip=True, exclude={"signals"}),
                "signals": sorted(signal.value for signal in entity.signals),
            }
            for entity in self.entity_candidates
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class EvidenceItem(ContractModel):
    relation: EvidenceRelation
    source_span: SourceSpan
    strength: EvidenceStrength
    reason: NonEmptyStr


class EvidenceIssue(ContractModel):
    code: EvidenceIssueCode
    message: NonEmptyStr
    field_path: NonEmptyStr | None = None
    source_span: SourceSpan | None = None
    fatal: bool = True


class EvidenceVerificationResult(ContractModel):
    vault_id: NonEmptyStr
    candidate_id: NonEmptyStr
    claim_fingerprint: NonEmptyStr
    status: EvidenceStatus
    evidence: tuple[EvidenceItem, ...] = ()
    counterevidence: tuple[EvidenceItem, ...] = ()
    issues: tuple[EvidenceIssue, ...] = ()
    verifier_version: NonEmptyStr
    eligible_for_display: bool = False

    @model_validator(mode="after")
    def validate_verification(self) -> Self:
        items = (*self.evidence, *self.counterevidence)
        if any(item.source_span.vault_id != self.vault_id for item in items):
            raise ValueError("all evidence must belong to the verification vault")
        if any(item.relation is EvidenceRelation.CONTRADICTS for item in self.evidence):
            raise ValueError("contradicting evidence must be kept in counterevidence")
        if any(item.relation is not EvidenceRelation.CONTRADICTS for item in self.counterevidence):
            raise ValueError("counterevidence items must use the contradicts relation")
        valid_support = any(
            item.relation is EvidenceRelation.SUPPORTS
            and item.source_span.source_kind is SourceKind.SOURCE
            for item in self.evidence
        )
        if self.status is EvidenceStatus.SUPPORTED and not valid_support:
            raise ValueError("supported status requires primary Source evidence")
        if self.eligible_for_display and (
            self.status is not EvidenceStatus.SUPPORTED
            or not valid_support
            or any(issue.fatal for issue in self.issues)
        ):
            raise ValueError("display eligibility requires verified, issue-free Source evidence")
        return self

    @property
    def supported(self) -> bool:
        return self.status is EvidenceStatus.SUPPORTED


class MemoryGateInput(ContractModel):
    claim: CandidateClaim
    verification: EvidenceVerificationResult
    user_confirmed: bool = False
    policy_allows_storage: bool = True
    sensitive_storage_granted: bool = False
    highly_sensitive_storage_granted: bool = False
    proactive_use_granted: bool = False

    @model_validator(mode="after")
    def references_match(self) -> Self:
        if self.claim.vault_id != self.verification.vault_id:
            raise ValueError("claim and verification must belong to the same vault")
        if self.claim.candidate_id != self.verification.candidate_id:
            raise ValueError("verification does not belong to the candidate claim")
        if self.claim.evidence_fingerprint != self.verification.claim_fingerprint:
            raise ValueError("verification claim fingerprint does not match")
        return self


class MemoryGateDecision(ContractModel):
    disposition: MemoryDisposition
    reasons: tuple[MemoryGateReason, ...] = Field(min_length=1)
    requires_confirmation: bool
    may_use_proactively: bool

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.disposition is MemoryDisposition.NON_PERSISTENT and self.may_use_proactively:
            raise ValueError("non-persistent content cannot be used proactively")
        if self.disposition is MemoryDisposition.ACTIVE and self.requires_confirmation:
            raise ValueError("active content cannot still require confirmation")
        return self

    @property
    def decision(self) -> MemoryDisposition:
        return self.disposition


class TimeScope(ContractModel):
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None

    @field_validator("from_", "to")
    @classmethod
    def bounds_are_aware(cls, value: datetime | None, info: object) -> datetime | None:
        return _aware(value, getattr(info, "field_name", "time scope"))

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.from_ is not None and self.to is not None and self.to <= self.from_:
            raise ValueError("time scope must be a non-empty half-open range")
        return self


class RetrievalQuery(ContractModel):
    vault_id: NonEmptyStr
    text: NonEmptyStr
    purpose: RetrievalIntent
    query_embedding: tuple[float, ...] = ()
    time_scope: TimeScope | None = None
    as_of: datetime | None = None
    entity_ids: frozenset[NonEmptyStr] = frozenset()
    require_all_entities: bool = False
    claim_kinds: frozenset[ClaimKind] = frozenset()
    lifecycle_states: frozenset[ClaimLifecycleState] = frozenset({ClaimLifecycleState.ACTIVE})
    max_sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    include_candidates: bool = False
    limit: int = Field(default=20, gt=0, le=500)
    counterevidence_limit: int = Field(default=5, ge=0, le=500)
    search_counterevidence: bool | None = None

    @field_validator("query_embedding")
    @classmethod
    def finite_query_embedding(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(component) for component in value):
            raise ValueError("query embedding must contain only finite numbers")
        return value

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "as_of")

    @model_validator(mode="after")
    def pattern_reflection_requires_counterevidence_budget(self) -> Self:
        if self.purpose is RetrievalIntent.PATTERN_REFLECTION and (
            self.search_counterevidence is False or self.counterevidence_limit == 0
        ):
            raise ValueError("pattern reflection cannot disable its counterevidence pass")
        return self

    @property
    def intent(self) -> RetrievalIntent:
        return self.purpose


class RetrievalRecord(ContractModel):
    record_id: NonEmptyStr
    vault_id: NonEmptyStr
    text: NonEmptyStr
    source_span: SourceSpan
    embedding: tuple[float, ...] = ()
    lexical_terms: tuple[NonEmptyStr, ...] = ()
    entity_ids: frozenset[NonEmptyStr] = frozenset()
    claim_kind: ClaimKind | None = None
    lifecycle_state: ClaimLifecycleState = ClaimLifecycleState.ACTIVE
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    index_policy: IndexPolicy = IndexPolicy.BOTH
    deleted: bool = False
    consent_allowed: bool = True
    valid_time: TemporalRange | None = None
    recorded_at: datetime | None = None
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS
    evidence_strength: EvidenceStrength | None = None
    user_confirmed: bool = False
    contradiction_ids: tuple[NonEmptyStr, ...] = ()
    structured_score: float = Field(default=0, ge=0)
    structured_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("embedding")
    @classmethod
    def finite_embedding(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(component) for component in value):
            raise ValueError("embedding must contain only finite numbers")
        return value

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "recorded_at")

    @field_validator("structured_score")
    @classmethod
    def finite_structured_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("structured score must be finite")
        return value

    @model_validator(mode="after")
    def record_matches_source(self) -> Self:
        if self.source_span.vault_id != self.vault_id:
            raise ValueError("retrieval record and source span must share a vault")
        return self


class SignalRank(ContractModel):
    signal: RetrievalSignal
    rank: int = Field(gt=0)
    raw_score: float

    @field_validator("raw_score")
    @classmethod
    def finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value


class RankedRetrievalItem(ContractModel):
    record: RetrievalRecord
    signal_ranks: tuple[SignalRank, ...] = ()
    rrf_score: float = Field(ge=0)
    rerank_score: float | None = None
    matched_signals: frozenset[RetrievalSignal] = frozenset()

    @field_validator("rrf_score", "rerank_score")
    @classmethod
    def finite_rank_score(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("ranking score must be finite")
        return value


class HybridRetrievalResult(ContractModel):
    query: RetrievalQuery
    items: tuple[RankedRetrievalItem, ...] = ()
    counterevidence: tuple[RankedRetrievalItem, ...] = ()
    counterevidence_searched: bool
    excluded_count_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)
    coverage: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def prevent_cross_vault_results(self) -> Self:
        all_items = (*self.items, *self.counterevidence)
        if any(item.record.vault_id != self.query.vault_id for item in all_items):
            raise ValueError("hybrid retrieval result cannot cross vault boundaries")
        if any(count < 0 for count in self.excluded_count_by_reason.values()):
            raise ValueError("excluded counts cannot be negative")
        return self


class ContextPolicySnapshot(ContractModel):
    consent_snapshot_id: NonEmptyStr
    policy_epoch: int = Field(ge=0)
    max_sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    cross_record_analysis_allowed: bool = False
    sensitive_resurface_allowed: bool = False


class ContextClaim(ContractModel):
    claim: CandidateClaim
    verification: EvidenceVerificationResult
    decision: MemoryGateDecision

    @model_validator(mode="after")
    def preserve_verified_gate_chain(self) -> Self:
        if self.claim.vault_id != self.verification.vault_id:
            raise ValueError("context claim and verification must share a vault")
        if self.claim.candidate_id != self.verification.candidate_id:
            raise ValueError("context verification targets another candidate")
        if self.claim.evidence_fingerprint != self.verification.claim_fingerprint:
            raise ValueError("context verification fingerprint does not match")
        hard_flags = {
            ClaimSafetyFlag.ARTIFACT_SOURCE,
            ClaimSafetyFlag.DIAGNOSTIC_LANGUAGE,
            ClaimSafetyFlag.SAFETY_RISK_LABEL,
            ClaimSafetyFlag.UNTRUSTED_INSTRUCTION,
        }
        if self.claim.safety_flags & hard_flags:
            raise ValueError("policy-rejected claims cannot enter a ContextPack")
        if (
            self.verification.status is EvidenceStatus.UNSUPPORTED
            or not self.verification.evidence
            or any(issue.fatal for issue in self.verification.issues)
        ):
            raise ValueError("unsupported claims cannot enter a ContextPack")
        if self.decision.disposition is MemoryDisposition.NON_PERSISTENT:
            raise ValueError("non-persistent claims cannot enter a ContextPack")
        if self.decision.disposition is MemoryDisposition.ACTIVE:
            if (
                self.verification.status is not EvidenceStatus.SUPPORTED
                or not self.verification.eligible_for_display
                or self.verification.counterevidence
            ):
                raise ValueError("active context claims require display-eligible verified support")
            if (
                ClaimSafetyFlag.HIGHLY_SENSITIVE in self.claim.safety_flags
                or ClaimSafetyFlag.PERSONALITY_INFERENCE in self.claim.safety_flags
            ):
                raise ValueError("high-sensitivity or personality hypotheses cannot be active")
        elif not self.decision.requires_confirmation:
            raise ValueError("candidate context claims must remain reviewable")
        return self


class ContextPack(ContractModel):
    vault_id: NonEmptyStr
    version: NonEmptyStr
    purpose: RetrievalIntent
    policy_snapshot: ContextPolicySnapshot
    time_scope: TimeScope | None = None
    confirmed_claims: tuple[ContextClaim, ...] = ()
    candidate_claims: tuple[ContextClaim, ...] = ()
    source_quotes: tuple[SourceSpan, ...] = ()
    counterevidence: tuple[EvidenceItem, ...] = ()
    uncertainties: tuple[NonEmptyStr, ...] = ()
    excluded_count_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)
    coverage: dict[str, JsonValue] = Field(default_factory=dict)
    citation_map: dict[NonEmptyStr, tuple[SourceSpan, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_context_boundary(self) -> Self:
        if any(
            item.decision.disposition is not MemoryDisposition.ACTIVE
            for item in self.confirmed_claims
        ):
            raise ValueError("confirmed ContextPack claims must be active")
        if any(
            item.decision.disposition is not MemoryDisposition.CANDIDATE
            for item in self.candidate_claims
        ):
            raise ValueError("candidate ContextPack claims must remain candidate")
        context_claims = (*self.confirmed_claims, *self.candidate_claims)
        claims = tuple(item.claim for item in context_claims)
        direct_spans = list(self.source_quotes)
        direct_spans.extend(item.source_span for item in self.counterevidence)
        direct_spans.extend(
            span for citation_spans in self.citation_map.values() for span in citation_spans
        )
        if any(claim.vault_id != self.vault_id for claim in claims):
            raise ValueError("ContextPack claims cannot cross vault boundaries")
        if any(not claim.uses_only_primary_sources for claim in claims):
            raise ValueError("AI artifacts cannot enter a ContextPack as claims")
        if any(
            claim.sensitivity.rank > self.policy_snapshot.max_sensitivity.rank
            or (
                ClaimSafetyFlag.HIGHLY_SENSITIVE in claim.safety_flags
                and self.policy_snapshot.max_sensitivity is not SensitivityLevel.HIGHLY_SENSITIVE
            )
            for claim in claims
        ):
            raise ValueError("ContextPack claim exceeds the policy sensitivity ceiling")
        if not self.policy_snapshot.sensitive_resurface_allowed and any(
            claim.sensitivity is not SensitivityLevel.NORMAL
            or ClaimSafetyFlag.HIGHLY_SENSITIVE in claim.safety_flags
            for claim in claims
        ):
            raise ValueError("sensitive claims require a separate resurface grant")
        if (
            self.purpose
            in {
                RetrievalIntent.PATTERN_REFLECTION,
                RetrievalIntent.NARRATIVE_RESEARCH,
            }
            and not self.policy_snapshot.cross_record_analysis_allowed
        ):
            raise ValueError("purpose requires cross-record analysis consent")
        if any(span.vault_id != self.vault_id for span in direct_spans):
            raise ValueError("ContextPack evidence cannot cross vault boundaries")
        if any(span.source_kind is SourceKind.ARTIFACT for span in direct_spans):
            raise ValueError("AI artifacts cannot enter a ContextPack as Source evidence")
        quote_keys = {
            (
                span.source_document_id,
                span.source_revision_id,
                span.source_fragment_id,
                span.char_start,
                span.char_end,
                span.quote,
                span.quote_hash,
                span.source_kind,
            )
            for span in self.source_quotes
        }
        citation_spans = (span for spans in self.citation_map.values() for span in spans)
        if any(
            (
                span.source_document_id,
                span.source_revision_id,
                span.source_fragment_id,
                span.char_start,
                span.char_end,
                span.quote,
                span.quote_hash,
                span.source_kind,
            )
            not in quote_keys
            for span in citation_spans
        ):
            raise ValueError("citation_map can only reference included source quotes")
        if (
            self.purpose is RetrievalIntent.PATTERN_REFLECTION
            and self.coverage.get("counterevidence_searched") is not True
        ):
            raise ValueError("pattern reflection requires a counterevidence pass")
        if any(count < 0 for count in self.excluded_count_by_reason.values()):
            raise ValueError("excluded counts cannot be negative")
        return self


__all__ = [
    "Attribution",
    "CandidateClaim",
    "ClaimKind",
    "ClaimLifecycleState",
    "ClaimSafetyFlag",
    "ClaimTemporalContext",
    "ContextClaim",
    "ContextPack",
    "ContextPolicySnapshot",
    "ContractModel",
    "DerivationType",
    "EntityCandidate",
    "EntityKind",
    "EntityResolutionSignal",
    "EntityResolutionState",
    "EvidenceIssue",
    "EvidenceIssueCode",
    "EvidenceItem",
    "EvidenceRelation",
    "EvidenceStatus",
    "EvidenceStrength",
    "EvidenceVerificationResult",
    "FallbackPolicy",
    "HybridRetrievalResult",
    "IndexPolicy",
    "MemoryDisposition",
    "MemoryGateDecision",
    "MemoryGateInput",
    "MemoryGateReason",
    "ModelInputKind",
    "ModelInputRef",
    "ModelRunSpec",
    "ModelTaskPolicy",
    "RankedRetrievalItem",
    "RetentionPolicy",
    "RetrievalIntent",
    "RetrievalQuery",
    "RetrievalRecord",
    "RetrievalSignal",
    "SchemaRef",
    "SensitivityLevel",
    "SignalRank",
    "SourceKind",
    "SourceSpan",
    "TemporalRange",
    "TimePrecision",
    "TimeScope",
]
