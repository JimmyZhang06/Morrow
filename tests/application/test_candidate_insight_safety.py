from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from life_coach.application.candidate_insight_safety import (
    CandidateInsightMemorySafetyClassifier,
)
from life_coach.modules.knowledge.enums import DataClass, SafetyDecision

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
KEY = b"candidate-safety-test-key-material-000000000000"


def _classify(
    classifier: CandidateInsightMemorySafetyClassifier,
    text: str,
    *,
    vault_id: uuid.UUID | None = None,
):
    return classifier.classify(
        session=Mock(),
        vault_id=vault_id or uuid.UUID("00000000-0000-0000-0000-000000000201"),
        texts=(text,),
        at=NOW,
    )


def test_clean_candidate_is_sensitive_non_proactive_and_stable() -> None:
    classifier = CandidateInsightMemorySafetyClassifier(KEY, key_id="test-v1")
    text = "你可能很重视拥有安静的创作时间。"

    first = _classify(classifier, text)
    replay = _classify(classifier, text)

    assert first == replay
    assert first.decision is SafetyDecision.ALLOW
    assert first.data_class is DataClass.SENSITIVE
    assert first.allows_proactive is False
    assert text not in repr(first)
    assert text not in repr(classifier)


@pytest.mark.parametrize(
    "text",
    [
        "你可能患有抑郁症。",
        "从这些记录看，你符合人格障碍的诊断。",  # noqa: RUF001
        "我觉得自己可能不想活了。",
        "You may be suicidal and want to end your life.",
        "你应该服用舍曲林 50mg。",
    ],
)
def test_clinical_crisis_and_output_policy_language_is_rejected(text: str) -> None:
    assessment = _classify(
        CandidateInsightMemorySafetyClassifier(KEY, key_id="test-v1"),
        text,
    )

    assert assessment.decision is SafetyDecision.REJECT
    assert assessment.data_class is DataClass.HIGHLY_SENSITIVE
    assert assessment.allows_proactive is False
    assert text not in repr(assessment)


def test_assessment_identity_is_vault_text_policy_and_key_bound() -> None:
    text = "你可能更偏好先独立思考，再参与讨论。"  # noqa: RUF001
    vault_a = uuid.UUID("00000000-0000-0000-0000-000000000211")
    vault_b = uuid.UUID("00000000-0000-0000-0000-000000000212")

    base = _classify(
        CandidateInsightMemorySafetyClassifier(KEY, key_id="test-v1"),
        text,
        vault_id=vault_a,
    )
    changed_text = _classify(
        CandidateInsightMemorySafetyClassifier(KEY, key_id="test-v1"),
        text + "也许。",
        vault_id=vault_a,
    )
    changed_vault = _classify(
        CandidateInsightMemorySafetyClassifier(KEY, key_id="test-v1"),
        text,
        vault_id=vault_b,
    )
    changed_key_id = _classify(
        CandidateInsightMemorySafetyClassifier(KEY, key_id="test-v2"),
        text,
        vault_id=vault_a,
    )

    assert (
        len(
            {
                base.assessment_id,
                changed_text.assessment_id,
                changed_vault.assessment_id,
                changed_key_id.assessment_id,
            }
        )
        == 4
    )


def test_malformed_or_empty_input_fails_closed_without_text_receipt() -> None:
    classifier = CandidateInsightMemorySafetyClassifier(KEY)
    empty = classifier.classify(
        session=Mock(),
        vault_id=uuid.uuid4(),
        texts=(),
        at=NOW,
    )
    blank = _classify(classifier, "   ")

    assert empty.decision is SafetyDecision.REJECT
    assert blank.decision is SafetyDecision.REJECT
    assert empty.allows_proactive is False
    assert blank.allows_proactive is False


def test_classifier_rejects_weak_key_and_naive_time() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        CandidateInsightMemorySafetyClassifier(b"short")

    classifier = CandidateInsightMemorySafetyClassifier(KEY)
    with pytest.raises(ValueError, match="timezone-aware"):
        classifier.classify(
            session=Mock(),
            vault_id=uuid.uuid4(),
            texts=("ordinary candidate",),
            at=datetime(2026, 8, 24, 12),
        )
