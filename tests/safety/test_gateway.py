from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.safety.authority import (
    SafetyAuthorityPort,
    SafetyAuthorityReceipt,
    SafetyAuthorityVerificationError,
    SafetyDecisionClaims,
    SafetyPermitClaims,
    TrustedClock,
)
from life_coach.modules.safety.gateway import (
    ExpiredSafetyStateError,
    InactiveSafetyStateError,
    RuleBasedSafetyGateway,
    SafetyDecision,
    SafetyGateway,
    SafetyPriority,
    SafetyRoute,
    SafetyState,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
POLICY_GENERATION = "safety-policy-7"


class ClockRollbackDetectedError(RuntimeError):
    pass


class FakeTrustedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.calls = 0
        self._last_returned: datetime | None = None

    def now(self) -> datetime:
        self.calls += 1
        if self._last_returned is not None and self.current < self._last_returned:
            raise ClockRollbackDetectedError("trusted clock rolled back")
        self._last_returned = self.current
        return self.current


class FakeSafetyAuthority:
    """In-memory opaque authority used only by domain tests."""

    def __init__(self) -> None:
        self._next_receipt = 0
        self.decisions: dict[str, SafetyDecisionClaims] = {}
        self.permits: dict[str, SafetyPermitClaims] = {}
        self.consumed_permits: set[str] = set()

    def _receipt(self, kind: str) -> SafetyAuthorityReceipt:
        self._next_receipt += 1
        return SafetyAuthorityReceipt(f"{kind}-receipt-{self._next_receipt}")

    def issue_decision(self, claims: SafetyDecisionClaims) -> SafetyAuthorityReceipt:
        receipt = self._receipt("decision")
        self.decisions[receipt.value] = claims
        return receipt

    def verify_decision(
        self,
        claims: SafetyDecisionClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        return self.decisions.get(receipt.value) == claims

    def issue_permit(self, claims: SafetyPermitClaims) -> SafetyAuthorityReceipt:
        parent = self.decisions.get(claims.decision_receipt.value)
        if (
            parent is None
            or parent.route != SafetyRoute.ORDINARY_COACH.value
            or parent.binding != claims.binding
            or claims.issued_at < parent.evaluated_at
            or claims.expires_at > parent.expires_at
        ):
            raise SafetyAuthorityVerificationError("invalid parent decision for permit")
        receipt = self._receipt("permit")
        self.permits[receipt.value] = claims
        return receipt

    def verify_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        return (
            receipt.value not in self.consumed_permits and self.permits.get(receipt.value) == claims
        )

    def verify_and_consume_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        if not self.verify_permit(claims, receipt):
            return False
        self.consumed_permits.add(receipt.value)
        return True


def state(**overrides: object) -> SafetyState:
    values: dict[str, object] = {
        "vault_id": "vault-1",
        "principal_id": "user-1",
        "session_id": "session-1",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return SafetyState(**values)  # type: ignore[arg-type]


def configured_gateway(
    *,
    at: datetime = NOW,
) -> tuple[RuleBasedSafetyGateway, FakeTrustedClock, FakeSafetyAuthority]:
    clock = FakeTrustedClock(at)
    authority = FakeSafetyAuthority()
    return (
        RuleBasedSafetyGateway(clock, authority, POLICY_GENERATION),
        clock,
        authority,
    )


def decide(safety_state: SafetyState, *, at: datetime = NOW) -> SafetyDecision:
    gateway, _, _ = configured_gateway(at=at)
    return gateway.evaluate(safety_state)


def test_gateway_implements_public_clock_and_authority_interfaces() -> None:
    gateway, clock, authority = configured_gateway()

    assert isinstance(gateway, SafetyGateway)
    assert isinstance(clock, TrustedClock)
    assert isinstance(authority, SafetyAuthorityPort)


def test_gateway_reads_clock_once_and_issues_exact_canonical_binding() -> None:
    gateway, clock, authority = configured_gateway()
    safety_state = state()

    decision = gateway.evaluate(safety_state)

    assert clock.calls == 1
    assert decision.binding.vault_id == "vault-1"
    assert decision.binding.principal_id == "user-1"
    assert decision.binding.session_id == "session-1"
    assert decision.binding.input_fingerprint == safety_state.input_fingerprint
    assert len(decision.binding.input_fingerprint) == 64
    assert decision.binding.policy_generation == POLICY_GENERATION
    assert authority.verify_decision(decision.claims, decision.authority_receipt)


def test_routing_fact_changes_recompute_input_fingerprint() -> None:
    ordinary = state()
    current_concern = state(current_intent=True)

    assert ordinary.input_fingerprint != current_concern.input_fingerprint
    assert ordinary.input_fingerprint == state().input_fingerprint


def test_gateway_no_longer_accepts_caller_controlled_time() -> None:
    gateway, _, _ = configured_gateway()

    with pytest.raises(TypeError):
        gateway.evaluate(state(), at=NOW)  # type: ignore[call-arg]


def test_state_requires_valid_authority_scope() -> None:
    with pytest.raises(ValueError, match="vault_id must not be empty"):
        state(vault_id=" ")


def test_decision_is_current_only_inside_its_signed_state_window() -> None:
    short_lived = state(expires_at=NOW + timedelta(minutes=5))

    decision = decide(short_lived)

    assert decision.evaluated_at == NOW
    assert decision.expires_at == short_lived.expires_at
    assert not decision.is_current(at=NOW - timedelta(microseconds=1))
    assert decision.is_current(at=NOW)
    assert decision.is_current(at=short_lived.expires_at - timedelta(microseconds=1))
    assert not decision.is_current(at=short_lived.expires_at)


def test_decision_currentness_rejects_naive_time() -> None:
    decision = decide(state())

    with pytest.raises(ValueError, match="at must be timezone-aware"):
        decision.is_current(at=datetime(2030, 1, 1, 12))


def test_publicly_forged_or_changed_decision_fails_authority_verification() -> None:
    gateway, _, authority = configured_gateway()
    decision = gateway.evaluate(state())
    forged_receipt = replace(
        decision,
        authority_receipt=SafetyAuthorityReceipt("public-forgery"),
    )
    changed_route = replace(
        decision,
        claims=replace(decision.claims, route=SafetyRoute.IMMEDIATE_DANGER.value),
    )

    assert not authority.verify_decision(forged_receipt.claims, forged_receipt.authority_receipt)
    assert not authority.verify_decision(changed_route.claims, changed_route.authority_receipt)


def test_trusted_clock_rollback_object_blocks_reuse() -> None:
    gateway, clock, _ = configured_gateway()
    gateway.evaluate(state())
    clock.current = NOW - timedelta(seconds=1)

    with pytest.raises(ClockRollbackDetectedError):
        gateway.evaluate(state())


def test_ordinary_sadness_does_not_trigger_a_crisis_route() -> None:
    decision = decide(state(possible_current_danger=False, ongoing_distress=True))

    assert decision.route is SafetyRoute.ORDINARY_COACH
    assert decision.priority is SafetyPriority.ORDINARY
    assert decision.ordinary_coach_allowed
    assert not decision.ordinary_coach_paused


def test_possible_current_danger_pauses_coach_for_brief_clarification() -> None:
    decision = decide(state(possible_current_danger=True))

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY
    assert decision.ordinary_coach_paused


def test_partial_negative_answers_still_require_immediate_safety_clarification() -> None:
    decision = decide(state(possible_current_danger=True, harm_already_occurred=False))

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


def test_possible_current_danger_precedes_ongoing_support_routes() -> None:
    for concerning_state in (
        state(possible_current_danger=True, ongoing_safety_concern=True),
        state(possible_current_danger=True, current_intent=True),
    ):
        assert decide(concerning_state).route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


def test_explicitly_ruled_out_immediate_danger_can_route_ongoing_support() -> None:
    decision = decide(
        state(
            possible_current_danger=True,
            immediate_danger=False,
            ongoing_safety_concern=True,
        )
    )

    assert decision.route is SafetyRoute.ONGOING_HUMAN_SUPPORT


def test_crisis_route_pauses_all_ordinary_coaching() -> None:
    decision = decide(
        state(
            current_intent=True,
            plan_present=True,
            accessible_means=True,
            imminent_timeframe=True,
        )
    )

    assert decision.route is SafetyRoute.IMMEDIATE_DANGER
    assert decision.ordinary_coach_paused
    assert not decision.ordinary_coach_allowed
    assert not decision.automatically_contacts_third_party


@pytest.mark.parametrize("current_signal", ["current_intent", "plan_present", "accessible_means"])
def test_each_current_signal_requires_clarification_while_immediacy_is_unknown(
    current_signal: str,
) -> None:
    decision = decide(state(**{current_signal: True}))

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY
    assert decision.ordinary_coach_paused


def test_complete_current_signal_set_does_not_infer_imminence() -> None:
    decision = decide(state(current_intent=True, plan_present=True, accessible_means=True))

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


@pytest.mark.parametrize("current_signal", ["current_intent", "plan_present", "accessible_means"])
@pytest.mark.parametrize("exclusion", ["immediate_danger", "imminent_timeframe"])
def test_current_signal_routes_to_ongoing_support_only_after_immediacy_is_excluded(
    current_signal: str,
    exclusion: str,
) -> None:
    decision = decide(state(**{current_signal: True, exclusion: False}))

    assert decision.route is SafetyRoute.ONGOING_HUMAN_SUPPORT


@pytest.mark.parametrize("current_signal", ["current_intent", "plan_present", "accessible_means"])
def test_each_current_signal_with_imminent_timeframe_is_immediate_danger(
    current_signal: str,
) -> None:
    decision = decide(state(**{current_signal: True, "imminent_timeframe": True}))

    assert decision.route is SafetyRoute.IMMEDIATE_DANGER


def test_possible_medical_emergency_has_priority_after_harm() -> None:
    decision = decide(
        state(
            harm_already_occurred=True,
            possible_medical_emergency=True,
            immediate_danger=True,
        )
    )

    assert decision.route is SafetyRoute.MEDICAL_EMERGENCY
    assert decision.priority is SafetyPriority.MEDICAL_FIRST
    assert decision.possible_medical_emergency_takes_priority
    assert not decision.may_continue_psychological_questionnaire
    assert decision.ordinary_coach_paused


def test_harm_with_unknown_medical_status_fails_safe_to_medical_first() -> None:
    decision = decide(state(harm_already_occurred=True, possible_medical_emergency=None))

    assert decision.route is SafetyRoute.MEDICAL_EMERGENCY
    assert not decision.may_continue_psychological_questionnaire


def test_abuse_context_never_assumes_a_contact_is_safe() -> None:
    decision = decide(
        state(
            possible_current_danger=True,
            abuse_or_coercive_control_context=True,
            supporter_selected_by_user=True,
            supporter_confirmed_safe=False,
        )
    )

    assert decision.ordinary_coach_paused
    assert not decision.may_suggest_selected_supporter


def test_explicit_user_selection_and_safety_confirmation_are_both_required() -> None:
    decision = decide(
        state(
            possible_current_danger=True,
            abuse_or_coercive_control_context=True,
            supporter_selected_by_user=True,
            supporter_confirmed_safe=True,
            contacting_supporter_feasible=True,
        )
    )

    assert decision.may_suggest_selected_supporter


def test_unconfirmed_supporter_feasibility_is_not_treated_as_feasible() -> None:
    decision = decide(
        state(
            possible_current_danger=True,
            supporter_selected_by_user=True,
            supporter_confirmed_safe=True,
            contacting_supporter_feasible=None,
        )
    )

    assert not decision.may_suggest_selected_supporter


def test_expired_short_term_state_must_not_be_reused() -> None:
    short_lived = state()

    with pytest.raises(ExpiredSafetyStateError):
        decide(short_lived, at=short_lived.expires_at)


def test_future_state_must_not_be_used_before_creation() -> None:
    future = state(created_at=NOW + timedelta(minutes=1))

    with pytest.raises(InactiveSafetyStateError):
        decide(future)


def test_truthy_string_is_not_accepted_as_a_safety_boolean() -> None:
    with pytest.raises(TypeError, match="possible_current_danger must be a bool"):
        state(possible_current_danger="true")
