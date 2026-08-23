from __future__ import annotations

import pytest

from life_coach.modules.safety import (
    DiagnosticLanguageDetector,
    NonDiagnosticOutputPolicy,
    OutputViolationKind,
    UnsafeGeneratedOutputError,
)


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
        ("The diagnosis is depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You seem to have depression.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You have ADHD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
        ("You have OCD.", OutputViolationKind.CLINICAL_DIAGNOSIS),
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
            "I already called the police for you.",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        (
            "I've contacted emergency services.",
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
        ),
        ("You are definitely safe.", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
        ("You are safe now.", OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE),
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
    ],
)
def test_non_diagnostic_supportive_language_is_allowed(text: str) -> None:
    assert DiagnosticLanguageDetector().inspect(text).allowed


def test_policy_exposes_pre_generation_constraints_and_enforces_post_check() -> None:
    policy = NonDiagnosticOutputPolicy()

    assert any("clinical diagnosis" in constraint for constraint in policy.generation_constraints)
    with pytest.raises(UnsafeGeneratedOutputError):
        policy.enforce("You have a personality disorder.")


def test_policy_allows_tentative_non_pathologizing_reflection() -> None:
    NonDiagnosticOutputPolicy().enforce(
        "One possibility is that this situation felt isolating; you may see it differently."
    )


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
