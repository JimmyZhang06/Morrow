from __future__ import annotations

from typing import cast

import pytest

from life_coach.modules.safety import (
    DiagnosticLanguageDetector,
    NonDiagnosticOutputPolicy,
    OutputViolationKind,
    UnsafeGeneratedOutputError,
)
from life_coach.modules.safety.output_policy import (
    OutputVerificationStatus,
    SemanticOutputVerifier,
)


class RecordingSemanticVerifier:
    def __init__(self, status: object) -> None:
        self.status = status
        self.seen_texts: list[str] = []

    def verify(self, text: str) -> OutputVerificationStatus:
        self.seen_texts.append(text)
        return cast(OutputVerificationStatus, self.status)


class FailingSemanticVerifier:
    def __init__(self) -> None:
        self.seen_texts: list[str] = []

    def verify(self, text: str) -> OutputVerificationStatus:
        self.seen_texts.append(text)
        raise RuntimeError("semantic verifier unavailable")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("你可能患有抑郁症。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("你有抑郁症。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("你患焦虑障碍。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("你患有强迫症。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("你没有抑郁症。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You likely have depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You may have depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You might have PTSD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You could have PTSD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You are bipolar.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You might be bipolar.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You do not have depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        (
            "Your symptoms indicate major depressive disorder.",
            OutputViolationKind.CLINICAL_DIAGNOSIS,
        ),
        ("I diagnose you with depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("I can diagnose you with depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("I can diagnose you with depression!", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("The diagnosis is depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You seem to have depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You have ADHD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You have OCD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("根据你的描述\uff0c可以确诊为抑郁症。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("这说明你患有强迫症。", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("I can confirm you have depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("This means you have OCD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("你就是边缘型人格。", OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT),
        ("你可能是边缘型人格。", OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT),
        ("You may be a narcissist.", OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT),
        ("你的自杀风险是高风险。", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        ("suicide risk: 23%", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        ("Your suicide risk is zero.", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        (
            "我已经为你联系了警方。",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "我们已通知了你的朋友。",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "我已经帮你叫了救护车。",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "我已经帮你叫了救护车\uff01",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "I already called the police for you.",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "I've contacted emergency services.",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        ("救援人员正在路上。", OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM),
        ("警方很快会到。", OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM),
        (
            "Emergency services are on the way.",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "Help has been dispatched.",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        ("You are definitely safe.", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("You are safe now.", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("你现在没有危险了。", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("你现在安全了。", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("你现在安全了\uff01", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("你现在安全了", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("我可以保证你一定安全。", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("You are no longer in danger.", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("I can guarantee you are safe.", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        (
            "You should take sertraline 25 mg daily.",
            OutputViolationKind.MEDICATION_DOSAGE_PRESCRIPTION,
        ),
        (
            "I recommend you take sertraline 25 mg daily.",
            OutputViolationKind.MEDICATION_DOSAGE_PRESCRIPTION,
        ),
        ("每天服用舍曲林25毫克。", OutputViolationKind.MEDICATION_DOSAGE_PRESCRIPTION),
    ],
)
def test_diagnostic_style_output_is_blocked(text: str, kind: OutputViolationKind) -> None:
    assessment = DiagnosticLanguageDetector().inspect(text)

    assert not assessment.lexical_layer_allowed
    assert not assessment.allowed
    assert kind in {violation.kind for violation in assessment.violations}


@pytest.mark.parametrize(
    "text",
    [
        "听起来你今天很难过; 如果你愿意, 我们可以先停在这里。",
        "Feeling sad after a loss can be painful, and it does not by itself define you.",
        "I cannot diagnose you. One possibility is worth discussing with a qualified clinician.",
        "You mentioned symptoms of depression, but that is not a diagnosis.",
        "你有抑郁症状的描述, 但这不是诊断。",
        "I cannot diagnose or rule out depression from this conversation.",
        "Your symptoms may be consistent with depression, but this is not a diagnosis.",
        "Take medication only as prescribed by a qualified clinician.",
        "I cannot confirm a diagnosis from this conversation.",
        "If you may be in immediate danger, contact local emergency services yourself.",
        "I cannot contact or dispatch emergency services for you.",
        "I cannot determine from here whether you are currently safe.",
    ],
)
def test_non_diagnostic_supportive_language_passes_only_the_lexical_layer(text: str) -> None:
    assessment = DiagnosticLanguageDetector().inspect(text)

    assert assessment.lexical_layer_allowed
    assert not assessment.allowed
    assert assessment.semantic_verification is OutputVerificationStatus.NOT_PERFORMED


def test_policy_exposes_pre_generation_constraints_and_enforces_post_check() -> None:
    verifier = RecordingSemanticVerifier(OutputVerificationStatus.VERIFIED)
    policy = NonDiagnosticOutputPolicy(semantic_verifier=verifier)

    assert any("clinical diagnosis" in constraint for constraint in policy.generation_constraints)
    with pytest.raises(UnsafeGeneratedOutputError):
        policy.enforce("You have a personality disorder.")
    assert verifier.seen_texts == []


def test_policy_allows_tentative_non_pathologizing_reflection() -> None:
    text = "One possibility is that this situation felt isolating; you may see it differently."
    verifier = RecordingSemanticVerifier(OutputVerificationStatus.VERIFIED)

    NonDiagnosticOutputPolicy(semantic_verifier=verifier).enforce(text)

    assert verifier.seen_texts == [text]


@pytest.mark.parametrize(
    "status",
    [
        OutputVerificationStatus.REJECTED,
        OutputVerificationStatus.INDETERMINATE,
        OutputVerificationStatus.UNAVAILABLE,
    ],
)
def test_policy_fails_closed_when_semantic_verification_does_not_clear_release(
    status: OutputVerificationStatus,
) -> None:
    verifier = RecordingSemanticVerifier(status)
    policy = NonDiagnosticOutputPolicy(semantic_verifier=verifier)
    text = "A tentative, non-pathologizing reflection."

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release(text)

    assert exc_info.value.violations == ()
    assert exc_info.value.semantic_verification is status
    assert verifier.seen_texts == [text]


def test_policy_fails_closed_when_semantic_verification_is_missing() -> None:
    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        NonDiagnosticOutputPolicy().release("A tentative, non-pathologizing reflection.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.NOT_PERFORMED


def test_policy_fails_closed_when_verifier_returns_non_enum_value() -> None:
    verifier = RecordingSemanticVerifier(True)
    policy = NonDiagnosticOutputPolicy(semantic_verifier=verifier)

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("A tentative, non-pathologizing reflection.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.INDETERMINATE


def test_policy_fails_closed_when_verifier_raises() -> None:
    verifier = FailingSemanticVerifier()
    policy = NonDiagnosticOutputPolicy(semantic_verifier=verifier)
    text = "A tentative, non-pathologizing reflection."

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release(text)

    assert exc_info.value.semantic_verification is OutputVerificationStatus.UNAVAILABLE
    assert verifier.seen_texts == [text]


def test_release_returns_verified_text() -> None:
    text = "One possibility is that this felt lonely; you may see it differently."
    verifier = RecordingSemanticVerifier(OutputVerificationStatus.VERIFIED)

    released = NonDiagnosticOutputPolicy(semantic_verifier=verifier).release(text)

    assert released == text
    assert verifier.seen_texts == [text]


def test_verification_is_recomputed_for_each_exact_text() -> None:
    first = "The first tentative reflection."
    second = "The second tentative reflection."
    verifier = RecordingSemanticVerifier(OutputVerificationStatus.VERIFIED)
    policy = NonDiagnosticOutputPolicy(semantic_verifier=verifier)

    assert policy.release(first) == first
    assert policy.release(second) == second

    assert verifier.seen_texts == [first, second]


def test_semantic_verifier_protocol_is_runtime_checkable() -> None:
    verifier = RecordingSemanticVerifier(OutputVerificationStatus.VERIFIED)

    assert isinstance(verifier, SemanticOutputVerifier)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ('{"diagnosis": "depression"}', OutputViolationKind.CLINICAL_DIAGNOSIS),
        (
            '{"personality_disorder": "borderline"}',
            OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT,
        ),
        (
            '{"suicide_risk_score": "high"}',
            OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER,
        ),
    ],
)
def test_structured_fields_are_classified_by_their_own_violation_kind(
    text: str,
    kind: OutputViolationKind,
) -> None:
    assessment = DiagnosticLanguageDetector().inspect(text)

    assert {violation.kind for violation in assessment.violations} == {kind}
