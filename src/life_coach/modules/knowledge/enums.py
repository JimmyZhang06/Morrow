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


class VerdictType(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    REJECT = "reject"
    SNOOZE = "snooze"
    RETRACT = "retract"
