"""Pre-generation boundaries and post-generation language checks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from ._time import require_aware
from .authority import TrustedClock, read_trusted_time


class OutputViolationKind(StrEnum):
    """Zero-tolerance categories for consumer-facing output."""

    CLINICAL_DIAGNOSIS = "clinical_diagnosis"
    PERSONALITY_DISORDER_JUDGMENT = "personality_disorder_judgment"
    SUICIDE_RISK_SCORE_OR_TIER = "suicide_risk_score_or_tier"
    AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM = "automatic_third_party_contact_claim"
    DETERMINISTIC_SAFETY_ASSURANCE = "deterministic_safety_assurance"
    MEDICATION_DOSAGE_PRESCRIPTION = "medication_dosage_prescription"


class OutputVerificationStatus(StrEnum):
    """Independent semantic-verification state for a release decision."""

    NOT_PERFORMED = "not_performed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"
    UNAVAILABLE = "unavailable"


@runtime_checkable
class SemanticOutputVerifier(Protocol):
    """Independent semantic checker that can request an authority receipt."""

    def verify(
        self,
        claims: SemanticOutputClaims,
        *,
        at: datetime,
    ) -> SemanticOutputVerificationReceipt:
        """Return a receipt for the exact claims evaluated at trusted time."""


@runtime_checkable
class SemanticOutputAuthorityPort(Protocol):
    """Validate an opaque receipt against the current exact output claims."""

    def verify_receipt(
        self,
        receipt: SemanticOutputVerificationReceipt,
        *,
        claims: SemanticOutputClaims,
        at: datetime,
    ) -> bool:
        """Return true only for an authentic, current, exactly bound receipt."""


def _require_nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _require_policy_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("policy_generation must be an integer")
    if value < 1:
        raise ValueError("policy_generation must be at least 1")
    return value


def _exact_text_hash(text: str) -> str:
    """Hash the exact UTF-8 output; no trimming or Unicode rewriting occurs."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticOutputClaims:
    """Exact text and tenant/session/policy scope presented for verification."""

    text: str = field(repr=False)
    vault_id: str
    session_id: str
    policy_generation: int
    text_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        _require_nonempty_string(self.vault_id, field_name="vault_id")
        _require_nonempty_string(self.session_id, field_name="session_id")
        _require_policy_generation(self.policy_generation)
        object.__setattr__(self, "text_hash", _exact_text_hash(self.text))


@dataclass(frozen=True, slots=True)
class SemanticOutputVerificationReceipt:
    """Opaque authority evidence bound to one exact output and validity window."""

    opaque_receipt: str = field(repr=False)
    issuer: str
    text_hash: str
    vault_id: str
    session_id: str
    policy_generation: int
    issued_at: datetime
    expires_at: datetime
    status: OutputVerificationStatus

    def __post_init__(self) -> None:
        for field_name, value in (
            ("opaque_receipt", self.opaque_receipt),
            ("issuer", self.issuer),
            ("text_hash", self.text_hash),
            ("vault_id", self.vault_id),
            ("session_id", self.session_id),
        ):
            _require_nonempty_string(value, field_name=field_name)
        _require_policy_generation(self.policy_generation)
        require_aware(self.issued_at, field_name="issued_at")
        require_aware(self.expires_at, field_name="expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("receipt expires_at must be after issued_at")
        if not isinstance(self.status, OutputVerificationStatus):
            raise TypeError("status must be an OutputVerificationStatus")


@dataclass(frozen=True, slots=True)
class OutputPolicyViolation:
    """A localized policy match produced after generation."""

    kind: OutputViolationKind
    matched_text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class OutputSafetyAssessment:
    """Post-generation policy result across lexical and semantic layers."""

    violations: tuple[OutputPolicyViolation, ...]
    semantic_verification: OutputVerificationStatus = OutputVerificationStatus.NOT_PERFORMED

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_verification, OutputVerificationStatus):
            raise TypeError("semantic_verification must be an OutputVerificationStatus")

    @property
    def lexical_layer_allowed(self) -> bool:
        """Whether the regex layer found no prohibited syntax."""

        return not self.violations

    @property
    def allowed(self) -> bool:
        """Only outputs cleared by both layers may be shown."""

        return (
            self.lexical_layer_allowed
            and self.semantic_verification is OutputVerificationStatus.VERIFIED
        )


class UnsafeGeneratedOutputError(ValueError):
    """Raised when generated text violates the consumer safety contract."""

    def __init__(
        self,
        violations: tuple[OutputPolicyViolation, ...],
        *,
        semantic_verification: OutputVerificationStatus = (OutputVerificationStatus.NOT_PERFORMED),
    ) -> None:
        message = (
            "generated output violates the non-diagnostic policy"
            if violations
            else "generated output was not semantically verified for release"
        )
        super().__init__(message)
        self.violations = violations
        self.semantic_verification = semantic_verification


_PatternRule = tuple[OutputViolationKind, re.Pattern[str]]


class DiagnosticLanguageDetector:
    """Conservative syntax detector for prohibited diagnostic-style claims.

    This regex detector is only the first, lexical post-generation layer; it
    does not claim full semantic coverage and cannot authorize release by
    itself.  It inspects assistant output only and emits no clinical inference.
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
                r"\bI\s+(?:can\s+)?diagnose\s+(?:you|the user)\s+with\s+"
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
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:根据|基于)(?:你|您)的(?:描述|情况|表现|症状)\s*[\uFF0C,]?\s*"
                r"(?:可以|能够|足以|已经)?\s*(?:确诊|确认诊断|诊断)\s*"
                r"(?:你|您)?\s*(?:为|是|患有)?\s*(?:"
                r"抑郁症|强迫症|精神分裂症|双相(?:情感)?障碍|"
                r"创伤后应激障碍|PTSD|ADHD|OCD|"
                r"[^\s\u3002\uFF01\uFF1F\uFF0C,]{1,16}障碍)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?<!不)(?<!未)(?<!无法)(?:可以|能够|足以|现可|已经)\s*"
                r"(?:明确)?\s*(?:确诊|确认诊断|诊断)\s*(?:你|您)?\s*"
                r"(?:为|是|患有)?\s*(?:抑郁症|强迫症|精神分裂症|"
                r"双相(?:情感)?障碍|创伤后应激障碍|PTSD|ADHD|OCD|"
                r"[^\s\u3002\uFF01\uFF1F\uFF0C,]{1,16}障碍)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:这|这种情况|这些(?:表现|症状)|你的(?:描述|情况))\s*"
                r"(?:清楚地|明确地)?\s*(?:说明|表明|意味着|证明|就是)\s*"
                r"(?:你|您)?\s*(?:患有|有|是)?\s*(?:抑郁症|强迫症|"
                r"精神分裂症|双相(?:情感)?障碍|创伤后应激障碍|"
                r"PTSD|ADHD|OCD)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\bI\s+(?:can\s+|do\s+)?(?:confirm|determine|conclude)\s+"
                r"(?:that\s+)?you\s+(?:have|meet (?:the )?criteria for)\s+"
                r"(?:an?\s+)?(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|"
                r"depression|schizophrenia|psychosis|bipolar disorder|"
                r"ptsd|adhd|ocd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\b(?:this|that|what you describe)\s+"
                r"(?:clearly\s+|definitely\s+)?(?:means|shows|confirms|proves)\s+"
                r"(?:that\s+)?you\s+have\s+(?:an?\s+)?"
                r"(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd|adhd|ocd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\bbased on (?:your|what you(?:'ve| have))\s+"
                r"(?:description|described|symptoms),?\s+"
                r"(?:this is|the diagnosis is)\s+(?:an?\s+)?"
                r"(?:[a-z-]+(?:\s+[a-z-]+){0,4}\s+disorder|depression|"
                r"schizophrenia|psychosis|bipolar disorder|ptsd|adhd|ocd)\b",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"(?:根据|基于)\s*(?:你|您)(?:的)?\s*"
                r"(?:description|symptoms?|描述|症状)\s*[,\uFF0C]?\s*"
                r"(?:可以|能够|足以|I\s+can|we\s+can)?\s*"
                r"(?:确诊|诊断|diagnose|confirm)\s*(?:你|您|you)?\s*"
                r"(?:为|是|患有|with|as)?\s*(?:depression|ptsd|adhd|ocd|"
                r"抑郁症|强迫症|精神分裂症|双相(?:情感)?障碍|创伤后应激障碍)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.CLINICAL_DIAGNOSIS,
            re.compile(
                r"\bbased\s+on\s+(?:你的|您的|your)\s*"
                r"(?:描述|症状|description|symptoms?)\s*[,\uFF0C]?\s*"
                r"(?:this\s+(?:is|confirms)|(?:I|we)\s+can\s+(?:diagnose|confirm)|"
                r"(?:可以|能够)?\s*(?:确诊|诊断))\s*(?:你|您|you)?\s*"
                r"(?:为|是|患有|have|with)?\s*(?:depression|ptsd|adhd|ocd|"
                r"抑郁症|强迫症|精神分裂症|双相(?:情感)?障碍|创伤后应激障碍)",
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
            OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER,
            re.compile(
                r"(?:(?:你的|您的|your|the user's)\s*)?"
                r"(?:自杀|自伤|suicide|self-harm)\s*(?:风险|risk)\s*"
                r"(?:为|是|is|=|:|\uFF1A)?\s*"
                r"(?:\d+(?:\.\d+)?\s*%|低|中|高|low|medium|moderate|high)"
                r"(?:风险|risk)?\b",
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
                r"(?:我|我们|系统)\s*(?:已经|已)\s*"
                r"(?:替你|为你|帮你|给你)?\s*(?:叫|呼叫|联系|通知|安排)(?:了)?\s*"
                r"(?:救护车|急救人员|医护人员|救援人员|警方|警察)",
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
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
            re.compile(
                r"(?:救援人员|急救人员|医护人员|救护车|警方|警察)\s*"
                r"(?:已经|已|正|正在|很快|马上|即将)?\s*"
                r"(?:在路上|赶来|赶到|会到|将到|到达)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
            re.compile(
                r"(?:救援|帮助|救护车|急救人员)\s*(?:已经|已)?\s*"
                r"(?:派出|出发|调度|安排)(?:了)?",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.AUTOMATIC_THIRD_PARTY_CONTACT_CLAIM,
            re.compile(
                r"\b(?:emergency services|help|the police|police|an ambulance|"
                r"responders?|paramedics?)\s+(?:have\s+|has\s+)?"
                r"(?:been\s+)?(?:dispatched|sent)|"
                r"\b(?:emergency services|help|the police|police|an ambulance|"
                r"responders?|paramedics?)\s+(?:are|is)\s+(?:already\s+)?"
                r"(?:on the way|coming)|"
                r"\b(?:the police|police|an ambulance|responders?|paramedics?)\s+"
                r"(?:will|should)\s+(?:arrive|be there)\s+(?:very\s+)?soon\b",
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
                r"(?:你|您)\s*(?:现在)?\s*(?:已经)?\s*"
                r"(?:没有|不存在)\s*(?:任何)?\s*(?:立即|即时|当前)?\s*危险(?:了)?",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE,
            re.compile(
                r"(?:我|我们|系统)\s*(?:可以|能|能够)?\s*"
                r"(?:保证|确保)\s*(?:你|您)\s*(?:肯定|绝对|一定|会)?\s*"
                r"(?:安全|不会有事|不会受伤|不会伤害自己)",
                re.IGNORECASE,
            ),
        ),
        (
            OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE,
            re.compile(
                r"\b(?:you|the user)\s+(?:are|is)\s+no longer\s+"
                r"(?:in danger|at risk)\b|"
                r"\bI\s+(?:can\s+)?(?:guarantee|assure you)\s+(?:that\s+)?"
                r"you(?:'re| are| will be)\s+(?:completely\s+|definitely\s+)?safe\b",
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
            OutputViolationKind.DETERMINISTIC_SAFETY_ASSURANCE,
            re.compile(
                r"(?:你|您)\s*(?:现在|已经)\s*安全(?:了)?"
                r"(?=\s*(?:[\u3002.!\uFF01]|$))",
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
        """Run the lexical regex layer over assistant-generated text.

        A clean result means only that these patterns did not match.  Its
        semantic verification remains ``NOT_PERFORMED`` and it is therefore
        not, by itself, releasable.
        """

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
    """Authority-bound consumer release contract.

    Regex matching is a mandatory first layer, not a claim of semantic
    completeness. A verifier receipt is useful only after an independent
    authority validates its opaque evidence against the exact text hash,
    vault, session, policy generation, and trusted current time.
    """

    vault_id: str
    session_id: str
    policy_generation: int
    clock: TrustedClock = field(repr=False, compare=False)
    detector: DiagnosticLanguageDetector = field(
        default_factory=DiagnosticLanguageDetector,
        repr=False,
        compare=False,
    )
    semantic_verifier: SemanticOutputVerifier | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    semantic_authority: SemanticOutputAuthorityPort | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _require_nonempty_string(self.vault_id, field_name="vault_id")
        _require_nonempty_string(self.session_id, field_name="session_id")
        _require_policy_generation(self.policy_generation)
        if not isinstance(self.clock, TrustedClock):
            raise TypeError("clock must implement TrustedClock")
        if not isinstance(self.detector, DiagnosticLanguageDetector):
            raise TypeError("detector must be a DiagnosticLanguageDetector")
        if self.semantic_verifier is not None and not isinstance(
            self.semantic_verifier, SemanticOutputVerifier
        ):
            raise TypeError("semantic_verifier must implement SemanticOutputVerifier")
        if self.semantic_authority is not None and not isinstance(
            self.semantic_authority, SemanticOutputAuthorityPort
        ):
            raise TypeError("semantic_authority must implement SemanticOutputAuthorityPort")

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
        """Inspect at one trusted instant; callers cannot supply that instant."""

        at = read_trusted_time(self.clock)
        return self._inspect_at(text, at=at)

    def enforce(self, text: str) -> None:
        """Raise unless every release layer passes at one trusted instant."""

        at = read_trusted_time(self.clock)
        self._enforce_at(text, at=at)

    def release(self, text: str) -> str:
        """Return text only after a single-time-read fail-closed check."""

        at = read_trusted_time(self.clock)
        self._enforce_at(text, at=at)
        return text

    def _inspect_at(self, text: str, *, at: datetime) -> OutputSafetyAssessment:
        lexical_assessment = self.detector.inspect(text)
        if not lexical_assessment.lexical_layer_allowed:
            return lexical_assessment

        claims = SemanticOutputClaims(
            text=text,
            vault_id=self.vault_id,
            session_id=self.session_id,
            policy_generation=self.policy_generation,
        )
        return OutputSafetyAssessment(
            lexical_assessment.violations,
            semantic_verification=self._verify_semantics(claims, at=at),
        )

    def _enforce_at(self, text: str, *, at: datetime) -> None:
        assessment = self._inspect_at(text, at=at)
        if not assessment.allowed:
            raise UnsafeGeneratedOutputError(
                assessment.violations,
                semantic_verification=assessment.semantic_verification,
            )

    def _verify_semantics(
        self,
        claims: SemanticOutputClaims,
        *,
        at: datetime,
    ) -> OutputVerificationStatus:
        verifier = self.semantic_verifier
        if verifier is None:
            return OutputVerificationStatus.NOT_PERFORMED
        authority = self.semantic_authority
        if authority is None:
            return OutputVerificationStatus.UNAVAILABLE
        try:
            receipt = verifier.verify(claims, at=at)
        except Exception:
            return OutputVerificationStatus.UNAVAILABLE
        if not isinstance(receipt, SemanticOutputVerificationReceipt):
            return OutputVerificationStatus.INDETERMINATE
        if (
            receipt.text_hash != claims.text_hash
            or receipt.vault_id != claims.vault_id
            or receipt.session_id != claims.session_id
            or receipt.policy_generation != claims.policy_generation
            or at < receipt.issued_at
            or at >= receipt.expires_at
        ):
            return OutputVerificationStatus.REJECTED
        if receipt.status is not OutputVerificationStatus.VERIFIED:
            return receipt.status
        try:
            authority_result = authority.verify_receipt(
                receipt,
                claims=claims,
                at=at,
            )
        except Exception:
            return OutputVerificationStatus.UNAVAILABLE
        if not isinstance(authority_result, bool):
            return OutputVerificationStatus.INDETERMINATE
        if not authority_result:
            return OutputVerificationStatus.REJECTED
        return OutputVerificationStatus.VERIFIED
