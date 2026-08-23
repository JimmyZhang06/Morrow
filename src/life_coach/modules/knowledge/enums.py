"""Stable persisted enums for the Knowledge domain."""

from __future__ import annotations

from enum import StrEnum


class DataClass(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    HIGHLY_SENSITIVE = "highly_sensitive"


class DerivedObjectKind(StrEnum):
    CLAIM_VERSION = "claim_version"
    INSIGHT_CANDIDATE = "insight_candidate"
    NARRATIVE_CLAIM = "narrative_claim"


class MemoryClaimKind(StrEnum):
    EXPLICIT_FACT = "explicit_fact"
    PREFERENCE = "preference"
    VALUE = "value"
    GOAL = "goal"
    RELATIONSHIP = "relationship"
    SELF_DESCRIPTION = "self_description"
    PATTERN_HYPOTHESIS = "pattern_hypothesis"


class EpistemicType(StrEnum):
    STATED = "stated"
    OBSERVED = "observed"
    INFERRED = "inferred"
    USER_AUTHORED = "user_authored"


class Attribution(StrEnum):
    SELF_REPORT = "self_report"
    QUOTED_OTHER = "quoted_other"
    IMPORTED_RECORD = "imported_record"
    MODEL_HYPOTHESIS = "model_hypothesis"


class LifecycleState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class ValidTimePrecision(StrEnum):
    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    RANGE = "range"
    UNKNOWN = "unknown"


class ConfidenceBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXTUALIZES = "contextualizes"


class EvidenceStrength(StrEnum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class EvidenceExtractionReason(StrEnum):
    EXPLICIT_STATEMENT = "explicit_statement"
    CONTRADICTION = "contradiction"
    CONTEXT = "context"
    USER_CORRECTION = "user_correction"


class TechnicalActor(StrEnum):
    KNOWLEDGE_PIPELINE = "knowledge_pipeline"
    USER = "user"
    SOURCE_RECONCILER = "source_reconciler"


class SourceEvidenceStatus(StrEnum):
    LIVE = "live"
    TOMBSTONED = "tombstoned"
    EXCLUDED = "excluded"
    CONSENT_REVOKED = "consent_revoked"
    STALE = "stale"
    UNKNOWN = "unknown"


class AuthorizationPurpose(StrEnum):
    MEMORY_CREATE = "memory_create"
    MEMORY_REVIEW = "memory_review"
    MEMORY_HISTORY = "memory_history"
    PROACTIVE_RESURFACING = "proactive_resurfacing"


class SafetyDecision(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    UNKNOWN = "unknown"


class CorrectionMode(StrEnum):
    INTERPRETATION_ERROR = "interpretation_error"
    LIFE_STAGE_CHANGE = "life_stage_change"


class ClaimVersionOrigin(StrEnum):
    PIPELINE_DERIVED = "pipeline_derived"
    USER_CORRECTION = "user_correction"


class VerdictType(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    REJECT = "reject"
    SNOOZE = "snooze"
    RETRACT = "retract"
