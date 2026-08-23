from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from life_coach.ai import (
    Attribution,
    CandidateClaim,
    ClaimKind,
    ClaimSafetyFlag,
    ContextClaim,
    ContextPack,
    ContextPolicySnapshot,
    DerivationType,
    EntityCandidate,
    EntityKind,
    EntityResolutionOption,
    EntityResolutionSignal,
    EntityResolutionState,
    EvidenceItem,
    EvidenceRelation,
    EvidenceStatus,
    EvidenceStrength,
    EvidenceVerificationResult,
    MemoryDisposition,
    MemoryGateDecision,
    MemoryGateReason,
    RetrievalIntent,
    SensitivityLevel,
    SourceKind,
    SourceSpan,
    TemporalRange,
    TimePrecision,
    resolve_entity_candidates,
    resolve_relative_time,
)


def _span(
    quote: str = "我不想当管理者",
    *,
    vault_id: str = "vault-a",
    fragment_id: str = "fragment-1",
    source_kind: SourceKind = SourceKind.SOURCE,
    quote_hash: str | None = None,
) -> SourceSpan:
    return SourceSpan(
        vault_id=vault_id,
        source_document_id=f"document-{fragment_id}",
        source_revision_id=f"revision-{fragment_id}",
        source_fragment_id=fragment_id,
        char_start=0,
        char_end=len(quote),
        quote=quote,
        quote_hash=quote_hash,
        source_kind=source_kind,
    )


def _claim(
    span: SourceSpan | None = None,
    *,
    canonical_text: str = "用户不想当管理者",
    claim_kind: ClaimKind = ClaimKind.PREFERENCE,
    derivation: DerivationType = DerivationType.PARAPHRASE,
    negated: bool = True,
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL,
) -> CandidateClaim:
    evidence_span = span or _span()
    return CandidateClaim(
        candidate_id="candidate-1",
        vault_id=evidence_span.vault_id,
        claim_kind=claim_kind,
        canonical_text=canonical_text,
        derivation=derivation,
        attribution=Attribution.SELF_REPORT,
        source_spans=(evidence_span,),
        negated=negated,
        sensitivity=sensitivity,
    )


def _policy(
    *,
    cross_record_analysis_allowed: bool = False,
    sensitive_resurface_allowed: bool = False,
    max_sensitivity: SensitivityLevel = SensitivityLevel.HIGHLY_SENSITIVE,
) -> ContextPolicySnapshot:
    return ContextPolicySnapshot(
        consent_snapshot_id="consent-1",
        policy_epoch=1,
        max_sensitivity=max_sensitivity,
        cross_record_analysis_allowed=cross_record_analysis_allowed,
        sensitive_resurface_allowed=sensitive_resurface_allowed,
    )


def _context_claim(
    claim: CandidateClaim | None = None,
    *,
    disposition: MemoryDisposition = MemoryDisposition.ACTIVE,
    status: EvidenceStatus = EvidenceStatus.SUPPORTED,
) -> ContextClaim:
    selected_claim = claim or _claim()
    evidence = ()
    if status is EvidenceStatus.SUPPORTED:
        evidence = (
            EvidenceItem(
                relation=EvidenceRelation.SUPPORTS,
                source_span=selected_claim.source_spans[0],
                strength=EvidenceStrength.STRONG,
                reason="verified by the test fixture",
            ),
        )
    verification = EvidenceVerificationResult(
        vault_id=selected_claim.vault_id,
        candidate_id=selected_claim.candidate_id,
        claim_fingerprint=selected_claim.evidence_fingerprint,
        status=status,
        evidence=evidence,
        verifier_version="test-verifier-v1",
        eligible_for_display=status is EvidenceStatus.SUPPORTED,
    )
    if disposition is MemoryDisposition.ACTIVE:
        decision = MemoryGateDecision(
            disposition=disposition,
            reasons=(MemoryGateReason.USER_CONFIRMED,),
            requires_confirmation=False,
            may_use_proactively=False,
        )
    elif disposition is MemoryDisposition.CANDIDATE:
        decision = MemoryGateDecision(
            disposition=disposition,
            reasons=(MemoryGateReason.CONFIRMATION_REQUIRED,),
            requires_confirmation=True,
            may_use_proactively=False,
        )
    else:
        decision = MemoryGateDecision(
            disposition=disposition,
            reasons=(MemoryGateReason.POLICY_FORBIDS_STORAGE,),
            requires_confirmation=False,
            may_use_proactively=False,
        )
    return ContextClaim(
        claim=selected_claim,
        verification=verification,
        decision=decision,
    )


def _claim_payload() -> dict[str, object]:
    return {
        "candidate_id": "candidate-1",
        "vault_id": "vault-a",
        "claim_kind": ClaimKind.PREFERENCE,
        "canonical_text": "用户不想当管理者",
        "derivation": DerivationType.PARAPHRASE,
        "attribution": Attribution.SELF_REPORT,
        "source_spans": (_span(),),
        "negated": True,
    }


def test_candidate_claim_requires_a_source_span() -> None:
    missing = _claim_payload()
    missing.pop("source_spans")
    with pytest.raises(ValidationError):
        CandidateClaim.model_validate(missing)

    empty = _claim_payload()
    empty["source_spans"] = ()
    with pytest.raises(ValidationError):
        CandidateClaim.model_validate(empty)


@pytest.mark.parametrize("field_name", ["unexpected", "tool_call", "diagnosis"])
def test_candidate_claim_rejects_extra_fields(field_name: str) -> None:
    payload = _claim_payload()
    payload[field_name] = {"name": "update_user_profile"}

    with pytest.raises(ValidationError):
        CandidateClaim.model_validate(payload)


@pytest.mark.parametrize(
    "forbidden_key",
    ["tool_call", "function_call", "diagnosis", "personality_disorder", "suicide_risk_score"],
)
def test_candidate_claim_rejects_executable_or_diagnostic_payload_fields(
    forbidden_key: str,
) -> None:
    payload = _claim_payload()
    payload["structured_payload"] = {"nested": {forbidden_key: "must not persist"}}

    with pytest.raises(ValidationError, match="forbidden executable or diagnostic field"):
        CandidateClaim.model_validate(payload)


def test_negation_and_paraphrase_are_preserved() -> None:
    claim = _claim()

    assert claim.negated is True
    assert claim.derivation is DerivationType.PARAPHRASE
    assert claim.canonical_text == "用户不想当管理者"
    assert claim.source_spans[0].quote == "我不想当管理者"


def test_inference_is_preserved_and_personality_inference_is_flagged() -> None:
    span = _span("我在聚会时常常先离开")
    claim = _claim(
        span,
        canonical_text="用户可能具有稳定的回避型人际模式",
        claim_kind=ClaimKind.PATTERN_HYPOTHESIS,
        derivation=DerivationType.INFERENCE,
        negated=False,
    )

    assert claim.derivation is DerivationType.INFERENCE
    assert ClaimSafetyFlag.PERSONALITY_INFERENCE in claim.safety_flags


def test_diagnostic_text_remains_reviewable_but_is_safety_flagged() -> None:
    text = "你可能患有抑郁症"
    claim = _claim(
        _span(text),
        canonical_text=text,
        claim_kind=ClaimKind.SELF_DESCRIPTION,
        derivation=DerivationType.INFERENCE,
        negated=False,
    )

    assert ClaimSafetyFlag.DIAGNOSTIC_LANGUAGE in claim.safety_flags
    assert ClaimSafetyFlag.PERSONALITY_INFERENCE in claim.safety_flags


def test_artifact_span_is_flagged_and_is_not_a_primary_source() -> None:
    artifact = _span("上一轮 AI 总结", source_kind=SourceKind.ARTIFACT)
    claim = _claim(artifact, canonical_text="AI 总结出的新说法", negated=False)

    assert ClaimSafetyFlag.ARTIFACT_SOURCE in claim.safety_flags
    assert claim.uses_only_primary_sources is False


def test_validated_contracts_are_frozen_against_post_validation_tampering() -> None:
    claim = _claim()
    original_fingerprint = claim.evidence_fingerprint

    with pytest.raises(ValidationError, match="frozen"):
        claim.safety_flags = frozenset()
    with pytest.raises(ValidationError, match="frozen"):
        claim.sensitivity = SensitivityLevel.HIGHLY_SENSITIVE
    with pytest.raises(ValidationError, match="frozen"):
        claim.source_spans = ()

    assert claim.evidence_fingerprint == original_fingerprint


def test_imprecise_time_requires_an_original_expression_and_half_open_range() -> None:
    start = datetime(2025, 3, 1, tzinfo=UTC)
    end = datetime(2025, 6, 1, tzinfo=UTC)

    with pytest.raises(ValidationError):
        TemporalRange(
            precision=TimePrecision.RANGE,
            earliest=start,
            latest=end,
        )

    resolved = TemporalRange(
        precision=TimePrecision.RANGE,
        original_expression="去年春天",
        earliest=start,
        latest=end,
        timezone="Asia/Shanghai",
    )
    assert resolved.original_expression == "去年春天"
    assert resolved.earliest == start
    assert resolved.latest == end
    assert resolved.latest - resolved.earliest > timedelta(days=1)


def test_calendar_precision_requires_timezone_and_consistent_duration() -> None:
    start = datetime(2025, 3, 1, tzinfo=UTC)

    with pytest.raises(ValidationError, match="historical timezone"):
        TemporalRange(
            precision=TimePrecision.DAY,
            original_expression="那天",
            earliest=start,
            latest=start + timedelta(days=1),
        )

    with pytest.raises(ValidationError, match="one local calendar day"):
        TemporalRange(
            precision=TimePrecision.DAY,
            original_expression="那天",
            earliest=start,
            latest=start + timedelta(days=3),
            timezone="Asia/Shanghai",
        )


def test_unknown_time_preserves_original_expression_and_timezone_without_bounds() -> None:
    with pytest.raises(ValidationError, match="original expression"):
        TemporalRange(precision=TimePrecision.UNKNOWN, timezone="Asia/Shanghai")
    with pytest.raises(ValidationError, match="historical timezone"):
        TemporalRange(precision=TimePrecision.UNKNOWN, original_expression="某个时候")

    unknown = TemporalRange(
        precision=TimePrecision.UNKNOWN,
        original_expression="某个时候",
        timezone="Asia/Shanghai",
    )
    assert unknown.earliest is None
    assert unknown.latest is None


def test_resolve_last_spring_keeps_timezone_and_stores_honest_utc_range() -> None:
    captured_at = datetime(2026, 8, 23, 4, 30, tzinfo=UTC)

    resolved = resolve_relative_time(
        "去年春天",
        captured_at=captured_at,
        capture_timezone="Asia/Shanghai",
    )

    assert resolved.precision is TimePrecision.RANGE
    assert resolved.original_expression == "去年春天"
    assert resolved.timezone == "Asia/Shanghai"
    assert resolved.reference_timestamp == captured_at
    assert resolved.earliest == datetime(2025, 2, 28, 16, tzinfo=UTC)
    assert resolved.latest == datetime(2025, 5, 31, 16, tzinfo=UTC)
    assert resolved.latest - resolved.earliest > timedelta(days=1)


def test_entity_candidate_requires_an_id_for_a_proposed_resolution() -> None:
    with pytest.raises(ValidationError):
        EntityCandidate(
            mention="小李",
            mention_span=_span("小李"),
            kind=EntityKind.PERSON,
            resolution_state=EntityResolutionState.PROPOSED,
            confidence_reason="alias matched",
        )


def test_entity_candidate_keeps_mention_and_confirmation_state_consistent() -> None:
    with pytest.raises(ValidationError, match="exactly match"):
        EntityCandidate(
            mention="小王",
            mention_span=_span("小李"),
            kind=EntityKind.PERSON,
            confidence_reason="model substituted another person",
        )

    with pytest.raises(ValidationError, match="requires user confirmation"):
        EntityCandidate(
            mention="小李",
            mention_span=_span("小李"),
            kind=EntityKind.PERSON,
            entity_id="person-a",
            resolution_state=EntityResolutionState.PROPOSED,
            confidence_reason="alias matched",
            requires_user_confirmation=False,
        )

    with pytest.raises(ValidationError, match="cannot still require confirmation"):
        EntityCandidate(
            mention="小李",
            mention_span=_span("小李"),
            kind=EntityKind.PERSON,
            entity_id="person-a",
            resolution_state=EntityResolutionState.CONFIRMED,
            confidence_reason="the user confirmed this entity",
        )

    confirmed = EntityCandidate(
        mention="小李",
        mention_span=_span("小李"),
        kind=EntityKind.PERSON,
        entity_id="person-a",
        resolution_state=EntityResolutionState.CONFIRMED,
        confidence_reason="the user confirmed this entity",
        requires_user_confirmation=False,
    )
    assert confirmed.resolution_state is EntityResolutionState.CONFIRMED


def test_entity_resolution_filters_cross_vault_options_before_matching() -> None:
    mention_span = _span("小李")
    same_vault = EntityResolutionOption(
        vault_id="vault-a",
        entity_id="person-a",
        kind=EntityKind.PERSON,
        canonical_label="李明",
        aliases=("小李",),
    )
    other_vault = EntityResolutionOption(
        vault_id="vault-b",
        entity_id="person-b",
        kind=EntityKind.PERSON,
        canonical_label="李明",
        aliases=("小李",),
    )

    candidates = resolve_entity_candidates(
        vault_id="vault-a",
        mention="小李",
        mention_span=mention_span,
        kind=EntityKind.PERSON,
        options=(other_vault, same_vault),
    )

    assert [candidate.entity_id for candidate in candidates] == ["person-a"]
    assert candidates[0].resolution_state is EntityResolutionState.PROPOSED
    assert candidates[0].requires_user_confirmation is True

    unresolved = resolve_entity_candidates(
        vault_id="vault-a",
        mention="小李",
        mention_span=mention_span,
        kind=EntityKind.PERSON,
        options=(other_vault,),
    )
    assert unresolved[0].resolution_state is EntityResolutionState.UNRESOLVED
    assert unresolved[0].entity_id is None


def test_entity_resolution_does_not_invent_a_time_signal_without_an_active_window() -> None:
    candidates = resolve_entity_candidates(
        vault_id="vault-a",
        mention="小李",
        mention_span=_span("小李"),
        kind=EntityKind.PERSON,
        options=(
            EntityResolutionOption(
                vault_id="vault-a",
                entity_id="person-a",
                kind=EntityKind.PERSON,
                canonical_label="小李",
            ),
        ),
        mentioned_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert candidates[0].signals == frozenset({EntityResolutionSignal.ALIAS})


def test_context_pack_citations_must_reference_included_source_quotes() -> None:
    included = _span("第一条原文", fragment_id="included")
    absent = _span("未纳入的原文", fragment_id="absent")

    with pytest.raises(ValidationError, match="citation_map can only reference"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(),
            source_quotes=(included,),
            citation_map={"claim-1": (absent,)},
        )

    context = ContextPack(
        vault_id="vault-a",
        version="context-v1",
        purpose=RetrievalIntent.FACT_LOOKUP,
        policy_snapshot=_policy(),
        source_quotes=(included,),
        citation_map={"claim-1": (included,)},
    )
    assert context.citation_map["claim-1"] == (included,)


def test_context_pack_rejects_citation_text_or_hash_spoofing_at_the_same_offsets() -> None:
    included = _span("第一条原文", fragment_id="same-anchor", quote_hash="hash-a")
    altered_text = _span("伪造的原文", fragment_id="same-anchor", quote_hash="hash-a")
    altered_hash = _span("第一条原文", fragment_id="same-anchor", quote_hash="hash-b")

    for spoofed in (altered_text, altered_hash):
        with pytest.raises(ValidationError, match="citation_map can only reference"):
            ContextPack(
                vault_id="vault-a",
                version="context-v1",
                purpose=RetrievalIntent.FACT_LOOKUP,
                policy_snapshot=_policy(),
                source_quotes=(included,),
                citation_map={"claim-1": (spoofed,)},
            )


def test_context_pack_rejects_artifacts_as_quotes_or_claim_evidence() -> None:
    artifact_span = _span("AI 生成的总结", source_kind=SourceKind.ARTIFACT)

    with pytest.raises(ValidationError, match="cannot enter a ContextPack"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(),
            source_quotes=(artifact_span,),
        )

    artifact_claim = _claim(artifact_span)
    with pytest.raises(ValidationError, match="cannot enter a ContextPack"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(),
            candidate_claims=(
                _context_claim(
                    artifact_claim,
                    disposition=MemoryDisposition.CANDIDATE,
                    status=EvidenceStatus.UNSUPPORTED,
                ),
            ),
        )


def test_context_claim_requires_the_verified_gate_chain() -> None:
    claim = _claim()
    valid = _context_claim(claim)

    mismatched_verification = valid.verification.model_copy(update={"claim_fingerprint": "0" * 64})
    with pytest.raises(ValidationError, match="fingerprint"):
        ContextClaim(
            claim=claim,
            verification=mismatched_verification,
            decision=valid.decision,
        )

    with pytest.raises(ValidationError, match="non-persistent"):
        _context_claim(claim, disposition=MemoryDisposition.NON_PERSISTENT)

    with pytest.raises(ValidationError):
        ContextPack.model_validate(
            {
                "vault_id": "vault-a",
                "version": "context-v1",
                "purpose": RetrievalIntent.FACT_LOOKUP,
                "policy_snapshot": _policy(),
                "confirmed_claims": (claim,),
            }
        )


def test_context_pack_keeps_active_and_candidate_claims_in_their_own_lanes() -> None:
    active = _context_claim(disposition=MemoryDisposition.ACTIVE)
    candidate = _context_claim(disposition=MemoryDisposition.CANDIDATE)

    with pytest.raises(ValidationError, match="confirmed ContextPack claims must be active"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(),
            confirmed_claims=(candidate,),
        )

    with pytest.raises(ValidationError, match="candidate ContextPack claims must remain"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(),
            candidate_claims=(active,),
        )

    context = ContextPack(
        vault_id="vault-a",
        version="context-v1",
        purpose=RetrievalIntent.FACT_LOOKUP,
        policy_snapshot=_policy(),
        confirmed_claims=(active,),
        candidate_claims=(candidate,),
    )
    assert context.confirmed_claims == (active,)
    assert context.candidate_claims == (candidate,)


def test_context_pack_requires_a_separate_sensitive_resurface_grant() -> None:
    claim = _claim(sensitivity=SensitivityLevel.SENSITIVE)
    active = _context_claim(claim)

    with pytest.raises(ValidationError, match="separate resurface grant"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(),
            confirmed_claims=(active,),
        )

    context = ContextPack(
        vault_id="vault-a",
        version="context-v1",
        purpose=RetrievalIntent.FACT_LOOKUP,
        policy_snapshot=_policy(sensitive_resurface_allowed=True),
        confirmed_claims=(active,),
    )
    assert context.confirmed_claims == (active,)

    with pytest.raises(ValidationError, match="sensitivity ceiling"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(
                sensitive_resurface_allowed=True,
                max_sensitivity=SensitivityLevel.NORMAL,
            ),
            confirmed_claims=(active,),
        )


@pytest.mark.parametrize("coverage", [{}, {"counterevidence_searched": False}])
def test_pattern_reflection_requires_a_completed_counterevidence_pass(
    coverage: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="counterevidence pass"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.PATTERN_REFLECTION,
            policy_snapshot=_policy(cross_record_analysis_allowed=True),
            coverage=coverage,
        )


def test_pattern_reflection_allows_an_empty_but_completed_counterevidence_pass() -> None:
    context = ContextPack(
        vault_id="vault-a",
        version="context-v1",
        purpose=RetrievalIntent.PATTERN_REFLECTION,
        policy_snapshot=_policy(cross_record_analysis_allowed=True),
        coverage={"counterevidence_searched": True},
    )

    assert context.counterevidence == ()
    assert context.coverage["counterevidence_searched"] is True


def test_pattern_reflection_requires_cross_record_consent_separately() -> None:
    with pytest.raises(ValidationError, match="cross-record analysis consent"):
        ContextPack(
            vault_id="vault-a",
            version="context-v1",
            purpose=RetrievalIntent.PATTERN_REFLECTION,
            policy_snapshot=_policy(),
            coverage={"counterevidence_searched": True},
        )
