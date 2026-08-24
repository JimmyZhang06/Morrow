"""Deterministic production safety classifier for candidate-insight memory."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from life_coach.modules.knowledge.contracts import SafetyAssessment
from life_coach.modules.knowledge.enums import DataClass, SafetyDecision
from life_coach.modules.knowledge.policy import contains_clinical_language
from life_coach.modules.safety.output_policy import DiagnosticLanguageDetector

_ASSESSMENT_NAMESPACE = uuid.UUID("8dddc55b-4cb4-51d1-9ee1-9f33832821fc")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CRISIS_LANGUAGE = re.compile(
    r"自杀|自傷|自伤|轻生|傷害自己|伤害自己|不想活|不想再活|活不下去|结束生命|結束生命|"
    r"\bsuicid(?:e|al)\b|\bself[- ]?harm\b|\bkill\s+myself\b|"
    r"\bend\s+my\s+life\b|\b(?:do\s+not|don['\u2019]?t)\s+want\s+to\s+live\b",
    re.IGNORECASE,
)


class CandidateInsightMemorySafetyClassifier:
    """Fail closed on risky model-authored memory without invoking another model.

    This classifier is deliberately scoped to ``candidate_insight``.  It does
    not diagnose, estimate risk, or authorize proactive use.  Its stable UUID
    binds the exact text tuple, Vault, policy version, key id, and decision via
    a secret HMAC; neither the UUID nor the returned assessment contains text.

    The assessment is not a standalone authority receipt.  For an accepted
    claim, Knowledge persists only ``assessment_id`` and ``allows_proactive``.
    Rejected assessments have no durable receipt until a dedicated safety audit
    store is introduced.
    """

    __slots__ = ("_detector", "_hmac_key", "_key_id")

    supported_task_type = "candidate_insight"
    policy_version = "candidate-memory-safety-v1"

    def __init__(
        self,
        assessment_hmac_key: bytes,
        *,
        key_id: str = "v1",
        detector: DiagnosticLanguageDetector | None = None,
    ) -> None:
        if not isinstance(assessment_hmac_key, bytes) or len(assessment_hmac_key) < 32:
            raise ValueError("candidate safety HMAC key must contain at least 32 bytes")
        if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
            raise ValueError("candidate safety key_id must be a technical identifier")
        if detector is not None and not isinstance(detector, DiagnosticLanguageDetector):
            raise TypeError("candidate safety detector must be DiagnosticLanguageDetector")
        self._hmac_key = bytes(assessment_hmac_key)
        self._key_id = key_id
        self._detector = detector or DiagnosticLanguageDetector()

    def classify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        texts: tuple[str, ...],
        at: datetime,
    ) -> SafetyAssessment:
        """Classify exact generated fields; DB state and Source text are out of scope."""

        del session
        if not isinstance(vault_id, uuid.UUID):
            raise TypeError("candidate safety vault_id must be a UUID")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("candidate safety assessment time must be timezone-aware")

        checked_texts = tuple(value for value in texts if isinstance(value, str))
        malformed = len(checked_texts) != len(texts) or not checked_texts
        rejected = malformed or any(self._rejects(value) for value in checked_texts)
        decision = SafetyDecision.REJECT if rejected else SafetyDecision.ALLOW
        data_class = DataClass.HIGHLY_SENSITIVE if rejected else DataClass.SENSITIVE
        assessment_id = self._assessment_id(
            vault_id=vault_id,
            texts=checked_texts,
            malformed=malformed,
            decision=decision,
            data_class=data_class,
        )
        return SafetyAssessment(
            assessment_id=assessment_id,
            vault_id=vault_id,
            decision=decision,
            data_class=data_class,
            allows_proactive=False,
        )

    def _rejects(self, value: str) -> bool:
        if not value.strip():
            return True
        normalized = "".join(
            character
            for character in unicodedata.normalize("NFKC", value).casefold()
            if unicodedata.category(character) != "Cf"
        )
        try:
            return (
                contains_clinical_language(normalized)
                or _CRISIS_LANGUAGE.search(normalized) is not None
                or not self._detector.inspect(value).lexical_layer_allowed
            )
        except Exception:
            return True

    def _assessment_id(
        self,
        *,
        vault_id: uuid.UUID,
        texts: tuple[str, ...],
        malformed: bool,
        decision: SafetyDecision,
        data_class: DataClass,
    ) -> uuid.UUID:
        digest = hmac.new(self._hmac_key, digestmod=hashlib.sha256)
        for field in (
            self.policy_version,
            self._key_id,
            str(vault_id),
            decision.value,
            data_class.value,
            "malformed" if malformed else "well_formed",
        ):
            _update_length_prefixed(digest, field.encode("utf-8"))
        for value in texts:
            _update_length_prefixed(digest, value.encode("utf-8"))
        return uuid.uuid5(_ASSESSMENT_NAMESPACE, digest.hexdigest())


def _update_length_prefixed(digest: hmac.HMAC, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


__all__ = ["CandidateInsightMemorySafetyClassifier"]
