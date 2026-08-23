"""Pure evidence-verification and memory-policy pipelines.

The verifier proves only that a candidate is anchored to the supplied Source
text.  It does not decide whether the user's account is objectively true.  The
memory gate then makes a deterministic persistence decision; neither component
performs I/O or accepts model-selected tools.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from .contracts import (
    Attribution,
    CandidateClaim,
    ClaimKind,
    ClaimSafetyFlag,
    ContractModel,
    DerivationType,
    EvidenceIssue,
    EvidenceIssueCode,
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
    SensitivityLevel,
    Sha256Hex,
    SourceKind,
    SourceSpan,
    TechnicalId,
)

_NEGATION = re.compile(
    r"(?:不|没(?:有)?|无|并非|从未|别|不能|不会|不想|不喜欢|"
    r"\b(?:not|never|no longer|do not|does not|did not|don't|doesn't|didn't|"
    r"cannot|can't|won't)\b)",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(r"(?:如果|要是|假如|除非|\b(?:if|unless|provided that)\b)", re.I)
_UNCERTAINTY = re.compile(
    r"(?:也许|可能|大概|似乎|好像|不确定|\b(?:maybe|perhaps|possibly|seems?|unsure)\b)",
    re.I,
)
_QUOTED_OTHER = re.compile(
    r"(?:妈妈|母亲|爸爸|父亲|老板|老师|医生|朋友|同事|伴侣|他|她|他们)"
    r".{0,12}(?:说|认为|觉得|告诉|评价)|"
    r"\b(?:my (?:mother|father|boss|friend|doctor)|he|she|they)\s+"
    r"(?:said|says|thought|thinks|told)\b",
    re.I,
)
_MEMORY_REQUEST = re.compile(r"(?:请|帮我)?记住|请保存|\bremember (?:this|that|my)\b", re.I)


class EvidenceSource(ContractModel):
    """A currently authorized Source fragment supplied by a repository adapter."""

    vault_id: TechnicalId
    source_document_id: TechnicalId
    source_revision_id: TechnicalId
    source_fragment_id: TechnicalId
    source_generation: int = Field(ge=0)
    text: str
    text_hash: Sha256Hex
    source_kind: SourceKind = SourceKind.SOURCE
    consent_allowed: bool = False
    deleted: bool = False

    @model_validator(mode="after")
    def primary_source_only_when_available(self) -> Self:
        expected_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_hash != expected_hash:
            raise ValueError("text_hash must be the SHA-256 digest of Source text")
        if self.source_kind is SourceKind.ARTIFACT and self.consent_allowed:
            # Keeping this explicit avoids an adapter accidentally presenting an
            # Artifact as evidence merely by setting the normal consent flag.
            object.__setattr__(self, "consent_allowed", False)
        return self


@runtime_checkable
class EvidenceVerifier(Protocol):
    """Provider-neutral port for validating candidate-to-Source support."""

    verifier_version: str

    def verify(
        self,
        claim: CandidateClaim,
        sources: Sequence[EvidenceSource],
        *,
        source_generation: int,
        counterevidence_spans: Sequence[SourceSpan] = (),
    ) -> EvidenceVerificationResult:
        """Validate evidence anchors without treating the candidate as evidence."""


class ExactSpanEvidenceVerifier:
    """Deterministic verifier for offsets, provenance, and preserved qualifiers.

    This verifier is intentionally conservative.  It validates exact Source
    anchors and common omission hazards (negation, conditions, uncertainty, and
    quoted-other attribution).  A production semantic verifier can implement the
    same protocol, but still has to return this evidence contract.
    """

    verifier_version = "exact-span-v1"

    def verify(
        self,
        claim: CandidateClaim,
        sources: Sequence[EvidenceSource],
        *,
        source_generation: int,
        counterevidence_spans: Sequence[SourceSpan] = (),
    ) -> EvidenceVerificationResult:
        if source_generation < 0:
            raise ValueError("source_generation must be non-negative")
        claim = CandidateClaim.model_validate_json(claim.model_dump_json(), strict=True)
        sources = tuple(
            EvidenceSource.model_validate_json(source.model_dump_json(), strict=True)
            for source in sources
        )
        counterevidence_spans = tuple(
            SourceSpan.model_validate_json(span.model_dump_json(), strict=True)
            for span in counterevidence_spans
        )
        evidence: list[EvidenceItem] = []
        issues: list[EvidenceIssue] = []
        source_index: dict[tuple[str, str, str, str], EvidenceSource] = {}
        conflicting_source_keys: set[tuple[str, str, str, str]] = set()
        for source in sources:
            if source.source_generation != source_generation:
                issues.append(
                    EvidenceIssue(
                        code=EvidenceIssueCode.SOURCE_GENERATION_MISMATCH,
                        message="Source fragment does not belong to the authorized generation",
                    )
                )
                continue
            key = (
                source.vault_id,
                source.source_document_id,
                source.source_revision_id,
                source.source_fragment_id,
            )
            if key in conflicting_source_keys:
                continue
            previous = source_index.get(key)
            if previous is None:
                source_index[key] = source
            elif previous != source:
                source_index.pop(key)
                conflicting_source_keys.add(key)
                issues.append(
                    EvidenceIssue(
                        code=EvidenceIssueCode.SOURCE_IDENTITY_CONFLICT,
                        message="duplicate Source identity has conflicting content or policy state",
                    )
                )

        for span in claim.source_spans:
            matched_source = source_index.get(
                (
                    span.vault_id,
                    span.source_document_id,
                    span.source_revision_id,
                    span.source_fragment_id,
                )
            )
            span_issues = self._validate_span(span, matched_source)
            issues.extend(span_issues)
            if not any(issue.fatal for issue in span_issues):
                evidence.append(
                    EvidenceItem(
                        relation=(
                            EvidenceRelation.SUPPORTS
                            if claim.derivation is DerivationType.EXPLICIT
                            else EvidenceRelation.CONTEXTUALIZES
                        ),
                        source_span=span,
                        strength=_strength_for(claim.derivation),
                        reason="candidate is anchored to an authorized primary Source span",
                    )
                )

        if evidence:
            combined_quote = "\n".join(item.source_span.quote for item in evidence)
            issues.extend(_qualifier_issues(claim, combined_quote))
            if claim.derivation is DerivationType.EXPLICIT:
                if _normalize_evidence_text(claim.canonical_text) not in _normalize_evidence_text(
                    combined_quote
                ):
                    issues.append(
                        EvidenceIssue(
                            code=EvidenceIssueCode.CLAIM_TEXT_MISMATCH,
                            message="explicit claim text is not present in its Source spans",
                            field_path="canonical_text",
                        )
                    )
            else:
                issues.append(
                    EvidenceIssue(
                        code=EvidenceIssueCode.SEMANTIC_REVIEW_REQUIRED,
                        message="paraphrase or inference requires an independent semantic verifier",
                        field_path="derivation",
                        fatal=False,
                    )
                )
            if claim.explicit_memory_request and not _MEMORY_REQUEST.search(combined_quote):
                issues.append(
                    EvidenceIssue(
                        code=EvidenceIssueCode.MEMORY_REQUEST_MISMATCH,
                        message="Source span does not contain an explicit memory request",
                        field_path="explicit_memory_request",
                    )
                )
            issues.extend(_temporal_issues(claim, combined_quote))
            issues.extend(
                _structured_payload_issues(
                    claim,
                    tuple(item.source_span for item in evidence),
                )
            )

        counterevidence: list[EvidenceItem] = []
        for span in counterevidence_spans:
            matched_source = source_index.get(
                (
                    span.vault_id,
                    span.source_document_id,
                    span.source_revision_id,
                    span.source_fragment_id,
                )
            )
            span_issues = self._validate_span(span, matched_source)
            issues.extend(span_issues)
            if not any(issue.fatal for issue in span_issues):
                counterevidence.append(
                    EvidenceItem(
                        relation=EvidenceRelation.CONTRADICTS,
                        source_span=span,
                        strength=EvidenceStrength.MODERATE,
                        reason="authorized Source was supplied by the counterevidence pass",
                    )
                )

        if counterevidence:
            issues.append(
                EvidenceIssue(
                    code=EvidenceIssueCode.COUNTEREVIDENCE_FOUND,
                    message="counterevidence requires the candidate to remain reviewable",
                    fatal=False,
                )
            )

        fatal = any(issue.fatal for issue in issues)
        if fatal or not evidence:
            status = EvidenceStatus.UNSUPPORTED
        elif (
            counterevidence
            or claim.derivation is not DerivationType.EXPLICIT
            or any(issue.code is EvidenceIssueCode.SEMANTIC_REVIEW_REQUIRED for issue in issues)
        ):
            status = EvidenceStatus.NEEDS_REVIEW
        else:
            status = EvidenceStatus.SUPPORTED

        return EvidenceVerificationResult(
            vault_id=claim.vault_id,
            candidate_id=claim.candidate_id,
            claim_fingerprint=claim.evidence_fingerprint,
            source_generation=source_generation,
            status=status,
            evidence=tuple(evidence),
            counterevidence=tuple(counterevidence),
            issues=tuple(issues),
            verifier_version=self.verifier_version,
            eligible_for_display=status is EvidenceStatus.SUPPORTED and not fatal,
        )

    @staticmethod
    def _validate_span(
        span: SourceSpan,
        source: EvidenceSource | None,
    ) -> tuple[EvidenceIssue, ...]:
        if span.source_kind is SourceKind.ARTIFACT:
            return (
                EvidenceIssue(
                    code=EvidenceIssueCode.ARTIFACT_IS_NOT_EVIDENCE,
                    message="an AI Artifact cannot be used as Source evidence",
                    source_span=span,
                ),
            )
        if source is None or source.deleted or not source.consent_allowed:
            return (
                EvidenceIssue(
                    code=EvidenceIssueCode.SOURCE_UNAVAILABLE,
                    message="the referenced Source is absent, deleted, or not authorized",
                    source_span=span,
                ),
            )
        if source.source_kind is SourceKind.ARTIFACT:
            return (
                EvidenceIssue(
                    code=EvidenceIssueCode.ARTIFACT_IS_NOT_EVIDENCE,
                    message="an AI Artifact cannot be used as Source evidence",
                    source_span=span,
                ),
            )
        identifiers_match = (
            source.vault_id == span.vault_id
            and source.source_document_id == span.source_document_id
            and source.source_revision_id == span.source_revision_id
            and source.source_fragment_id == span.source_fragment_id
        )
        in_bounds = span.char_end <= len(source.text)
        quote_matches = in_bounds and source.text[span.char_start : span.char_end] == span.quote
        hash_matches = hashlib.sha256(span.quote.encode("utf-8")).hexdigest() == span.quote_hash
        if not identifiers_match or not quote_matches or not hash_matches:
            return (
                EvidenceIssue(
                    code=EvidenceIssueCode.SPAN_MISMATCH,
                    message="Source ids, offsets, quote, or quote hash do not match",
                    source_span=span,
                ),
            )
        return ()


def decide_memory(input_: MemoryGateInput) -> MemoryGateDecision:
    """Apply the fail-closed, user-governed memory gate as a pure function."""

    input_ = MemoryGateInput.model_validate_json(input_.model_dump_json(), strict=True)
    claim = input_.claim
    verification = input_.verification
    flags = claim.safety_flags

    hard_reasons: list[MemoryGateReason] = []
    authorization = input_.authorization
    if not authorization.storage_allowed:
        hard_reasons.append(MemoryGateReason.POLICY_FORBIDS_STORAGE)
    if ClaimSafetyFlag.ARTIFACT_SOURCE in flags or not claim.uses_only_primary_sources:
        hard_reasons.append(MemoryGateReason.ARTIFACT_SOURCE)
    if ClaimSafetyFlag.DIAGNOSTIC_LANGUAGE in flags:
        hard_reasons.append(MemoryGateReason.DIAGNOSTIC_LANGUAGE)
    if ClaimSafetyFlag.SAFETY_RISK_LABEL in flags:
        hard_reasons.append(MemoryGateReason.SAFETY_RISK_LABEL)
    if (
        claim.attribution is Attribution.MODEL_HYPOTHESIS
        and ClaimSafetyFlag.PERSONALITY_INFERENCE in flags
    ):
        hard_reasons.append(MemoryGateReason.MODEL_ORIGIN_POLICY_REJECTED)
    if ClaimSafetyFlag.UNTRUSTED_INSTRUCTION in flags:
        hard_reasons.append(MemoryGateReason.UNTRUSTED_INSTRUCTION)
    effectively_highly_sensitive = (
        claim.sensitivity is SensitivityLevel.HIGHLY_SENSITIVE
        or ClaimSafetyFlag.HIGHLY_SENSITIVE in flags
    )
    if effectively_highly_sensitive and not authorization.highly_sensitive_storage_allowed:
        hard_reasons.extend(
            [
                MemoryGateReason.HIGHLY_SENSITIVE,
                MemoryGateReason.SENSITIVE_PERMISSION_REQUIRED,
            ]
        )
    elif (
        claim.sensitivity is SensitivityLevel.SENSITIVE
        and not authorization.sensitive_storage_allowed
    ):
        hard_reasons.append(MemoryGateReason.SENSITIVE_PERMISSION_REQUIRED)
    if not verification.evidence:
        hard_reasons.append(MemoryGateReason.EVIDENCE_MISSING)
    if verification.status is EvidenceStatus.UNSUPPORTED or any(
        issue.fatal for issue in verification.issues
    ):
        hard_reasons.append(MemoryGateReason.EVIDENCE_REJECTED)
    if hard_reasons:
        return _decision(input_, MemoryDisposition.NON_PERSISTENT, hard_reasons)

    if claim.claim_kind in {
        ClaimKind.EMOTION,
        ClaimKind.THOUGHT,
        ClaimKind.CONCERN,
        ClaimKind.IDEA,
    }:
        return _decision(
            input_,
            MemoryDisposition.NON_PERSISTENT,
            [MemoryGateReason.EPISODIC_ONLY],
        )

    candidate_reasons: list[MemoryGateReason] = []
    if verification.status is not EvidenceStatus.SUPPORTED:
        candidate_reasons.append(MemoryGateReason.CONFIRMATION_REQUIRED)
    if verification.counterevidence:
        candidate_reasons.append(MemoryGateReason.CONFIRMATION_REQUIRED)
    if claim.claim_kind is ClaimKind.WISH:
        candidate_reasons.append(MemoryGateReason.WISH_IS_NOT_COMMITMENT)
    if claim.conditional or claim.uncertainty_text is not None:
        candidate_reasons.append(MemoryGateReason.CONFIRMATION_REQUIRED)

    if effectively_highly_sensitive:
        candidate_reasons.append(MemoryGateReason.HIGHLY_SENSITIVE)
        # Even with a storage grant, automatic activation is forbidden.  A
        # separate explicit user verdict may create a governed active version.
        candidate_reasons.append(MemoryGateReason.CONFIRMATION_REQUIRED)

    identity_or_commitment = claim.claim_kind in {
        ClaimKind.GOAL,
        ClaimKind.COMMITMENT,
        ClaimKind.VALUE,
        ClaimKind.RELATIONSHIP,
        ClaimKind.SELF_DESCRIPTION,
        ClaimKind.PATTERN_HYPOTHESIS,
        ClaimKind.WISH,
    }
    user_confirmed = input_.confirmation_verdict_id is not None
    if identity_or_commitment and not user_confirmed:
        candidate_reasons.append(MemoryGateReason.CONFIRMATION_REQUIRED)

    inference = claim.derivation is DerivationType.INFERENCE
    personality_inference = ClaimSafetyFlag.PERSONALITY_INFERENCE in flags
    if inference and not user_confirmed:
        candidate_reasons.append(MemoryGateReason.CONFIRMATION_REQUIRED)
    if personality_inference or claim.claim_kind is ClaimKind.PATTERN_HYPOTHESIS:
        candidate_reasons.append(MemoryGateReason.MULTI_SOURCE_CONFIRMATION_REQUIRED)

    if candidate_reasons:
        return _decision(
            input_,
            MemoryDisposition.CANDIDATE,
            candidate_reasons,
            requires_confirmation=True,
        )

    active_reasons: list[MemoryGateReason] = []
    if user_confirmed:
        active_reasons.append(MemoryGateReason.USER_CONFIRMED)
    elif claim.explicit_memory_request and claim.attribution is Attribution.SELF_REPORT:
        active_reasons.append(MemoryGateReason.EXPLICIT_MEMORY_REQUEST)
    elif (
        claim.derivation is DerivationType.EXPLICIT
        and claim.attribution is Attribution.SELF_REPORT
        and claim.sensitivity is SensitivityLevel.NORMAL
        and claim.claim_kind in {ClaimKind.EXPLICIT_FACT, ClaimKind.PREFERENCE}
    ):
        active_reasons.append(MemoryGateReason.EXPLICIT_LOW_SENSITIVITY)
    else:
        return _decision(
            input_,
            MemoryDisposition.CANDIDATE,
            [MemoryGateReason.CONFIRMATION_REQUIRED],
            requires_confirmation=True,
        )

    return _decision(
        input_,
        MemoryDisposition.ACTIVE,
        active_reasons,
        may_use_proactively=authorization.proactive_use_allowed,
    )


class MemoryGate:
    """Stateless object form of :func:`decide_memory` for dependency injection."""

    def decide(self, input_: MemoryGateInput) -> MemoryGateDecision:
        return decide_memory(input_)


class MemoryIngestionResult(ContractModel):
    """Auditable result of candidate -> verification -> policy gate."""

    claim: CandidateClaim
    verification: EvidenceVerificationResult
    decision: MemoryGateDecision
    todo_eligible: bool = False

    @model_validator(mode="after")
    def preserve_bound_pipeline_result(self) -> Self:
        if self.decision.candidate_id != self.claim.candidate_id:
            raise ValueError("memory decision targets another candidate")
        if self.decision.claim_fingerprint != self.claim.evidence_fingerprint:
            raise ValueError("memory decision claim fingerprint does not match")
        if self.decision.evidence_fingerprint != self.verification.evidence_fingerprint:
            raise ValueError("memory decision evidence fingerprint does not match")
        if self.verification.source_generation != self.decision.authorization.source_generation:
            raise ValueError("memory decision uses evidence from another Source generation")
        expected = decide_memory(
            MemoryGateInput(
                claim=self.claim,
                verification=self.verification,
                authorization=self.decision.authorization,
                evaluated_at=self.decision.evaluated_at,
                confirmation_verdict_id=self.decision.confirmation_verdict_id,
            )
        )
        if self.decision != expected:
            raise ValueError("memory decision does not match the deterministic gate verdict")
        return self


class MemoryIngestionPipeline:
    """Pure orchestration that never lets model output skip evidence verification."""

    def __init__(
        self,
        verifier: EvidenceVerifier | None = None,
        gate: MemoryGate | None = None,
    ) -> None:
        self._verifier = verifier or ExactSpanEvidenceVerifier()
        self._gate = gate or MemoryGate()

    def evaluate(
        self,
        claim: CandidateClaim,
        sources: Sequence[EvidenceSource],
        *,
        counterevidence_spans: Sequence[SourceSpan] = (),
        authorization: MemoryAuthorizationSnapshot,
        evaluated_at: datetime,
        confirmation_verdict_id: TechnicalId | None = None,
    ) -> MemoryIngestionResult:
        claim = CandidateClaim.model_validate_json(claim.model_dump_json(), strict=True)
        sources = tuple(
            EvidenceSource.model_validate_json(source.model_dump_json(), strict=True)
            for source in sources
        )
        counterevidence_spans = tuple(
            SourceSpan.model_validate_json(span.model_dump_json(), strict=True)
            for span in counterevidence_spans
        )
        verification = self._verifier.verify(
            claim,
            sources,
            source_generation=authorization.source_generation,
            counterevidence_spans=counterevidence_spans,
        )
        gate_input = MemoryGateInput(
            claim=claim,
            verification=verification,
            authorization=authorization,
            evaluated_at=evaluated_at,
            confirmation_verdict_id=confirmation_verdict_id,
        )
        decision = self._gate.decide(gate_input)
        return MemoryIngestionResult(
            claim=claim,
            verification=verification,
            decision=decision,
            todo_eligible=is_todo_eligible(gate_input, decision),
        )


def is_todo_eligible(input_: MemoryGateInput, decision: MemoryGateDecision) -> bool:
    """Return whether an already confirmed commitment may create a task.

    Wishes, goals, concerns, model suggestions, and unconfirmed commitments are
    deliberately false.  Task creation itself remains outside the AI pipeline.
    """

    return (
        input_.claim.claim_kind is ClaimKind.COMMITMENT
        and input_.confirmation_verdict_id is not None
        and decision.disposition is MemoryDisposition.ACTIVE
    )


def _decision(
    input_: MemoryGateInput,
    disposition: MemoryDisposition,
    reasons: Sequence[MemoryGateReason],
    *,
    requires_confirmation: bool = False,
    may_use_proactively: bool = False,
) -> MemoryGateDecision:
    return MemoryGateDecision(
        candidate_id=input_.claim.candidate_id,
        claim_fingerprint=input_.claim.evidence_fingerprint,
        evidence_fingerprint=input_.verification.evidence_fingerprint,
        authorization=input_.authorization,
        authorization_fingerprint=input_.authorization.authorization_fingerprint,
        confirmation_verdict_id=input_.confirmation_verdict_id,
        evaluated_at=input_.evaluated_at,
        disposition=disposition,
        reasons=tuple(dict.fromkeys(reasons)),
        requires_confirmation=requires_confirmation,
        may_use_proactively=may_use_proactively,
    )


def _strength_for(derivation: DerivationType) -> EvidenceStrength:
    return {
        DerivationType.EXPLICIT: EvidenceStrength.STRONG,
        DerivationType.PARAPHRASE: EvidenceStrength.MODERATE,
        DerivationType.INFERENCE: EvidenceStrength.WEAK,
    }[derivation]


def _qualifier_issues(claim: CandidateClaim, quote: str) -> tuple[EvidenceIssue, ...]:
    issues: list[EvidenceIssue] = []
    quote_is_negated = bool(_NEGATION.search(quote))
    if quote_is_negated != claim.negated:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.NEGATION_OMITTED,
                message="candidate polarity does not preserve Source negation",
                field_path="negated",
            )
        )
    quote_is_conditional = bool(_CONDITIONAL.search(quote))
    if quote_is_conditional != claim.conditional:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.CONDITION_OMITTED,
                message="candidate condition does not match the Source",
                field_path="conditional",
            )
        )
    quote_is_uncertain = bool(_UNCERTAINTY.search(quote))
    claim_is_uncertain = claim.uncertainty_text is not None
    if quote_is_uncertain != claim_is_uncertain or (
        claim.derivation is DerivationType.EXPLICIT
        and claim.uncertainty_text is not None
        and _normalize_evidence_text(claim.uncertainty_text) not in _normalize_evidence_text(quote)
    ):
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.UNCERTAINTY_OMITTED,
                message="candidate uncertainty does not match the Source",
                field_path="uncertainty_text",
            )
        )
    quote_is_attributed_to_other = bool(_QUOTED_OTHER.search(quote))
    claim_is_attributed_to_other = claim.attribution is Attribution.QUOTED_OTHER
    if quote_is_attributed_to_other != claim_is_attributed_to_other:
        issues.append(
            EvidenceIssue(
                code=EvidenceIssueCode.ATTRIBUTION_MISMATCH,
                message="a quoted other person's view cannot become a self-report",
                field_path="attribution",
            )
        )
    return tuple(issues)


def _temporal_issues(claim: CandidateClaim, quote: str) -> tuple[EvidenceIssue, ...]:
    if claim.temporal_context is None:
        return ()
    normalized_quote = _normalize_evidence_text(quote)
    ranges = (
        claim.temporal_context.event_time,
        claim.temporal_context.valid_time,
    )
    for value in ranges:
        if value is None:
            continue
        expression = value.original_expression
        if expression is None or _normalize_evidence_text(expression) not in normalized_quote:
            return (
                EvidenceIssue(
                    code=EvidenceIssueCode.TIME_MISMATCH,
                    message="resolved time is not anchored to its original Source expression",
                    field_path="temporal_context",
                ),
            )
    return ()


def _structured_payload_issues(
    claim: CandidateClaim,
    verified_spans: Sequence[SourceSpan],
) -> tuple[EvidenceIssue, ...]:
    if not claim.structured_payload:
        return ()

    issues: list[EvidenceIssue] = []
    for field_name, value in claim.structured_payload.items():
        field_path = f"structured_payload.{field_name}"
        field_spans = tuple(span for span in verified_spans if span.field_path == field_path)
        if not field_spans:
            issues.append(
                EvidenceIssue(
                    code=EvidenceIssueCode.MISSING_SPAN,
                    message="structured claim field lacks a verified Source span",
                    field_path=field_path,
                )
            )
            continue
        if claim.derivation is not DerivationType.EXPLICIT:
            continue

        normalized_quote = _normalize_evidence_text("\n".join(span.quote for span in field_spans))
        tokens = tuple(_explicit_payload_tokens(value))
        if not tokens:
            issues.append(
                EvidenceIssue(
                    code=EvidenceIssueCode.SEMANTIC_REVIEW_REQUIRED,
                    message="structured field cannot be verified by exact lexical comparison",
                    field_path=field_path,
                    fatal=False,
                )
            )
            continue
        if any(_normalize_evidence_text(token) not in normalized_quote for token in tokens):
            issues.append(
                EvidenceIssue(
                    code=EvidenceIssueCode.CLAIM_TEXT_MISMATCH,
                    message="structured field value is not present in its Source span",
                    field_path=field_path,
                )
            )
    return tuple(issues)


def _explicit_payload_tokens(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, bool) or value is None:
        return ()
    if isinstance(value, (int, float)):
        return (str(value),)
    if isinstance(value, list):
        return tuple(token for item in value for token in _explicit_payload_tokens(item))
    if isinstance(value, dict):
        return tuple(token for item in value.values() for token in _explicit_payload_tokens(item))
    return ()


def _normalize_evidence_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


__all__ = [
    "EvidenceSource",
    "EvidenceVerifier",
    "ExactSpanEvidenceVerifier",
    "MemoryGate",
    "MemoryIngestionPipeline",
    "MemoryIngestionResult",
    "decide_memory",
    "is_todo_eligible",
]
