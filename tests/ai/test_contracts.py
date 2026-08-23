from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import JsonValue, ValidationError

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
    MemoryAuthorizationSnapshot,
    MemoryDisposition,
    MemoryGateDecision,
    MemoryGateInput,
    MemoryGateReason,
    RetrievalIntent,
    SensitivityLevel,
    SourceKind,
    SourceSpan,
    TemporalRange,
    TimePrecision,
    decide_memory,
    resolve_entity_candidates,
    resolve_relative_time,
)
from life_coach.ai.contracts import (
    CitationBinding,
    CitationObjectType,
    CitationTarget,
    ContextSourceQuote,
)

SOURCE_GENERATION = 7
ISSUED_AT = datetime(2026, 8, 24, 8, tzinfo=UTC)
EVALUATED_AT = datetime(2026, 8, 24, 9, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 8, 24, 10, tzinfo=UTC)
RETRIEVAL_FINGERPRINT = sha256(b"retrieval-1").hexdigest()
OTHER_RETRIEVAL_FINGERPRINT = sha256(b"retrieval-2").hexdigest()


def _span(
    quote: str = "我不想当管理者",
    *,
    vault_id: str = "vault-a",
    fragment_id: str = "fragment-1",
    source_kind: SourceKind = SourceKind.SOURCE,
    quote_hash: str | None = None,
) -> SourceSpan:
    digest = quote_hash if quote_hash is not None else sha256(quote.encode("utf-8")).hexdigest()
    return SourceSpan(
        vault_id=vault_id,
        source_document_id=f"document-{fragment_id}",
        source_revision_id=f"revision-{fragment_id}",
        source_fragment_id=fragment_id,
        char_start=0,
        char_end=len(quote),
        quote=quote,
        quote_hash=digest,
        source_kind=source_kind,
    )


def _claim(
    span: SourceSpan | None = None,
    *,
    candidate_id: str = "candidate-1",
    canonical_text: str = "用户不想当管理者",
    claim_kind: ClaimKind = ClaimKind.PREFERENCE,
    derivation: DerivationType = DerivationType.PARAPHRASE,
    negated: bool = True,
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL,
) -> CandidateClaim:
    evidence_span = span or _span()
    return CandidateClaim(
        candidate_id=candidate_id,
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
    vault_id: str = "vault-a",
    purpose: RetrievalIntent = RetrievalIntent.FACT_LOOKUP,
    consent_snapshot_id: str = "consent-1",
    policy_epoch: int = 1,
    source_generation: int = SOURCE_GENERATION,
    issued_at: datetime = ISSUED_AT,
    expires_at: datetime = EXPIRES_AT,
    cross_record_analysis_allowed: bool = False,
    sensitive_resurface_allowed: bool = False,
    max_sensitivity: SensitivityLevel = SensitivityLevel.HIGHLY_SENSITIVE,
) -> ContextPolicySnapshot:
    return ContextPolicySnapshot(
        vault_id=vault_id,
        purpose=purpose,
        consent_snapshot_id=consent_snapshot_id,
        policy_epoch=policy_epoch,
        source_generation=source_generation,
        issued_at=issued_at,
        expires_at=expires_at,
        max_sensitivity=max_sensitivity,
        cross_record_analysis_allowed=cross_record_analysis_allowed,
        sensitive_resurface_allowed=sensitive_resurface_allowed,
    )


def _authorization(
    *,
    vault_id: str = "vault-a",
    consent_snapshot_id: str = "consent-1",
    policy_epoch: int = 1,
    source_generation: int = SOURCE_GENERATION,
    issued_at: datetime = ISSUED_AT,
    expires_at: datetime = EXPIRES_AT,
    storage_allowed: bool = True,
    sensitive_storage_allowed: bool = False,
    highly_sensitive_storage_allowed: bool = False,
    proactive_use_allowed: bool = False,
) -> MemoryAuthorizationSnapshot:
    return MemoryAuthorizationSnapshot(
        vault_id=vault_id,
        consent_snapshot_id=consent_snapshot_id,
        policy_epoch=policy_epoch,
        source_generation=source_generation,
        issued_at=issued_at,
        expires_at=expires_at,
        storage_allowed=storage_allowed,
        sensitive_storage_allowed=sensitive_storage_allowed,
        highly_sensitive_storage_allowed=highly_sensitive_storage_allowed,
        proactive_use_allowed=proactive_use_allowed,
    )


def _verification(
    claim: CandidateClaim,
    *,
    status: EvidenceStatus = EvidenceStatus.SUPPORTED,
    source_generation: int = SOURCE_GENERATION,
    verifier_version: str = "test-verifier-v1",
) -> EvidenceVerificationResult:
    evidence: tuple[EvidenceItem, ...] = ()
    if status is EvidenceStatus.SUPPORTED:
        evidence = (
            EvidenceItem(
                relation=EvidenceRelation.SUPPORTS,
                source_span=claim.source_spans[0],
                strength=EvidenceStrength.STRONG,
                reason="verified by the test fixture",
            ),
        )
    return EvidenceVerificationResult(
        vault_id=claim.vault_id,
        candidate_id=claim.candidate_id,
        claim_fingerprint=claim.evidence_fingerprint,
        source_generation=source_generation,
        status=status,
        evidence=evidence,
        verifier_version=verifier_version,
        eligible_for_display=status is EvidenceStatus.SUPPORTED,
    )


def _context_claim(
    claim: CandidateClaim | None = None,
    *,
    disposition: MemoryDisposition = MemoryDisposition.ACTIVE,
    status: EvidenceStatus = EvidenceStatus.SUPPORTED,
    retrieval_fingerprint: str = RETRIEVAL_FINGERPRINT,
) -> ContextClaim:
    selected_claim = claim or _claim()
    verification = _verification(selected_claim, status=status)
    sensitive_storage_allowed = selected_claim.sensitivity is not SensitivityLevel.NORMAL
    highly_sensitive_storage_allowed = (
        selected_claim.sensitivity is SensitivityLevel.HIGHLY_SENSITIVE
    )
    authorization = _authorization(
        vault_id=selected_claim.vault_id,
        storage_allowed=disposition is not MemoryDisposition.NON_PERSISTENT,
        sensitive_storage_allowed=sensitive_storage_allowed,
        highly_sensitive_storage_allowed=highly_sensitive_storage_allowed,
    )
    confirmation_verdict_id = "verdict-1" if disposition is MemoryDisposition.ACTIVE else None
    decision = decide_memory(
        MemoryGateInput(
            claim=selected_claim,
            verification=verification,
            authorization=authorization,
            evaluated_at=EVALUATED_AT,
            confirmation_verdict_id=confirmation_verdict_id,
        )
    )
    return ContextClaim(
        claim=selected_claim,
        verification=verification,
        decision=decision,
        retrieval_fingerprint=retrieval_fingerprint,
    )


def _context_pack(
    *,
    vault_id: str = "vault-a",
    purpose: RetrievalIntent = RetrievalIntent.FACT_LOOKUP,
    policy_snapshot: ContextPolicySnapshot | None = None,
    policy_fingerprint: str | None = None,
    retrieval_fingerprint: str = RETRIEVAL_FINGERPRINT,
    created_at: datetime = EVALUATED_AT,
    confirmed_claims: tuple[ContextClaim, ...] = (),
    candidate_claims: tuple[ContextClaim, ...] = (),
    source_quotes: tuple[ContextSourceQuote, ...] = (),
    citation_map: tuple[CitationBinding, ...] = (),
    coverage: dict[str, JsonValue] | None = None,
) -> ContextPack:
    policy = policy_snapshot or _policy(vault_id=vault_id, purpose=purpose)
    context_claims = (*confirmed_claims, *candidate_claims)
    if context_claims and not source_quotes and not citation_map:
        generated_quotes: list[ContextSourceQuote] = []
        citations: list[CitationBinding] = []
        for context_claim in context_claims:
            for index, evidence in enumerate(context_claim.verification.evidence):
                quote = _source_quote(
                    evidence.source_span,
                    record_id=f"record-{context_claim.claim.candidate_id}-{index}",
                    sensitivity=context_claim.claim.sensitivity,
                    retrieval_fingerprint=retrieval_fingerprint,
                )
                generated_quotes.append(quote)
                citations.extend(
                    (
                        _record_citation(quote),
                        _claim_citation(context_claim, quote),
                    )
                )
        source_quotes = tuple(generated_quotes)
        citation_map = tuple(citations)
    return ContextPack(
        vault_id=vault_id,
        version="context-v1",
        purpose=purpose,
        policy_snapshot=policy,
        policy_fingerprint=(
            policy.policy_fingerprint if policy_fingerprint is None else policy_fingerprint
        ),
        retrieval_fingerprint=retrieval_fingerprint,
        created_at=created_at,
        confirmed_claims=confirmed_claims,
        candidate_claims=candidate_claims,
        source_quotes=source_quotes,
        coverage=coverage or {},
        citation_map=citation_map,
    )


def _source_quote(
    source_span: SourceSpan,
    *,
    record_id: str = "record-1",
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL,
    retrieval_fingerprint: str = RETRIEVAL_FINGERPRINT,
) -> ContextSourceQuote:
    return ContextSourceQuote(
        record_id=record_id,
        source_generation=SOURCE_GENERATION,
        source_span=source_span,
        sensitivity=sensitivity,
        retrieval_fingerprint=retrieval_fingerprint,
    )


def _record_citation(
    quote: ContextSourceQuote,
    *,
    source_span: SourceSpan | None = None,
    retrieval_record_id: str | None = None,
    retrieval_fingerprint: str | None = None,
) -> CitationBinding:
    record_id = retrieval_record_id or quote.record_id
    return CitationBinding(
        target=CitationTarget(
            object_type=CitationObjectType.RETRIEVAL_RECORD,
            object_id=record_id,
        ),
        retrieval_record_id=record_id,
        source_span=source_span or quote.source_span,
        retrieval_fingerprint=(
            quote.retrieval_fingerprint if retrieval_fingerprint is None else retrieval_fingerprint
        ),
    )


def _claim_citation(
    context_claim: ContextClaim,
    quote: ContextSourceQuote,
    *,
    source_span: SourceSpan | None = None,
    verification_fingerprint: str | None = None,
    retrieval_fingerprint: str | None = None,
) -> CitationBinding:
    return CitationBinding(
        target=CitationTarget(
            object_type=CitationObjectType.CANDIDATE_CLAIM,
            object_id=context_claim.claim.candidate_id,
        ),
        retrieval_record_id=quote.record_id,
        source_span=source_span or quote.source_span,
        retrieval_fingerprint=(
            context_claim.retrieval_fingerprint
            if retrieval_fingerprint is None
            else retrieval_fingerprint
        ),
        verification_fingerprint=(
            context_claim.verification.evidence_fingerprint
            if verification_fingerprint is None
            else verification_fingerprint
        ),
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


def test_source_span_helper_uses_the_exact_quote_sha256() -> None:
    span = _span("带 Unicode 的原文")

    assert span.quote_hash == sha256(span.quote.encode("utf-8")).hexdigest()


def test_source_span_rejects_a_missing_quote_hash() -> None:
    payload = _span().model_dump()
    payload.pop("quote_hash")

    with pytest.raises(ValidationError):
        SourceSpan.model_validate(payload)


def test_source_span_rejects_a_malformed_quote_hash() -> None:
    payload = _span().model_dump()
    payload["quote_hash"] = "not-a-sha256-digest"

    with pytest.raises(ValidationError):
        SourceSpan.model_validate(payload)


def test_source_span_rejects_a_well_formed_but_wrong_quote_hash() -> None:
    payload = _span().model_dump()
    payload["quote_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="SHA-256 digest"):
        SourceSpan.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_id",
    ["这是正文", "identifier with spaces", "x" * 129],
)
def test_technical_ids_reject_prose_unicode_and_overlong_values(invalid_id: str) -> None:
    payload = _span().model_dump()
    payload["source_fragment_id"] = invalid_id

    with pytest.raises(ValidationError):
        SourceSpan.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_version",
    ["这是版本正文", "release candidate", "v" * 65],
)
def test_version_ids_reject_prose_unicode_and_overlong_values(invalid_version: str) -> None:
    payload = _verification(_claim()).model_dump()
    payload["verifier_version"] = invalid_version

    with pytest.raises(ValidationError):
        EvidenceVerificationResult.model_validate(payload)


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
    [
        "tool_call",
        "toolCall",
        "function_call",
        "diagnosis",
        "personality_disorder",
        "personalityDisorder",
        "suicide_risk_score",
        "suicideRiskScore",
        "tool\u200bCall",
    ],
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


def test_memory_authorization_defaults_to_deny() -> None:
    authorization = MemoryAuthorizationSnapshot(
        vault_id="vault-a",
        consent_snapshot_id="consent-1",
        policy_epoch=1,
        source_generation=SOURCE_GENERATION,
        issued_at=ISSUED_AT,
        expires_at=EXPIRES_AT,
    )

    assert authorization.storage_allowed is False
    assert authorization.sensitive_storage_allowed is False
    assert authorization.highly_sensitive_storage_allowed is False
    assert authorization.proactive_use_allowed is False


@pytest.mark.parametrize(
    "grant",
    [
        "sensitive_storage_allowed",
        "highly_sensitive_storage_allowed",
        "proactive_use_allowed",
    ],
)
def test_memory_authorization_rejects_derived_grants_without_storage(grant: str) -> None:
    values = {
        "vault_id": "vault-a",
        "consent_snapshot_id": "consent-1",
        "policy_epoch": 1,
        "source_generation": SOURCE_GENERATION,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
        grant: True,
    }

    with pytest.raises(ValidationError, match="base storage authorization"):
        MemoryAuthorizationSnapshot.model_validate(values)


def test_memory_authorization_requires_an_ordered_utc_window() -> None:
    with pytest.raises(ValidationError, match="expiry must be after issuance"):
        _authorization(expires_at=ISSUED_AT)

    with pytest.raises(ValidationError, match="timezone"):
        _authorization(issued_at=datetime(2026, 8, 24, 8))


@pytest.mark.parametrize(
    "changes",
    [
        {"vault_id": "vault-b"},
        {"consent_snapshot_id": "consent-2"},
        {"policy_epoch": 2},
        {"source_generation": SOURCE_GENERATION + 1},
        {"issued_at": ISSUED_AT - timedelta(minutes=1)},
        {"expires_at": EXPIRES_AT + timedelta(minutes=1)},
        {"storage_allowed": False},
        {"sensitive_storage_allowed": True},
        {
            "sensitive_storage_allowed": True,
            "highly_sensitive_storage_allowed": True,
        },
        {"proactive_use_allowed": True},
    ],
)
def test_memory_authorization_fingerprint_binds_every_snapshot_field(
    changes: dict[str, object],
) -> None:
    baseline = _authorization()
    changed = MemoryAuthorizationSnapshot.model_validate({**baseline.model_dump(), **changes})

    assert changed.authorization_fingerprint != baseline.authorization_fingerprint


def test_memory_gate_input_binds_generation_confirmation_and_expiry() -> None:
    claim = _claim()
    verification = _verification(claim)
    authorization = _authorization()
    valid = MemoryGateInput(
        claim=claim,
        verification=verification,
        authorization=authorization,
        evaluated_at=ISSUED_AT,
        confirmation_verdict_id="verdict-1",
    )

    assert valid.confirmation_verdict_id == "verdict-1"
    with pytest.raises(ValidationError, match="Source generation"):
        MemoryGateInput(
            claim=claim,
            verification=_verification(claim, source_generation=SOURCE_GENERATION - 1),
            authorization=authorization,
            evaluated_at=EVALUATED_AT,
        )
    with pytest.raises(ValidationError, match="expired"):
        MemoryGateInput(
            claim=claim,
            verification=verification,
            authorization=authorization,
            evaluated_at=EXPIRES_AT,
        )


def test_memory_gate_decision_binds_chain_authorization_time_and_confirmation() -> None:
    context_claim = _context_claim()
    decision = context_claim.decision

    assert decision.candidate_id == context_claim.claim.candidate_id
    assert decision.claim_fingerprint == context_claim.claim.evidence_fingerprint
    assert decision.evidence_fingerprint == context_claim.verification.evidence_fingerprint
    assert decision.authorization_fingerprint == decision.authorization.authorization_fingerprint
    assert decision.evaluated_at == EVALUATED_AT
    assert decision.confirmation_verdict_id == "verdict-1"
    with pytest.raises(ValidationError, match="frozen"):
        decision.confirmation_verdict_id = "verdict-replayed"

    expired = decision.model_dump()
    expired["evaluated_at"] = EXPIRES_AT
    with pytest.raises(ValidationError, match="outside its authorization window"):
        MemoryGateDecision.model_validate(expired)

    replayed_authorization = decision.model_dump()
    replayed_authorization["authorization"] = _authorization(
        consent_snapshot_id="consent-replayed"
    ).model_dump()
    with pytest.raises(ValidationError, match="authorization fingerprint"):
        MemoryGateDecision.model_validate(replayed_authorization)


def test_context_claim_rejects_old_decision_claim_and_evidence_replay() -> None:
    original = _context_claim()

    other_candidate = _claim(candidate_id="candidate-2")
    with pytest.raises(ValidationError, match="targets another candidate"):
        ContextClaim(
            claim=other_candidate,
            verification=_verification(other_candidate),
            decision=original.decision,
            retrieval_fingerprint=RETRIEVAL_FINGERPRINT,
        )

    revised_claim = _claim(canonical_text="用户现在愿意考虑管理岗位")
    with pytest.raises(ValidationError, match="decision claim fingerprint"):
        ContextClaim(
            claim=revised_claim,
            verification=_verification(revised_claim),
            decision=original.decision,
            retrieval_fingerprint=RETRIEVAL_FINGERPRINT,
        )

    reverified = _verification(original.claim, verifier_version="test-verifier-v2")
    with pytest.raises(ValidationError, match="decision evidence fingerprint"):
        ContextClaim(
            claim=original.claim,
            verification=reverified,
            decision=original.decision,
            retrieval_fingerprint=RETRIEVAL_FINGERPRINT,
        )


def test_context_claim_recomputes_and_rejects_a_handmade_active_decision() -> None:
    real_candidate = _context_claim(disposition=MemoryDisposition.CANDIDATE)
    forged_payload = real_candidate.decision.model_dump()
    forged_payload.update(
        disposition=MemoryDisposition.ACTIVE,
        reasons=(MemoryGateReason.EXPLICIT_LOW_SENSITIVITY,),
        requires_confirmation=False,
    )
    forged_active = MemoryGateDecision.model_validate(forged_payload)

    with pytest.raises(ValidationError, match="deterministic MemoryGate verdict"):
        ContextClaim(
            claim=real_candidate.claim,
            verification=real_candidate.verification,
            decision=forged_active,
            retrieval_fingerprint=real_candidate.retrieval_fingerprint,
        )


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


@pytest.mark.parametrize(
    "field_name",
    [
        "vault_id",
        "purpose",
        "consent_snapshot_id",
        "policy_epoch",
        "source_generation",
        "issued_at",
        "expires_at",
    ],
)
def test_context_policy_snapshot_requires_all_replay_binding_fields(field_name: str) -> None:
    payload = _policy().model_dump()
    payload.pop(field_name)

    with pytest.raises(ValidationError):
        ContextPolicySnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"vault_id": "vault-b"},
        {"purpose": RetrievalIntent.RECENT_CONTEXT},
        {"consent_snapshot_id": "consent-2"},
        {"policy_epoch": 2},
        {"source_generation": SOURCE_GENERATION + 1},
        {"issued_at": ISSUED_AT - timedelta(minutes=1)},
        {"expires_at": EXPIRES_AT + timedelta(minutes=1)},
        {"max_sensitivity": SensitivityLevel.NORMAL},
        {"cross_record_analysis_allowed": True},
        {"sensitive_resurface_allowed": True},
    ],
)
def test_context_policy_fingerprint_binds_every_snapshot_field(
    changes: dict[str, object],
) -> None:
    baseline = _policy()
    changed = ContextPolicySnapshot.model_validate({**baseline.model_dump(), **changes})

    assert changed.policy_fingerprint != baseline.policy_fingerprint


def test_context_policy_snapshot_requires_an_ordered_utc_window() -> None:
    with pytest.raises(ValidationError, match="expiry must be after issuance"):
        _policy(expires_at=ISSUED_AT)

    with pytest.raises(ValidationError, match="timezone"):
        _policy(expires_at=datetime(2026, 8, 24, 10))


@pytest.mark.parametrize(
    "field_name",
    ["policy_fingerprint", "retrieval_fingerprint", "created_at"],
)
def test_context_pack_requires_policy_fingerprint_and_creation_time(field_name: str) -> None:
    policy = _policy()
    payload = {
        "vault_id": "vault-a",
        "version": "context-v1",
        "purpose": RetrievalIntent.FACT_LOOKUP,
        "policy_snapshot": policy,
        "policy_fingerprint": policy.policy_fingerprint,
        "retrieval_fingerprint": RETRIEVAL_FINGERPRINT,
        "created_at": EVALUATED_AT,
    }
    payload.pop(field_name)

    with pytest.raises(ValidationError):
        ContextPack.model_validate(payload)


def test_context_pack_binds_policy_vault_and_purpose() -> None:
    with pytest.raises(ValidationError, match="share a vault"):
        _context_pack(policy_snapshot=_policy(vault_id="vault-b"))

    with pytest.raises(ValidationError, match="share a purpose"):
        _context_pack(
            purpose=RetrievalIntent.FACT_LOOKUP,
            policy_snapshot=_policy(purpose=RetrievalIntent.RECENT_CONTEXT),
        )


def test_context_pack_rejects_stale_source_generation() -> None:
    active = _context_claim()

    with pytest.raises(ValidationError, match="stale Source generation"):
        _context_pack(
            policy_snapshot=_policy(source_generation=SOURCE_GENERATION + 1),
            confirmed_claims=(active,),
        )


def test_context_pack_rejects_policy_snapshot_replay_and_expiry_boundary() -> None:
    original = _policy()
    replayed = _policy(policy_epoch=original.policy_epoch + 1)

    with pytest.raises(ValidationError, match="policy fingerprint"):
        _context_pack(
            policy_snapshot=replayed,
            policy_fingerprint=original.policy_fingerprint,
        )
    assert _context_pack(policy_snapshot=original, created_at=ISSUED_AT).created_at == ISSUED_AT
    with pytest.raises(ValidationError, match="expired"):
        _context_pack(policy_snapshot=original, created_at=EXPIRES_AT)


def test_context_pack_citations_must_reference_included_source_quotes() -> None:
    included = _span("第一条原文", fragment_id="included")
    absent = _span("未纳入的原文", fragment_id="absent")
    included_quote = _source_quote(included, record_id="record-included")

    with pytest.raises(ValidationError, match="exact retrieved Source quote"):
        _context_pack(
            source_quotes=(included_quote,),
            citation_map=(_record_citation(included_quote, source_span=absent),),
        )

    citation = _record_citation(included_quote)
    context = _context_pack(
        source_quotes=(included_quote,),
        citation_map=(citation,),
    )
    assert context.citation_map == (citation,)


def test_context_pack_binds_claim_quotes_and_citations_to_one_retrieval_result() -> None:
    foreign_claim = _context_claim(retrieval_fingerprint=OTHER_RETRIEVAL_FINGERPRINT)
    with pytest.raises(ValidationError, match="claim belongs to another retrieval result"):
        _context_pack(confirmed_claims=(foreign_claim,))

    foreign_quote = _source_quote(
        _span("外部检索原文", fragment_id="foreign-retrieval"),
        record_id="record-foreign",
        retrieval_fingerprint=OTHER_RETRIEVAL_FINGERPRINT,
    )
    with pytest.raises(ValidationError, match="content belongs to another retrieval result"):
        _context_pack(
            source_quotes=(foreign_quote,),
            citation_map=(_record_citation(foreign_quote),),
        )

    local_quote = _source_quote(
        _span("本次检索原文", fragment_id="local-retrieval"),
        record_id="record-local",
    )
    with pytest.raises(ValidationError, match="content belongs to another retrieval result"):
        _context_pack(
            source_quotes=(local_quote,),
            citation_map=(
                _record_citation(
                    local_quote,
                    retrieval_fingerprint=OTHER_RETRIEVAL_FINGERPRINT,
                ),
            ),
        )


def test_claim_citation_is_bound_to_the_exact_verifier_output() -> None:
    context_claim = _context_claim()
    quote = _source_quote(
        context_claim.verification.evidence[0].source_span,
        record_id="record-claim-evidence",
    )
    forged_claim_citation = _claim_citation(
        context_claim,
        quote,
        verification_fingerprint="0" * 64,
    )

    with pytest.raises(ValidationError, match="another verifier output"):
        _context_pack(
            confirmed_claims=(context_claim,),
            source_quotes=(quote,),
            citation_map=(
                _record_citation(quote),
                forged_claim_citation,
            ),
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("object_type", "claim"),
        ("object_id", "未类型化的自然语言正文"),
    ],
)
def test_citation_target_requires_a_typed_technical_object_id(
    field_name: str,
    invalid_value: str,
) -> None:
    payload = {
        "object_type": CitationObjectType.RETRIEVAL_RECORD,
        "object_id": "record-1",
    }
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError):
        CitationTarget.model_validate(payload)


def test_unretrieved_record_id_cannot_borrow_a_benign_retrieved_span() -> None:
    quote = _source_quote(
        _span("无害的已检索原文", fragment_id="benign"),
        record_id="record-benign",
    )
    masquerading = CitationBinding(
        target=CitationTarget(
            object_type=CitationObjectType.RETRIEVAL_RECORD,
            object_id="record-not-retrieved",
        ),
        retrieval_record_id="record-not-retrieved",
        source_span=quote.source_span,
        retrieval_fingerprint=RETRIEVAL_FINGERPRINT,
    )

    with pytest.raises(ValidationError, match="exact retrieved Source quote"):
        _context_pack(
            source_quotes=(quote,),
            citation_map=(masquerading,),
        )


def test_absent_claim_and_benign_span_cannot_masquerade_as_verified_evidence() -> None:
    context_claim = _context_claim()
    benign_quote = _source_quote(
        _span("无害但与 claim 无关的原文", fragment_id="unrelated"),
        record_id="record-unrelated",
    )
    absent_claim_citation = CitationBinding(
        target=CitationTarget(
            object_type=CitationObjectType.CANDIDATE_CLAIM,
            object_id="candidate-not-in-pack",
        ),
        retrieval_record_id=benign_quote.record_id,
        source_span=benign_quote.source_span,
        retrieval_fingerprint=RETRIEVAL_FINGERPRINT,
        verification_fingerprint=context_claim.verification.evidence_fingerprint,
    )
    with pytest.raises(ValidationError, match="claim absent from this ContextPack"):
        _context_pack(
            source_quotes=(benign_quote,),
            citation_map=(
                _record_citation(benign_quote),
                absent_claim_citation,
            ),
        )

    unrelated_evidence = _claim_citation(
        context_claim,
        benign_quote,
    )
    with pytest.raises(ValidationError, match="not part of its verified evidence"):
        _context_pack(
            confirmed_claims=(context_claim,),
            source_quotes=(benign_quote,),
            citation_map=(
                _record_citation(benign_quote),
                unrelated_evidence,
            ),
        )


@pytest.mark.parametrize("reverse_order", [False, True])
def test_context_pack_rejects_source_identity_conflicts_in_either_order(
    reverse_order: bool,
) -> None:
    included = _span("第一条原文", fragment_id="same-anchor")
    conflicting = _span("伪造的原文", fragment_id="same-anchor")
    quotes: tuple[ContextSourceQuote, ...] = (
        _source_quote(included, record_id="record-original"),
        _source_quote(conflicting, record_id="record-conflicting"),
    )
    if reverse_order:
        quotes = tuple(reversed(quotes))

    assert len(included.quote) == len(conflicting.quote)
    assert included.quote_hash != conflicting.quote_hash
    with pytest.raises(ValidationError, match="conflicting text, hash, or kind"):
        _context_pack(source_quotes=quotes)


def test_context_pack_rejects_high_sensitivity_source_quote_under_normal_policy() -> None:
    sensitive_quote = _source_quote(
        _span("高敏原文", fragment_id="sensitive"),
        record_id="record-sensitive",
        sensitivity=SensitivityLevel.HIGHLY_SENSITIVE,
    )

    with pytest.raises(ValidationError, match="source quote exceeds"):
        _context_pack(
            policy_snapshot=_policy(max_sensitivity=SensitivityLevel.NORMAL),
            source_quotes=(sensitive_quote,),
        )


def test_context_pack_rejects_artifacts_as_quotes_or_claim_evidence() -> None:
    artifact_span = _span("AI 生成的总结", source_kind=SourceKind.ARTIFACT)

    with pytest.raises(ValidationError, match="cannot enter a ContextPack"):
        _context_pack(
            source_quotes=(_source_quote(artifact_span),),
        )

    artifact_claim = _claim(artifact_span)
    with pytest.raises(ValidationError, match="policy-rejected claims"):
        _context_pack(
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
            retrieval_fingerprint=RETRIEVAL_FINGERPRINT,
        )

    with pytest.raises(ValidationError, match="non-persistent"):
        _context_claim(claim, disposition=MemoryDisposition.NON_PERSISTENT)

    policy = _policy()
    with pytest.raises(ValidationError):
        ContextPack.model_validate(
            {
                "vault_id": "vault-a",
                "version": "context-v1",
                "purpose": RetrievalIntent.FACT_LOOKUP,
                "policy_snapshot": policy,
                "policy_fingerprint": policy.policy_fingerprint,
                "retrieval_fingerprint": RETRIEVAL_FINGERPRINT,
                "created_at": EVALUATED_AT,
                "confirmed_claims": (claim,),
            }
        )


def test_context_pack_keeps_active_and_candidate_claims_in_their_own_lanes() -> None:
    active = _context_claim(disposition=MemoryDisposition.ACTIVE)
    candidate = _context_claim(
        _claim(candidate_id="candidate-2"),
        disposition=MemoryDisposition.CANDIDATE,
    )

    with pytest.raises(ValidationError, match="confirmed ContextPack claims must be active"):
        _context_pack(
            confirmed_claims=(candidate,),
        )

    with pytest.raises(ValidationError, match="candidate ContextPack claims must remain"):
        _context_pack(
            candidate_claims=(active,),
        )

    context = _context_pack(
        confirmed_claims=(active,),
        candidate_claims=(candidate,),
    )
    assert context.confirmed_claims == (active,)
    assert context.candidate_claims == (candidate,)


def test_context_pack_requires_a_separate_sensitive_resurface_grant() -> None:
    claim = _claim(sensitivity=SensitivityLevel.SENSITIVE)
    active = _context_claim(claim)

    with pytest.raises(ValidationError, match="separate resurface grant"):
        _context_pack(
            confirmed_claims=(active,),
        )

    context = _context_pack(
        policy_snapshot=_policy(sensitive_resurface_allowed=True),
        confirmed_claims=(active,),
    )
    assert context.confirmed_claims == (active,)

    with pytest.raises(ValidationError, match="sensitivity ceiling"):
        _context_pack(
            policy_snapshot=_policy(
                sensitive_resurface_allowed=True,
                max_sensitivity=SensitivityLevel.NORMAL,
            ),
            confirmed_claims=(active,),
        )


@pytest.mark.parametrize("coverage", [{}, {"counterevidence_searched": False}])
def test_pattern_reflection_requires_a_completed_counterevidence_pass(
    coverage: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError, match="counterevidence pass"):
        _context_pack(
            purpose=RetrievalIntent.PATTERN_REFLECTION,
            policy_snapshot=_policy(
                purpose=RetrievalIntent.PATTERN_REFLECTION,
                cross_record_analysis_allowed=True,
            ),
            coverage=coverage,
        )


def test_pattern_reflection_allows_an_empty_but_completed_counterevidence_pass() -> None:
    context = _context_pack(
        purpose=RetrievalIntent.PATTERN_REFLECTION,
        policy_snapshot=_policy(
            purpose=RetrievalIntent.PATTERN_REFLECTION,
            cross_record_analysis_allowed=True,
        ),
        coverage={"counterevidence_searched": True},
    )

    assert context.counterevidence == ()
    assert context.coverage["counterevidence_searched"] is True


def test_pattern_reflection_requires_cross_record_consent_separately() -> None:
    with pytest.raises(ValidationError, match="cross-record analysis consent"):
        _context_pack(
            purpose=RetrievalIntent.PATTERN_REFLECTION,
            policy_snapshot=_policy(purpose=RetrievalIntent.PATTERN_REFLECTION),
            coverage={"counterevidence_searched": True},
        )
