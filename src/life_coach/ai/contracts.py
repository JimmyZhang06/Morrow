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
import unicodedata
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
TechnicalId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    ),
]
VersionId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    ),
]
Sha256Hex = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class ContractModel(BaseModel):
    """Base for fail-closed contracts used at model and pipeline boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        revalidate_instances="always",
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
    SOURCE_IDENTITY_CONFLICT = "source_identity_conflict"
    SOURCE_GENERATION_MISMATCH = "source_generation_mismatch"
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
    MODEL_ORIGIN_POLICY_REJECTED = "model_origin_policy_rejected"


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


class CitationObjectType(StrEnum):
    RETRIEVAL_RECORD = "retrieval_record"
    CANDIDATE_CLAIM = "candidate_claim"


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


class AuthorizationPurpose(StrEnum):
    MEMORY_INGESTION = "memory_ingestion"


class SchemaRef(ContractModel):
    name: TechnicalId
    version: VersionId
    json_schema: dict[str, JsonValue] | None = None


class ModelTaskPolicy(ContractModel):
    task_type: TechnicalId
    required_capabilities: frozenset[TechnicalId] = frozenset()
    allowed_providers: frozenset[TechnicalId] = Field(min_length=1)
    data_residency: frozenset[TechnicalId] = Field(min_length=1)
    retention_policy: RetentionPolicy
    max_sensitivity: SensitivityLevel
    input_schema: SchemaRef
    output_schema: SchemaRef
    latency_budget_ms: int = Field(gt=0)
    cost_budget: Decimal = Field(ge=0)
    fallback_policy: FallbackPolicy = FallbackPolicy.FAIL_CLOSED


class ModelInputRef(ContractModel):
    vault_id: TechnicalId
    kind: ModelInputKind
    object_id: TechnicalId


class ModelRunSpec(ContractModel):
    run_id: TechnicalId
    vault_id: TechnicalId
    policy: ModelTaskPolicy
    provider: TechnicalId
    model: TechnicalId
    model_revision: VersionId
    prompt_template_version: VersionId
    schema_version: VersionId
    pipeline_version: VersionId
    consent_snapshot_id: TechnicalId
    policy_epoch: int = Field(ge=0)
    source_generation: int = Field(ge=0)
    actual_sensitivity: SensitivityLevel
    data_residency: TechnicalId
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
    vault_id: TechnicalId
    source_document_id: TechnicalId
    source_revision_id: TechnicalId
    source_fragment_id: TechnicalId
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quote: SourceText
    quote_hash: Sha256Hex
    field_path: NonEmptyStr | None = None
    source_kind: SourceKind = SourceKind.SOURCE

    @model_validator(mode="after")
    def validate_offsets(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.char_end - self.char_start != len(self.quote):
            raise ValueError("source span offsets must exactly cover quote")
        expected_hash = hashlib.sha256(self.quote.encode("utf-8")).hexdigest()
        if self.quote_hash != expected_hash:
            raise ValueError("quote_hash must be the SHA-256 digest of quote")
        return self


_SourceSpanIdentity = tuple[str, str, str, str, int, int, str, str, SourceKind]


def _source_span_identity(span: SourceSpan) -> _SourceSpanIdentity:
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


def _source_anchor(span: SourceSpan) -> tuple[str, str, str, str, int, int]:
    return (
        span.vault_id,
        span.source_document_id,
        span.source_revision_id,
        span.source_fragment_id,
        span.char_start,
        span.char_end,
    )


def _ensure_no_source_identity_conflicts(spans: tuple[SourceSpan, ...]) -> None:
    seen: dict[tuple[str, str, str, str, int, int], tuple[str, str, SourceKind]] = {}
    for span in spans:
        content_identity = (span.quote, span.quote_hash, span.source_kind)
        previous = seen.setdefault(_source_anchor(span), content_identity)
        if previous != content_identity:
            raise ValueError("duplicate Source identity has conflicting text, hash, or kind")


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must include a timezone")
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be stored in UTC")
    return value


def _canonical_fingerprint_value(value: object) -> JsonValue:
    """Convert Python-mode contract data into deterministic canonical JSON."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _canonical_fingerprint_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (set, frozenset)):
        children = [_canonical_fingerprint_value(child) for child in value]
        return sorted(
            children,
            key=lambda child: json.dumps(
                child,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_fingerprint_value(child) for child in value]
    raise TypeError(f"unsupported fingerprint value type: {type(value).__name__}")


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
    entity_id: TechnicalId | None = None
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
_FORBIDDEN_PAYLOAD_TOKENS = frozenset(
    "".join(character for character in item if character.isalnum())
    for item in _FORBIDDEN_PAYLOAD_KEYS
)
_DIAGNOSTIC_TEXT = re.compile(
    r"(?:[我你他她](?:可能)?(?:得了|患有|患上)(?:抑郁症?|焦虑症?|创伤后应激障碍|"
    r"双相情感障碍|[^\s\uFF0C\u3002]{1,12}(?:症|障碍|精神疾病))|"
    r"你(?:可能)?(?:患有|有).{0,12}(?:症|障碍)|"
    r"(?:初步|临床)?诊断(?:考虑|倾向|为|是)?|确诊|"
    r"(?:医生|精神科医生).{0,12}(?:说|认为).{0,8}(?:有|患有)|"
    r"符合.{0,16}(?:诊断|标准)|人格障碍|"
    r"自杀风险(?:分数|等级)|\b(?:diagnos(?:is|ed)|personality disorder|"
    r"(?:i|you|he|she|they) (?:may |might )?have .{0,24}(?:disorder|depression|ptsd|bipolar)|"
    r"(?:this )?(?:presentation|clinical picture|symptoms?)\s+(?:is|are)\s+consistent with\s+"
    r"(?:mdd|ptsd|depression|bipolar|.{1,24} disorder)|"
    r"clinically,?\s+this is\s+(?:mdd|ptsd|depression|bipolar|.{1,24} disorder)|"
    r"meets (?:the )?(?:diagnostic )?criteria for|clinical impression|"
    r"suicide risk score|DSM-5|ICD-11)\b)",
    re.IGNORECASE,
)
_SAFETY_RISK_TEXT = re.compile(
    r"(?:自杀(?:风险|倾向|高危)|自伤风险|"
    r"\b(?:suicidal|suicide risk|risk of suicide|self-harm risk)\b)",
    re.IGNORECASE,
)
_PERSONALITY_TEXT = re.compile(
    r"(?:回避型(?:人格)?|焦虑型依恋|依恋类型|人格类型|内向型人格|外向型人格|"
    r"自恋型人格|边缘型人格|"
    r"\b(?:avoidant personality|attachment style|personality type|narcissistic personality|"
    r"borderline personality)\b)",
    re.IGNORECASE,
)
_MODEL_ASSERTED_PERSONALITY_TEXT = re.compile(
    r"(?:你(?:就是|是|属于|表现为|符合).{0,16}"
    r"(?:回避型|焦虑型|自恋型|边缘型|人格|依恋)|"
    r"\byou (?:are|have|present as|fit|meet).{0,24}"
    r"(?:avoidant|anxious attachment|narcissistic|borderline|personality)\b)",
    re.IGNORECASE,
)
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
            normalized = "".join(
                character
                for character in unicodedata.normalize("NFKC", key).casefold()
                if character.isalnum()
            )
            child_path = f"{path}.{key}"
            if normalized in _FORBIDDEN_PAYLOAD_TOKENS:
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


def _normalize_safety_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if unicodedata.category(character) != "Cf")


class CandidateClaim(ContractModel):
    candidate_id: TechnicalId
    vault_id: TechnicalId
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
        _ensure_no_source_identity_conflicts(tuple(all_spans))
        # Treat every model-controlled textual field and every Source span as
        # untrusted data.  Scanning only canonical_text (or only a selected
        # evidence quote) lets an injected instruction hide in another field.
        scan_text = _normalize_safety_text(
            json.dumps(
                self.model_dump(mode="json", round_trip=True, exclude={"safety_flags"}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
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
        if _PERSONALITY_TEXT.search(scan_text):
            flags.add(ClaimSafetyFlag.PERSONALITY_INFERENCE)
        if _MODEL_ASSERTED_PERSONALITY_TEXT.search(scan_text):
            # A model assertion remains diagnostic/personality output even if
            # it lies about claim_kind, derivation, or attribution.
            flags.add(ClaimSafetyFlag.DIAGNOSTIC_LANGUAGE)
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
    vault_id: TechnicalId
    candidate_id: TechnicalId
    claim_fingerprint: Sha256Hex
    source_generation: int = Field(ge=0)
    status: EvidenceStatus
    evidence: tuple[EvidenceItem, ...] = ()
    counterevidence: tuple[EvidenceItem, ...] = ()
    issues: tuple[EvidenceIssue, ...] = ()
    verifier_version: VersionId
    eligible_for_display: bool = False

    @model_validator(mode="after")
    def validate_verification(self) -> Self:
        items = (*self.evidence, *self.counterevidence)
        _ensure_no_source_identity_conflicts(tuple(item.source_span for item in items))
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

    @property
    def evidence_fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json", round_trip=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class MemoryAuthorizationSnapshot(ContractModel):
    vault_id: TechnicalId
    purpose: AuthorizationPurpose = AuthorizationPurpose.MEMORY_INGESTION
    consent_snapshot_id: TechnicalId
    policy_epoch: int = Field(ge=0)
    source_generation: int = Field(ge=0)
    issued_at: datetime
    expires_at: datetime
    storage_allowed: bool = False
    sensitive_storage_allowed: bool = False
    highly_sensitive_storage_allowed: bool = False
    proactive_use_allowed: bool = False

    @field_validator("issued_at", "expires_at")
    @classmethod
    def expiry_is_aware_utc(cls, value: datetime) -> datetime:
        checked = _aware(value, "expires_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_authorization_window_and_grants(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization expiry must be after issuance")
        if not self.storage_allowed and any(
            (
                self.sensitive_storage_allowed,
                self.highly_sensitive_storage_allowed,
                self.proactive_use_allowed,
            )
        ):
            raise ValueError("derived grants require base storage authorization")
        if self.highly_sensitive_storage_allowed and not self.sensitive_storage_allowed:
            raise ValueError("highly-sensitive storage requires sensitive storage authorization")
        return self

    @property
    def authorization_fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json", round_trip=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class MemoryGateInput(ContractModel):
    claim: CandidateClaim
    verification: EvidenceVerificationResult
    authorization: MemoryAuthorizationSnapshot
    evaluated_at: datetime
    confirmation_verdict_id: TechnicalId | None = None

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware_utc(cls, value: datetime) -> datetime:
        checked = _aware(value, "evaluated_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def references_match(self) -> Self:
        if self.claim.vault_id != self.verification.vault_id:
            raise ValueError("claim and verification must belong to the same vault")
        if self.claim.candidate_id != self.verification.candidate_id:
            raise ValueError("verification does not belong to the candidate claim")
        if self.claim.evidence_fingerprint != self.verification.claim_fingerprint:
            raise ValueError("verification claim fingerprint does not match")
        if self.authorization.vault_id != self.claim.vault_id:
            raise ValueError("authorization snapshot does not belong to the claim vault")
        if self.verification.source_generation != self.authorization.source_generation:
            raise ValueError("verification does not belong to the authorized Source generation")
        if not self.authorization.issued_at <= self.evaluated_at < self.authorization.expires_at:
            raise ValueError("authorization snapshot is expired")
        return self


class MemoryGateDecision(ContractModel):
    candidate_id: TechnicalId
    claim_fingerprint: Sha256Hex
    evidence_fingerprint: Sha256Hex
    authorization: MemoryAuthorizationSnapshot
    authorization_fingerprint: Sha256Hex
    confirmation_verdict_id: TechnicalId | None = None
    evaluated_at: datetime
    disposition: MemoryDisposition
    reasons: tuple[MemoryGateReason, ...] = Field(min_length=1)
    requires_confirmation: bool
    may_use_proactively: bool

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware_utc(cls, value: datetime) -> datetime:
        checked = _aware(value, "evaluated_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.authorization.authorization_fingerprint != self.authorization_fingerprint:
            raise ValueError("decision authorization fingerprint does not match its snapshot")
        if not self.authorization.issued_at <= self.evaluated_at < self.authorization.expires_at:
            raise ValueError("decision was evaluated outside its authorization window")
        if self.may_use_proactively and not self.authorization.proactive_use_allowed:
            raise ValueError("decision exceeds proactive-use authorization")
        if MemoryGateReason.USER_CONFIRMED in self.reasons and self.confirmation_verdict_id is None:
            raise ValueError("user-confirmed decision requires a confirmation verdict id")
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
    vault_id: TechnicalId
    text: NonEmptyStr
    purpose: RetrievalIntent
    consent_snapshot_id: TechnicalId
    policy_epoch: int = Field(ge=0)
    source_generation: int = Field(ge=0)
    query_embedding: tuple[float, ...] = ()
    time_scope: TimeScope | None = None
    as_of: datetime | None = None
    entity_ids: frozenset[TechnicalId] = frozenset()
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
    record_id: TechnicalId
    vault_id: TechnicalId
    source_generation: int = Field(ge=0)
    text: NonEmptyStr
    source_span: SourceSpan
    embedding: tuple[float, ...] = ()
    lexical_terms: tuple[NonEmptyStr, ...] = ()
    entity_ids: frozenset[TechnicalId] = frozenset()
    claim_kind: ClaimKind | None = None
    lifecycle_state: ClaimLifecycleState = ClaimLifecycleState.ACTIVE
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    index_policy: IndexPolicy = IndexPolicy.BOTH
    deleted: bool = False
    consent_allowed: bool = False
    valid_time: TemporalRange | None = None
    recorded_at: datetime | None = None
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS
    evidence_strength: EvidenceStrength | None = None
    confirmation_verdict_id: TechnicalId | None = None
    contradiction_ids: tuple[TechnicalId, ...] = ()
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
        if self.relation is EvidenceRelation.CONTRADICTS and not self.contradiction_ids:
            raise ValueError("contradicting records must explicitly link a main record")
        if self.relation is not EvidenceRelation.CONTRADICTS and self.contradiction_ids:
            raise ValueError("only contradicting records may carry contradiction links")
        return self

    @property
    def user_confirmed(self) -> bool:
        return self.confirmation_verdict_id is not None


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
        _ensure_no_source_identity_conflicts(tuple(item.record.source_span for item in all_items))
        if any(item.record.vault_id != self.query.vault_id for item in all_items):
            raise ValueError("hybrid retrieval result cannot cross vault boundaries")
        if any(item.record.source_generation != self.query.source_generation for item in all_items):
            raise ValueError("hybrid retrieval result uses a stale Source generation")
        if any(
            not item.record.consent_allowed
            or item.record.deleted
            or item.record.index_policy is IndexPolicy.NONE
            or item.record.source_span.source_kind is SourceKind.ARTIFACT
            or item.record.sensitivity.rank > self.query.max_sensitivity.rank
            for item in all_items
        ):
            raise ValueError("hybrid retrieval result contains a policy-ineligible record")
        if any(count < 0 for count in self.excluded_count_by_reason.values()):
            raise ValueError("excluded counts cannot be negative")
        record_ids = [item.record.record_id for item in all_items]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("hybrid retrieval results cannot contain duplicate record ids")
        main_ids = {item.record.record_id for item in self.items}
        if any(item.record.relation is EvidenceRelation.CONTRADICTS for item in self.items):
            raise ValueError("contradicting records must remain in the counterevidence pass")
        if any(
            item.record.relation is not EvidenceRelation.CONTRADICTS
            or not set(item.record.contradiction_ids) & main_ids
            for item in self.counterevidence
        ):
            raise ValueError("counterevidence must explicitly link a returned main record")
        if self.counterevidence and not self.counterevidence_searched:
            raise ValueError("counterevidence cannot appear without an independent search pass")
        return self

    @property
    def retrieval_fingerprint(self) -> str:
        """Canonical digest binding context to this exact retrieval result."""

        encoded = json.dumps(
            _canonical_fingerprint_value(self.model_dump(mode="python", round_trip=True)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ContextPolicySnapshot(ContractModel):
    vault_id: TechnicalId
    purpose: RetrievalIntent
    consent_snapshot_id: TechnicalId
    policy_epoch: int = Field(ge=0)
    source_generation: int = Field(ge=0)
    issued_at: datetime
    expires_at: datetime
    max_sensitivity: SensitivityLevel = SensitivityLevel.NORMAL
    cross_record_analysis_allowed: bool = False
    sensitive_resurface_allowed: bool = False

    @field_validator("issued_at", "expires_at")
    @classmethod
    def expiry_is_aware_utc(cls, value: datetime) -> datetime:
        checked = _aware(value, "expires_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def expiry_follows_issuance(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("context policy expiry must be after issuance")
        return self

    @property
    def policy_fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json", round_trip=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ContextClaim(ContractModel):
    claim: CandidateClaim
    verification: EvidenceVerificationResult
    decision: MemoryGateDecision
    retrieval_fingerprint: Sha256Hex

    @model_validator(mode="after")
    def preserve_verified_gate_chain(self) -> Self:
        if self.claim.vault_id != self.verification.vault_id:
            raise ValueError("context claim and verification must share a vault")
        if self.claim.candidate_id != self.verification.candidate_id:
            raise ValueError("context verification targets another candidate")
        if self.claim.evidence_fingerprint != self.verification.claim_fingerprint:
            raise ValueError("context verification fingerprint does not match")
        if self.decision.candidate_id != self.claim.candidate_id:
            raise ValueError("context decision targets another candidate")
        if self.decision.claim_fingerprint != self.claim.evidence_fingerprint:
            raise ValueError("context decision claim fingerprint does not match")
        if self.decision.evidence_fingerprint != self.verification.evidence_fingerprint:
            raise ValueError("context decision evidence fingerprint does not match")
        if self.decision.authorization.vault_id != self.claim.vault_id:
            raise ValueError("context decision authorization belongs to another vault")
        # Fingerprints prevent accidental swapping; deterministic recomputation
        # also prevents a caller from copying the real fingerprints into a
        # hand-made ACTIVE decision for content the gate left as CANDIDATE.
        from .memory import decide_memory

        expected_decision = decide_memory(
            MemoryGateInput(
                claim=self.claim,
                verification=self.verification,
                authorization=self.decision.authorization,
                evaluated_at=self.decision.evaluated_at,
                confirmation_verdict_id=self.decision.confirmation_verdict_id,
            )
        )
        if self.decision != expected_decision:
            raise ValueError("context decision does not match the deterministic MemoryGate verdict")
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


class ContextSourceQuote(ContractModel):
    record_id: TechnicalId
    source_generation: int = Field(ge=0)
    source_span: SourceSpan
    sensitivity: SensitivityLevel
    retrieval_fingerprint: Sha256Hex


class ContextCounterevidence(ContractModel):
    record_id: TechnicalId
    source_generation: int = Field(ge=0)
    evidence: EvidenceItem
    contradiction_ids: tuple[TechnicalId, ...] = Field(min_length=1)
    sensitivity: SensitivityLevel
    retrieval_fingerprint: Sha256Hex

    @model_validator(mode="after")
    def preserve_counterevidence_relation(self) -> Self:
        if self.evidence.relation is not EvidenceRelation.CONTRADICTS:
            raise ValueError("Context counterevidence must use the contradicts relation")
        return self


class CitationTarget(ContractModel):
    object_type: CitationObjectType
    object_id: TechnicalId


class CitationBinding(ContractModel):
    target: CitationTarget
    retrieval_record_id: TechnicalId
    source_span: SourceSpan
    retrieval_fingerprint: Sha256Hex
    verification_fingerprint: Sha256Hex | None = None

    @model_validator(mode="after")
    def require_verifier_binding_for_claims(self) -> Self:
        if self.target.object_type is CitationObjectType.RETRIEVAL_RECORD:
            if self.target.object_id != self.retrieval_record_id:
                raise ValueError("record citation target must equal its retrieval record id")
            if self.verification_fingerprint is not None:
                raise ValueError("record citations cannot claim a verifier fingerprint")
        elif self.verification_fingerprint is None:
            raise ValueError("claim citations require a verification fingerprint")
        return self


class ContextPack(ContractModel):
    vault_id: TechnicalId
    version: VersionId
    purpose: RetrievalIntent
    policy_snapshot: ContextPolicySnapshot
    policy_fingerprint: Sha256Hex
    retrieval_fingerprint: Sha256Hex
    created_at: datetime
    time_scope: TimeScope | None = None
    confirmed_claims: tuple[ContextClaim, ...] = ()
    candidate_claims: tuple[ContextClaim, ...] = ()
    source_quotes: tuple[ContextSourceQuote, ...] = ()
    counterevidence: tuple[ContextCounterevidence, ...] = ()
    uncertainties: tuple[NonEmptyStr, ...] = ()
    excluded_count_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)
    coverage: dict[str, JsonValue] = Field(default_factory=dict)
    citation_map: tuple[CitationBinding, ...] = ()

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        checked = _aware(value, "created_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def enforce_context_boundary(self) -> Self:
        if self.policy_snapshot.vault_id != self.vault_id:
            raise ValueError("ContextPack and policy snapshot must share a vault")
        if self.policy_snapshot.purpose is not self.purpose:
            raise ValueError("ContextPack and policy snapshot must share a purpose")
        if self.policy_snapshot.policy_fingerprint != self.policy_fingerprint:
            raise ValueError("ContextPack policy fingerprint does not match its snapshot")
        if not self.policy_snapshot.issued_at <= self.created_at < self.policy_snapshot.expires_at:
            raise ValueError("ContextPack policy snapshot is expired")
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
        if any(item.retrieval_fingerprint != self.retrieval_fingerprint for item in context_claims):
            raise ValueError("ContextPack claim belongs to another retrieval result")
        if any(
            item.verification.source_generation != self.policy_snapshot.source_generation
            for item in context_claims
        ):
            raise ValueError("ContextPack claim evidence uses a stale Source generation")
        direct_spans = [quote.source_span for quote in self.source_quotes]
        direct_spans.extend(item.evidence.source_span for item in self.counterevidence)
        direct_spans.extend(binding.source_span for binding in self.citation_map)
        _ensure_no_source_identity_conflicts(tuple(direct_spans))
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
        if any(
            quote.sensitivity.rank > self.policy_snapshot.max_sensitivity.rank
            for quote in self.source_quotes
        ) or any(
            item.sensitivity.rank > self.policy_snapshot.max_sensitivity.rank
            for item in self.counterevidence
        ):
            raise ValueError("ContextPack source quote exceeds the policy sensitivity ceiling")
        if not self.policy_snapshot.sensitive_resurface_allowed and any(
            sensitivity is not SensitivityLevel.NORMAL
            for sensitivity in (
                *(quote.sensitivity for quote in self.source_quotes),
                *(item.sensitivity for item in self.counterevidence),
            )
        ):
            raise ValueError("sensitive Source quotations require a separate resurface grant")
        if (
            any(
                quote.retrieval_fingerprint != self.retrieval_fingerprint
                for quote in self.source_quotes
            )
            or any(
                counter.retrieval_fingerprint != self.retrieval_fingerprint
                for counter in self.counterevidence
            )
            or any(
                binding.retrieval_fingerprint != self.retrieval_fingerprint
                for binding in self.citation_map
            )
        ):
            raise ValueError("ContextPack content belongs to another retrieval result")
        if any(
            quote.source_generation != self.policy_snapshot.source_generation
            for quote in self.source_quotes
        ) or any(
            counter.source_generation != self.policy_snapshot.source_generation
            for counter in self.counterevidence
        ):
            raise ValueError("ContextPack Source quote uses a stale Source generation")

        quote_by_record: dict[str, ContextSourceQuote] = {}
        for source_quote in self.source_quotes:
            if source_quote.record_id in quote_by_record:
                raise ValueError("ContextPack cannot contain duplicate retrieval record ids")
            quote_by_record[source_quote.record_id] = source_quote
        counter_ids: set[str] = set()
        for counter in self.counterevidence:
            if counter.record_id in quote_by_record or counter.record_id in counter_ids:
                raise ValueError("ContextPack cannot reuse a retrieval record id")
            counter_ids.add(counter.record_id)
            if not set(counter.contradiction_ids) & quote_by_record.keys():
                raise ValueError("Context counterevidence must link a returned main record")

        context_by_candidate: dict[str, ContextClaim] = {}
        for context_item in context_claims:
            if context_item.claim.candidate_id in context_by_candidate:
                raise ValueError("ContextPack cannot contain a candidate twice")
            context_by_candidate[context_item.claim.candidate_id] = context_item

        record_citations: set[str] = set()
        claim_evidence_citations: set[tuple[str, _SourceSpanIdentity]] = set()
        for binding in self.citation_map:
            cited_quote = quote_by_record.get(binding.retrieval_record_id)
            if cited_quote is None or binding.source_span != cited_quote.source_span:
                raise ValueError("citation must reference its exact retrieved Source quote")
            if binding.target.object_type is CitationObjectType.RETRIEVAL_RECORD:
                record_citations.add(binding.retrieval_record_id)
                continue
            context_claim = context_by_candidate.get(binding.target.object_id)
            if context_claim is None:
                raise ValueError("citation targets a claim absent from this ContextPack")
            if binding.verification_fingerprint != context_claim.verification.evidence_fingerprint:
                raise ValueError("claim citation uses another verifier output")
            evidence_spans = {
                _source_span_identity(evidence.source_span)
                for evidence in context_claim.verification.evidence
            }
            span_identity = _source_span_identity(binding.source_span)
            if span_identity not in evidence_spans:
                raise ValueError("claim citation is not part of its verified evidence")
            claim_evidence_citations.add((context_claim.claim.candidate_id, span_identity))

        if record_citations != quote_by_record.keys():
            raise ValueError("every retrieved Source quote requires a typed record citation")
        required_claim_citations = {
            (item.claim.candidate_id, _source_span_identity(evidence.source_span))
            for item in context_claims
            for evidence in item.verification.evidence
        }
        if required_claim_citations != claim_evidence_citations:
            raise ValueError("every Context claim evidence span requires a verifier-bound citation")
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
    "AuthorizationPurpose",
    "CandidateClaim",
    "CitationBinding",
    "CitationObjectType",
    "CitationTarget",
    "ClaimKind",
    "ClaimLifecycleState",
    "ClaimSafetyFlag",
    "ClaimTemporalContext",
    "ContextClaim",
    "ContextCounterevidence",
    "ContextPack",
    "ContextPolicySnapshot",
    "ContextSourceQuote",
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
    "MemoryAuthorizationSnapshot",
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
    "Sha256Hex",
    "SignalRank",
    "SourceKind",
    "SourceSpan",
    "TechnicalId",
    "TemporalRange",
    "TimePrecision",
    "TimeScope",
    "VersionId",
]
