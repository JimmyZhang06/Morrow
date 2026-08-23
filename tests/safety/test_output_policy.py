from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import signature
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
    SemanticOutputAuthorityPort,
    SemanticOutputClaims,
    SemanticOutputVerificationReceipt,
    SemanticOutputVerifier,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


class FakeTrustedClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.current


class RecordingSemanticAuthority:
    def __init__(self, *, result: object = True, raises: bool = False) -> None:
        self.result = result
        self.raises = raises
        self.trusted: set[SemanticOutputVerificationReceipt] = set()
        self.seen: list[
            tuple[SemanticOutputVerificationReceipt, SemanticOutputClaims, datetime]
        ] = []

    def trust(self, receipt: SemanticOutputVerificationReceipt) -> None:
        self.trusted.add(receipt)

    def verify_receipt(
        self,
        receipt: SemanticOutputVerificationReceipt,
        *,
        claims: SemanticOutputClaims,
        at: datetime,
    ) -> bool:
        self.seen.append((receipt, claims, at))
        if self.raises:
            raise RuntimeError("authority unavailable")
        if receipt not in self.trusted:
            return False
        if (
            receipt.text_hash != claims.text_hash
            or receipt.vault_id != claims.vault_id
            or receipt.session_id != claims.session_id
            or receipt.policy_generation != claims.policy_generation
            or at < receipt.issued_at
            or at >= receipt.expires_at
        ):
            return False
        return cast(bool, self.result)


class RecordingReceiptVerifier:
    def __init__(
        self,
        authority: RecordingSemanticAuthority,
        *,
        status: OutputVerificationStatus = OutputVerificationStatus.VERIFIED,
        text_hash: str | None = None,
        vault_id: str | None = None,
        session_id: str | None = None,
        policy_generation: int | None = None,
        issued_offset: timedelta = timedelta(),
        expires_offset: timedelta = timedelta(minutes=5),
        register: bool = True,
    ) -> None:
        self.authority = authority
        self.status = status
        self.text_hash = text_hash
        self.vault_id = vault_id
        self.session_id = session_id
        self.policy_generation = policy_generation
        self.issued_offset = issued_offset
        self.expires_offset = expires_offset
        self.register = register
        self.seen: list[tuple[SemanticOutputClaims, datetime]] = []

    def verify(
        self,
        claims: SemanticOutputClaims,
        *,
        at: datetime,
    ) -> SemanticOutputVerificationReceipt:
        self.seen.append((claims, at))
        receipt = SemanticOutputVerificationReceipt(
            opaque_receipt=f"opaque-{len(self.seen)}",
            issuer="test-output-authority",
            text_hash=self.text_hash or claims.text_hash,
            vault_id=self.vault_id or claims.vault_id,
            session_id=self.session_id or claims.session_id,
            policy_generation=self.policy_generation or claims.policy_generation,
            issued_at=at + self.issued_offset,
            expires_at=at + self.expires_offset,
            status=self.status,
        )
        if self.register:
            self.authority.trust(receipt)
        return receipt


class InvalidReceiptVerifier:
    def verify(
        self,
        claims: SemanticOutputClaims,
        *,
        at: datetime,
    ) -> SemanticOutputVerificationReceipt:
        del claims, at
        return cast(SemanticOutputVerificationReceipt, True)


class FailingReceiptVerifier:
    def __init__(self) -> None:
        self.seen: list[tuple[SemanticOutputClaims, datetime]] = []

    def verify(
        self,
        claims: SemanticOutputClaims,
        *,
        at: datetime,
    ) -> SemanticOutputVerificationReceipt:
        self.seen.append((claims, at))
        raise RuntimeError("semantic verifier unavailable")


def receipt_policy(
    *,
    clock: FakeTrustedClock | None = None,
    authority: RecordingSemanticAuthority | None = None,
    verifier: SemanticOutputVerifier | None = None,
    vault_id: str = "vault-1",
    session_id: str = "session-1",
    policy_generation: int = 7,
) -> tuple[
    NonDiagnosticOutputPolicy,
    FakeTrustedClock,
    RecordingSemanticAuthority,
    SemanticOutputVerifier,
]:
    resolved_clock = clock or FakeTrustedClock()
    resolved_authority = authority or RecordingSemanticAuthority()
    resolved_verifier = verifier or RecordingReceiptVerifier(resolved_authority)
    return (
        NonDiagnosticOutputPolicy(
            vault_id=vault_id,
            session_id=session_id,
            policy_generation=policy_generation,
            clock=resolved_clock,
            semantic_verifier=resolved_verifier,
            semantic_authority=resolved_authority,
        ),
        resolved_clock,
        resolved_authority,
        resolved_verifier,
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
        (
            "根据你的 description\uff0c可以确诊为 depression。",
            OutputViolationKind.CLINICAL_DIAGNOSIS,
        ),
        (
            "Based on 你的描述, this confirms 抑郁症。",
            OutputViolationKind.CLINICAL_DIAGNOSIS,
        ),
        ("你就是边缘型人格。", OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT),
        ("你可能是边缘型人格。", OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT),
        ("You may be a narcissist.", OutputViolationKind.PERSONALITY_DISORDER_JUDGMENT),
        ("你的自杀风险是高风险。", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        ("suicide risk: 23%", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        ("Your suicide risk is zero.", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        ("你的 suicide risk is high。", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
        ("Your 自杀风险 is 中风险。", OutputViolationKind.SUICIDE_RISK_SCORE_OR_TIER),
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
    policy, clock, _authority, verifier = receipt_policy()

    assert any("clinical diagnosis" in constraint for constraint in policy.generation_constraints)
    with pytest.raises(UnsafeGeneratedOutputError):
        policy.enforce("You have a personality disorder.")
    assert isinstance(verifier, RecordingReceiptVerifier)
    assert verifier.seen == []
    assert clock.calls == 1


def test_policy_allows_tentative_non_pathologizing_reflection() -> None:
    text = "One possibility is that this situation felt isolating; you may see it differently."
    policy, clock, authority, verifier = receipt_policy()

    policy.enforce(text)

    assert isinstance(verifier, RecordingReceiptVerifier)
    assert [claims.text for claims, _at in verifier.seen] == [text]
    assert authority.seen[0][1].text == text
    assert clock.calls == 1


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
    authority = RecordingSemanticAuthority()
    verifier = RecordingReceiptVerifier(authority, status=status)
    policy, _clock, _authority, _verifier = receipt_policy(
        authority=authority,
        verifier=verifier,
    )
    text = "A tentative, non-pathologizing reflection."

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release(text)

    assert exc_info.value.violations == ()
    assert exc_info.value.semantic_verification is status
    assert [claims.text for claims, _at in verifier.seen] == [text]
    assert authority.seen == []


def test_policy_fails_closed_when_semantic_verification_is_missing() -> None:
    clock = FakeTrustedClock()
    policy = NonDiagnosticOutputPolicy(
        vault_id="vault-1",
        session_id="session-1",
        policy_generation=7,
        clock=clock,
    )
    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("A tentative, non-pathologizing reflection.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.NOT_PERFORMED
    assert clock.calls == 1


def test_policy_fails_closed_when_authority_is_missing() -> None:
    verifier_authority = RecordingSemanticAuthority()
    verifier = RecordingReceiptVerifier(verifier_authority)
    policy = NonDiagnosticOutputPolicy(
        vault_id="vault-1",
        session_id="session-1",
        policy_generation=7,
        clock=FakeTrustedClock(),
        semantic_verifier=verifier,
    )

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("A tentative, non-pathologizing reflection.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.UNAVAILABLE


def test_policy_fails_closed_when_verifier_returns_non_receipt_value() -> None:
    policy, _clock, _authority, _verifier = receipt_policy(verifier=InvalidReceiptVerifier())

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("A tentative, non-pathologizing reflection.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.INDETERMINATE


def test_policy_fails_closed_when_verifier_raises() -> None:
    verifier = FailingReceiptVerifier()
    policy, _clock, _authority, _verifier = receipt_policy(verifier=verifier)
    text = "A tentative, non-pathologizing reflection."

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release(text)

    assert exc_info.value.semantic_verification is OutputVerificationStatus.UNAVAILABLE
    assert [claims.text for claims, _at in verifier.seen] == [text]


@pytest.mark.parametrize(
    ("authority_result", "raises", "expected"),
    [
        (False, False, OutputVerificationStatus.REJECTED),
        ("true", False, OutputVerificationStatus.INDETERMINATE),
        (True, True, OutputVerificationStatus.UNAVAILABLE),
    ],
)
def test_policy_fails_closed_when_authority_does_not_authenticate(
    authority_result: object,
    raises: bool,
    expected: OutputVerificationStatus,
) -> None:
    authority = RecordingSemanticAuthority(result=authority_result, raises=raises)
    verifier = RecordingReceiptVerifier(authority)
    policy, _clock, _authority, _verifier = receipt_policy(
        authority=authority,
        verifier=verifier,
    )

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("A tentative, non-pathologizing reflection.")

    assert exc_info.value.semantic_verification is expected


def test_release_returns_verified_text() -> None:
    text = "One possibility is that this felt lonely; you may see it differently."
    policy, clock, authority, verifier = receipt_policy()

    released = policy.release(text)

    assert released == text
    assert isinstance(verifier, RecordingReceiptVerifier)
    assert [claims.text for claims, _at in verifier.seen] == [text]
    assert len(authority.seen) == 1
    assert clock.calls == 1


def test_verification_is_recomputed_for_each_exact_text() -> None:
    first = "The first tentative reflection."
    second = "The second tentative reflection."
    policy, clock, _authority, verifier = receipt_policy()

    assert policy.release(first) == first
    assert policy.release(second) == second

    assert isinstance(verifier, RecordingReceiptVerifier)
    claims = [item[0] for item in verifier.seen]
    assert [claim.text for claim in claims] == [first, second]
    assert claims[0].text_hash != claims[1].text_hash
    assert clock.calls == 2


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("text_hash", "0" * 64),
        ("vault_id", "vault-2"),
        ("session_id", "session-2"),
        ("policy_generation", 8),
    ],
)
def test_receipt_must_bind_exact_text_and_current_scope(override: str, value: object) -> None:
    authority = RecordingSemanticAuthority()
    options: dict[str, object] = {override: value}
    verifier = RecordingReceiptVerifier(authority, **options)  # type: ignore[arg-type]
    policy, _clock, _authority, _verifier = receipt_policy(
        authority=authority,
        verifier=verifier,
    )

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("Exact release text.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.REJECTED
    assert authority.seen == []


def test_handcrafted_receipt_is_rejected_even_when_claim_fields_match() -> None:
    authority = RecordingSemanticAuthority()
    verifier = RecordingReceiptVerifier(authority, register=False)
    policy, _clock, _authority, _verifier = receipt_policy(
        authority=authority,
        verifier=verifier,
    )

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("Exact release text.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.REJECTED
    assert len(authority.seen) == 1


@pytest.mark.parametrize(
    ("issued_offset", "expires_offset"),
    [
        (timedelta(minutes=-5), timedelta(seconds=-1)),
        (timedelta(seconds=1), timedelta(minutes=5)),
    ],
)
def test_expired_or_clock_rollback_receipt_is_rejected_before_authority(
    issued_offset: timedelta,
    expires_offset: timedelta,
) -> None:
    authority = RecordingSemanticAuthority()
    verifier = RecordingReceiptVerifier(
        authority,
        issued_offset=issued_offset,
        expires_offset=expires_offset,
    )
    policy, clock, _authority, _verifier = receipt_policy(
        authority=authority,
        verifier=verifier,
    )

    with pytest.raises(UnsafeGeneratedOutputError) as exc_info:
        policy.release("Exact release text.")

    assert exc_info.value.semantic_verification is OutputVerificationStatus.REJECTED
    assert authority.seen == []
    assert clock.calls == 1


def test_text_hash_is_exact_utf8_without_trimming_or_unicode_rewriting() -> None:
    policy, _clock, _authority, verifier = receipt_policy()

    policy.release("café")
    policy.release("cafe\u0301")
    policy.release("café ")

    assert isinstance(verifier, RecordingReceiptVerifier)
    hashes = [claims.text_hash for claims, _at in verifier.seen]
    assert len(set(hashes)) == 3


def test_each_public_entry_reads_trusted_clock_exactly_once() -> None:
    policy, clock, _authority, _verifier = receipt_policy()

    assert policy.inspect("First safe reflection.").allowed
    policy.enforce("Second safe reflection.")
    assert policy.release("Third safe reflection.") == "Third safe reflection."

    assert clock.calls == 3


def test_policy_entries_do_not_accept_caller_supplied_time() -> None:
    for method in (
        NonDiagnosticOutputPolicy.inspect,
        NonDiagnosticOutputPolicy.enforce,
        NonDiagnosticOutputPolicy.release,
    ):
        assert "at" not in signature(method).parameters


def test_semantic_verifier_protocol_is_runtime_checkable() -> None:
    authority = RecordingSemanticAuthority()
    verifier = RecordingReceiptVerifier(authority)

    assert isinstance(verifier, SemanticOutputVerifier)
    assert isinstance(authority, SemanticOutputAuthorityPort)


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
