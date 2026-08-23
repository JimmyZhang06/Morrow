"""Memory activation and psychology-safety policy gates."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from .enums import (
    Attribution,
    DataClass,
    EpistemicType,
    EvidenceRelation,
    LifecycleState,
    MemoryClaimKind,
    VerdictType,
)
from .exceptions import PolicyViolationError


class EvidenceForPolicy(Protocol):
    """Minimal evidence shape consumed by activation and lifecycle policy."""

    @property
    def source_fragment_id(self) -> uuid.UUID: ...

    @property
    def source_revision_id(self) -> uuid.UUID: ...

    @property
    def source_content_fingerprint(self) -> str: ...

    @property
    def relation(self) -> EvidenceRelation: ...

    @property
    def source_recorded_at(self) -> datetime | None: ...

    @property
    def deleted_at(self) -> datetime | None: ...


_PROHIBITED_CLINICAL_KEYS = frozenset(
    {
        "diagnosis",
        "diagnoses",
        "diagnosticconclusion",
        "conditioncode",
        "conditioncodes",
        "dsmdiagnosis",
        "icddiagnosis",
        "personalitydisorder",
        "suicideriskscore",
        "suiciderisklevel",
    }
)

_CLINICAL_LANGUAGE = re.compile(
    r"(?:\b(?:major\s+depressive\s+disorder|depression|bipolar(?:\s+disorder)?|"
    r"ptsd|ocd|psychosis|schizophrenia|autism|adhd|anorexia|bulimia|"
    r"eating\s+disorder|anxiety\s+disorder|substance\s+use\s+disorder|"
    r"depressive\s+episode|suicidal|self[- ]harm|narcissist(?:ic)?|borderline|"
    r"[a-z-]+\s+personality\s+disorder)\b|"
    r"\bdiagnos(?:e|ed|is|tic|tics)\b|\bsuicide\s+risk(?:\s+(?:score|level))?\b|"
    r"\bDSM-?\d*\b|\bICD-?\d*\b|抑郁症|双相(?:情感)?障碍|创伤后应激障碍|"
    r"强迫症|精神分裂症|精神病性障碍|焦虑症|(?:广泛性)?焦虑障碍|"
    r"自闭症|孤独症|注意缺陷多动障碍|进食障碍|厌食症|贪食症|"
    r"人格障碍|自恋狂|自恋型人格|自杀风险|自杀倾向|想自杀|轻生|"
    r"(?:临床|心理|精神科)?诊断)",
    re.IGNORECASE,
)

_CONFIRMATION_REQUIRED = frozenset(
    {
        MemoryClaimKind.VALUE,
        MemoryClaimKind.GOAL,
        MemoryClaimKind.RELATIONSHIP,
        MemoryClaimKind.SELF_DESCRIPTION,
        MemoryClaimKind.PATTERN_HYPOTHESIS,
    }
)


def _payload_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        keys = {re.sub(r"[^a-z0-9]", "", str(key).casefold()) for key in value}
        for child in value.values():
            keys.update(_payload_keys(child))
        return keys
    if isinstance(value, list):
        list_keys: set[str] = set()
        for child in value:
            list_keys.update(_payload_keys(child))
        return list_keys
    return set()


def _payload_contains_clinical_language(value: Any) -> bool:
    if isinstance(value, str):
        return contains_clinical_language(value)
    if isinstance(value, Mapping):
        return any(contains_clinical_language(str(key)) for key in value) or any(
            _payload_contains_clinical_language(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(_payload_contains_clinical_language(child) for child in value)
    return False


def validate_persistable_memory(
    *,
    canonical_text: str,
    structured_payload: Mapping[str, Any],
    epistemic_type: EpistemicType,
    attribution: Attribution,
    data_class: DataClass,
) -> None:
    """Reject model-generated clinical/personality conclusions from ordinary memory.

    A first-party report such as "my doctor told me ..." may remain a highly
    sensitive record, but it must keep self-report/quoted attribution and is never
    reclassified here as a system diagnosis.
    """

    unsafe_keys = _payload_keys(structured_payload) & _PROHIBITED_CLINICAL_KEYS
    if unsafe_keys:
        raise PolicyViolationError(
            "Clinical diagnosis and suicide-risk fields are not part of ordinary memory"
        )
    if (
        epistemic_type is EpistemicType.USER_AUTHORED
        and attribution is Attribution.MODEL_HYPOTHESIS
    ):
        raise PolicyViolationError("User-authored memory cannot carry model-hypothesis attribution")
    model_origin = (
        epistemic_type is EpistemicType.INFERRED or attribution is Attribution.MODEL_HYPOTHESIS
    )
    contains_clinical_content = contains_clinical_language(
        canonical_text
    ) or _payload_contains_clinical_language(structured_payload)
    if model_origin and contains_clinical_content:
        raise PolicyViolationError(
            "Model-generated diagnostic, personality-disorder, and suicide-risk "
            "conclusions cannot be persisted as ordinary memory"
        )
    if contains_clinical_content and data_class is not DataClass.HIGHLY_SENSITIVE:
        raise PolicyViolationError(
            "First-party clinical reports require highly-sensitive classification"
        )


def contains_clinical_language(text: str) -> bool:
    """Conservatively detect content that must retain highly-sensitive handling."""

    return _CLINICAL_LANGUAGE.search(text) is not None


def is_strict_hypothesis(
    *,
    kind: MemoryClaimKind,
    epistemic_type: EpistemicType,
    attribution: Attribution,
) -> bool:
    """Return whether activation needs endorsement *and* independent evidence."""

    return kind is MemoryClaimKind.PATTERN_HYPOTHESIS or (
        kind in {MemoryClaimKind.SELF_DESCRIPTION, MemoryClaimKind.RELATIONSHIP}
        and (
            epistemic_type is EpistemicType.INFERRED or attribution is Attribution.MODEL_HYPOTHESIS
        )
    )


def _effective_evidence(evidence: Iterable[EvidenceForPolicy]) -> list[EvidenceForPolicy]:
    return [item for item in evidence if item.deleted_at is None]


def independent_support_count(evidence: Iterable[EvidenceForPolicy]) -> int:
    """Count time-independent supports without pretending unknown times are distinct."""

    points: set[datetime] = set()
    fragments: set[uuid.UUID] = set()
    revisions: set[uuid.UUID] = set()
    source_fingerprints: set[str] = set()
    for item in _effective_evidence(evidence):
        if item.relation is not EvidenceRelation.SUPPORTS or item.source_recorded_at is None:
            continue
        recorded_at = item.source_recorded_at
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        points.add(recorded_at.astimezone(UTC))
        fragments.add(item.source_fragment_id)
        revisions.add(item.source_revision_id)
        source_fingerprints.add(item.source_content_fingerprint)
    return min(len(points), len(fragments), len(revisions), len(source_fingerprints))


def has_support(evidence: Iterable[EvidenceForPolicy]) -> bool:
    return any(item.relation is EvidenceRelation.SUPPORTS for item in _effective_evidence(evidence))


def has_counterevidence(evidence: Iterable[EvidenceForPolicy]) -> bool:
    return any(
        item.relation is EvidenceRelation.CONTRADICTS for item in _effective_evidence(evidence)
    )


def can_auto_activate(
    *,
    kind: MemoryClaimKind,
    data_class: DataClass,
    epistemic_type: EpistemicType,
    attribution: Attribution,
    evidence: Iterable[EvidenceForPolicy],
) -> bool:
    """Narrow allow-list for low-risk explicit first-party facts."""

    return (
        data_class is DataClass.NORMAL
        and kind is MemoryClaimKind.EXPLICIT_FACT
        and epistemic_type in {EpistemicType.STATED, EpistemicType.USER_AUTHORED}
        and attribution is Attribution.SELF_REPORT
        and has_support(evidence)
        and not has_counterevidence(evidence)
    )


def activation_allowed(
    *,
    kind: MemoryClaimKind,
    data_class: DataClass,
    epistemic_type: EpistemicType,
    attribution: Attribution,
    evidence: Iterable[EvidenceForPolicy],
    last_decisive_verdict: VerdictType | None,
    requires_explicit_confirmation: bool = False,
) -> bool:
    """Combine provenance, evidence, sensitivity, and user governance deterministically."""

    if last_decisive_verdict in {VerdictType.REJECT, VerdictType.RETRACT, VerdictType.CORRECT}:
        return False
    effective_evidence = tuple(_effective_evidence(evidence))
    if not has_support(effective_evidence):
        return False
    user_endorsed = last_decisive_verdict is VerdictType.CONFIRM or (
        last_decisive_verdict is None
        and epistemic_type is EpistemicType.USER_AUTHORED
        and attribution is Attribution.SELF_REPORT
    )
    if requires_explicit_confirmation and last_decisive_verdict is not VerdictType.CONFIRM:
        return False
    if is_strict_hypothesis(kind=kind, epistemic_type=epistemic_type, attribution=attribution):
        return user_endorsed and independent_support_count(effective_evidence) >= 2
    if (
        kind in _CONFIRMATION_REQUIRED
        or data_class is not DataClass.NORMAL
        or epistemic_type is EpistemicType.INFERRED
        or attribution is Attribution.MODEL_HYPOTHESIS
    ):
        return user_endorsed
    return user_endorsed or can_auto_activate(
        kind=kind,
        data_class=data_class,
        epistemic_type=epistemic_type,
        attribution=attribution,
        evidence=effective_evidence,
    )


def policy_lifecycle(
    *,
    reduced_state: LifecycleState,
    activation_is_allowed: bool,
    evidence: Iterable[EvidenceForPolicy],
) -> LifecycleState:
    """Apply evidence/counterevidence gates to a reduced nonterminal lifecycle."""

    if reduced_state in {LifecycleState.SUPERSEDED, LifecycleState.RETRACTED}:
        return reduced_state
    if has_counterevidence(evidence):
        return LifecycleState.DISPUTED
    if activation_is_allowed:
        return LifecycleState.ACTIVE
    if reduced_state is LifecycleState.ACTIVE:
        return LifecycleState.DISPUTED
    return reduced_state


def allowed_uses(
    *,
    state: LifecycleState,
    kind: MemoryClaimKind,
    data_class: DataClass,
    current_verdict: VerdictType | None,
    governance_verdict: VerdictType | None,
    governance_applies: bool,
    is_historical: bool,
    source_authority_current: bool,
    authorization_allows_read: bool,
    authorization_allows_proactive: bool,
    safety_allows_proactive: bool,
    suppression_pending: bool,
) -> tuple[str, ...]:
    if not authorization_allows_read:
        return ()
    if state in {LifecycleState.SUPERSEDED, LifecycleState.RETRACTED}:
        return ()
    if governance_applies and governance_verdict is VerdictType.RETRACT:
        return ()
    if suppression_pending or (
        governance_applies
        and governance_verdict in {VerdictType.SNOOZE, VerdictType.REJECT, VerdictType.CORRECT}
    ):
        return ("review_only",)
    if current_verdict in {VerdictType.SNOOZE, VerdictType.REJECT}:
        return ("review_only",)
    if not source_authority_current:
        return ("review_only",)
    if state in {LifecycleState.CANDIDATE, LifecycleState.DISPUTED}:
        return ("review_only", "answer_when_asked")
    if is_historical:
        return ("answer_when_asked",)
    if data_class is not DataClass.NORMAL or kind is MemoryClaimKind.PATTERN_HYPOTHESIS:
        return ("answer_when_asked",)
    if not authorization_allows_proactive or not safety_allows_proactive:
        return ("answer_when_asked",)
    return ("answer_when_asked", "proactive_coaching")
