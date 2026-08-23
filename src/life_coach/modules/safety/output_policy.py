"""Pre-generation boundaries and post-generation language checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar


class OutputViolationKind(StrEnum):
    """Zero-tolerance categories for consumer-facing output."""

    CLINICAL_DIAGNOSIS = "clinical_diagnosis"
    PERSONALITY_DISORDER_JUDGMENT = "personality_disorder_judgment"
    SUICIDE_RISK_SCORE_OR_TIER = "suicide_risk_score_or_tier"
    AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM = "automatic_third_party_contact_claim"
    DETERMINISTIC_SAFETY_ASSURANCE = "deterministic_safety_assurance"
    MEDICATION_DOSAGE_PRESCRIPTION = "medication_dosage_prescription"


@dataclass(frozen=True, slots=True)
class OutputPolicyViolation:
    """A localized policy match produced after generation."""

    kind: OutputViolationKind
    matched_text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class OutputSafetyAssessment:
    """Post-generation policy result."""

    violations: tuple[OutputPolicyViolation, ...]

    @property
    def allowed(self) -> bool:
        """Only outputs with no findings may be shown."""

        return not self.violations


class UnsafeGeneratedOutputError(ValueError):
    """Raised when generated text violates the consumer safety contract."""

    def __init__(self, violations: tuple[OutputPolicyViolation, ...]) -> None:
        super().__init__("generated output violates the non-diagnostic policy")
        self.violations = violations


_PatternRule = tuple[OutputViolationKind, re.Pattern[str]]


class DiagnosticLanguageDetector:
    """Conservative syntax detector for prohibited diagnostic-style claims.

    This is a post-generation release gate, not a classifier of the user.  It
    inspects assistant output only and emits no clinical inference.
    """

    _RULES: ClassVar[tuple[_PatternRule, ...]] = (
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:你|该用户|用户|他|她)\s*(?:很|极)?(?:可能|大概|显然|明确|确实|已经)?"
                r"\s*(?:患有|得了|符合.{0,8}诊断)\s*[^\u3002\uFF01\uFF1F\n]{0,30}"
                r"(?:障碍|抑郁症|强迫症|精神病|双相|创伤后应激|PTSD)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:你|该用户|用户|他|她)\s*(?:很|极)?"
                r"(?:可能|大概|显然|明确|确实|已经)?\s*(?:有|患)\s*(?:"
                r"抑郁症(?!状|的症状)|强迫症(?!状|的症状)|精神分裂症(?!状|的症状)|"
                r"[^\s\u3002\uFF01\uFF1F\uFF0C,]{1,16}障碍(?!状|的症状))",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:你|该用户|用户|他|她)\s*(?:并不|不|并没有|没有)\s*"
                r"(?:患有|患|有|是|属于)?\s*(?:"
                r"抑郁症(?!状|的症状)|强迫症(?!状|的症状)|精神分裂症(?!状|的症状)|"
                r"[^\s\u3002\uFF01\uFF1F\uFF0C,]{1,16}障碍(?!状|的症状))",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:你|该用户|用户|他|她)\s*(?:很|极)?"
                r"(?:可能|大概|显然|明确|确实|就是)?\s*(?:是|属于)\s*"
                r"(?:一名|一个|典型的)?\s*"
                r"(?:抑郁症|双相情感障碍|精神病|创伤后应激障碍|PTSD)(?:患者)?",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\b(?:you|the user|they|he|she)\s+"
                r"(?:(?:probably|likely|clearly|definitely)\s+)?"
                r"(?:(?:may|might|could)\s+|seem(?:s)?\s+to\s+)?"
                r"(?:have|has|suffer(?:s)? from|meet(?:s)? (?:the )?criteria for)\s+"
                r"(?!symptoms?\b|signs?\b)"
                r"(?:an?\s+)?(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd|adhd|ocd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\b(?:you|the user|they|he|she)\s+"
                r"(?:do(?:es)?\s+not|don't|doesn't)\s+"
                r"(?:have|suffer from|meet (?:the )?criteria for)\s+"
                r"(?!symptoms?\b|signs?\b)"
                r"(?:an?\s+)?(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\bI\s+diagnose\s+(?:you|the user)\s+with\s+"
                r"(?:an?\s+)?(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd|adhd|ocd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\bthe\s+diagnosis\s+is\s+(?:an?\s+)?"
                r"(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd|adhd|ocd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\byour symptoms\s+(?:clearly\s+|strongly\s+)?"
                r"(?:indicate|confirm|demonstrate|prove|mean)\s+"
                r"(?:that you have\s+)?(?:an?\s+)?"
                r"(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\b(?:you|the user|they|he|she)\s+"
                r"(?:(?:(?:probably|likely|clearly|definitely)\s+)?(?:are|is)|"
                r"(?:may|might|could)\s+be)\s+"
                r"(?:bipolar|psychotic|schizophrenic)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT,
            re.compile(
                r"(?:你|该用户|用户|他|她)\s*"
                r"(?:(?:很|极)?(?:可能|大概|显然|明确|确实)?\s*)?"
                r"(?:(?:并不|不)?(?:是|属于)|就是)\s*"
                r"(?:典型的)?[^\u3002\uFF01\uFF1F\n]{0,20}"
                r"(?:人格障碍|自恋型人格|边缘型人格|反社会人格)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT,
            re.compile(
                r"\b(?:you|the user|they|he|she)\s+(?:"
                r"(?:(?:clearly|probably|likely|definitely)\s+)?(?:are|is)\s+(?:not\s+)?|"
                r"(?:may|might|could)\s+be\s+)"
                r"(?:an?\s+)?(?:narcissist|psychopath|sociopath)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER,
            re.compile(
                r"(?:自杀|自伤)风险\s*(?:为|是|:|\uFF1A)?\s*"
                r"(?:\d+(?:\.\d+)?\s*%|零|无|没有|"
                r"低(?:风险)?|中(?:风险)?|高(?:风险)?)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER,
            re.compile(
                r"\b(?:(?:your|the user's)\s+)?(?:suicide|self-harm) risk\s*"
                r"(?:is|=|:)?\s*(?:\d+(?:\.\d+)?\s*%(?!\w)|"
                r"(?:zero|none|low|medium|moderate|high)\b)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\bdiagnosis[\"']?\s*[:=]",
                re.I,
            ),
        ),
        (
            OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT,
            re.compile(r"\bpersonality_disorder[\"']?\s*[:=]", re.I),
        ),
        (
            OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER,
            re.compile(r"\bsuicide_risk_score[\"']?\s*[:=]", re.I),
        ),
        (
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
            re.compile(
                r"(?:我|我们|系统)\s*(?:已经|已)\s*(?:替你|为你)?\s*"
                r"(?:联系|通知|呼叫)(?:了)?\s*"
                r"(?:警方|警察|急救|救援|你的家人|你的朋友|你的联系人)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
            re.compile(
                r"\b(?:I(?:\s+(?:have\s+)?|['\u2019]ve\s+)|"
                r"we(?:\s+(?:have\s+)?|['\u2019]ve\s+)|"
                r"the system\s+(?:has\s+)?)"
                r"(?:already\s+)?(?:contacted|called|notified)\s+"
                r"(?:emergency services|the police|an ambulance|your family|"
                r"your friend|your contact)\b(?:\s+for you)?",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE,
            re.compile(
                r"\b(?:you|the user)\s+(?:are|will be)\s+"
                r"(?:(?:definitely|certainly|absolutely|completely)\s+safe|safe\s+now)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE,
            re.compile(
                r"(?:你|该用户|用户)\s*(?:现在)?\s*(?:肯定|绝对|一定|完全)\s*安全",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.MEDICATION_DOSAGE_PRESCRIPTION,
            re.compile(
                r"\b(?:(?:you\s+(?:should|must|need to)\s+)|"
                r"(?:I recommend(?: that)? you\s+))?"
                r"(?:take|start taking|increase(?: your dose)? to|decrease(?: your dose)? to)\s+"
                r"(?:[a-z][a-z-]*\s+){0,5}\d+(?:\.\d+)?\s*"
                r"(?:mg|mcg|g|ml|milligrams?|micrograms?|grams?|milliliters?)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.MEDICATION_DOSAGE_PRESCRIPTION,
            re.compile(
                r"(?:你\s*(?:应该|必须|需要)?\s*|建议你\s*)?"
                r"(?:服用|吃|开始服用|加量到|减量到)\s*"
                r"[^\s\d\u3002\uFF01\uFF1F\uFF0C,]{0,16}\s*"
                r"\d+(?:\.\d+)?\s*(?:毫克|微克|克|毫升|mg|mcg|ml)\b",
                re.IGNORECASE,
            ),
        ),
    )

    def inspect(self, text: str) -> OutputSafetyAssessment:
        """Find all prohibited claims in assistant-generated text."""

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        findings = [
            OutputPolicyViolation(kind, match.group(0), match.start(), match.end())
            for kind, pattern in self._RULES
            for match in pattern.finditer(text)
        ]
        findings.sort(key=lambda finding: (finding.start, finding.end, finding.kind))
        return OutputSafetyAssessment(tuple(findings))


@dataclass(frozen=True, slots=True)
class NonDiagnosticOutputPolicy:
    """Consumer generation contract plus its mandatory release check."""

    detector: DiagnosticLanguageDetector = field(
        default_factory=DiagnosticLanguageDetector,
        repr=False,
        compare=False,
    )

    @property
    def generation_constraints(self) -> tuple[str, ...]:
        """Stable pre-generation constraints for any model adapter."""

        return (
            "Use tentative observations and preserve the user's right to disagree.",
            "Do not provide a clinical diagnosis or personality-disorder judgment.",
            "Do not calculate or display a suicide/self-harm risk score or tier.",
            "Do not claim that a third party or emergency service was contacted.",
            "Do not provide medication prescriptions or promise that the user is safe.",
        )

    def inspect(self, text: str) -> OutputSafetyAssessment:
        """Run the required post-generation release check."""

        return self.detector.inspect(text)

    def enforce(self, text: str) -> None:
        """Raise instead of releasing text that violates the contract."""

        assessment = self.inspect(text)
        if not assessment.allowed:
            raise UnsafeGeneratedOutputError(assessment.violations)
