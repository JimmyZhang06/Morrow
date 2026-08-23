from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.safety import (
    ExpiredSafetyStateError,
    InactiveSafetyStateError,
    RuleBasedSafetyGateway,
    SafetyGateway,
    SafetyPriority,
    SafetyRoute,
    SafetyState,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


def state(**overrides: object) -> SafetyState:
    values: dict[str, object] = {
        "session_id": "session",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return SafetyState(**values)  # type: ignore[arg-type]


def test_gateway_implements_the_public_interface() -> None:
    assert isinstance(RuleBasedSafetyGateway(), SafetyGateway)


def test_decision_is_current_only_inside_its_safety_state_window() -> None:
    short_lived = state(expires_at=NOW + timedelta(minutes=5))

    decision = RuleBasedSafetyGateway().evaluate(short_lived, at=NOW)

    assert decision.evaluated_at == NOW
    assert decision.valid_until == short_lived.expires_at
    assert not decision.is_current(at=NOW - timedelta(microseconds=1))
    assert decision.is_current(at=NOW)
    assert decision.is_current(at=short_lived.expires_at - timedelta(microseconds=1))
    assert not decision.is_current(at=short_lived.expires_at)


def test_decision_currentness_rejects_naive_time() -> None:
    decision = RuleBasedSafetyGateway().evaluate(state(), at=NOW)

    with pytest.raises(ValueError, match="at must be timezone-aware"):
        decision.is_current(at=datetime(2030, 1, 1, 12))


def test_ordinary_sadness_does_not_trigger_a_crisis_route() -> None:
    ordinary_sadness = state(
        possible_current_danger=False,
        ongoing_distress=True,
    )

    decision = RuleBasedSafetyGateway().evaluate(ordinary_sadness, at=NOW)

    assert decision.route is SafetyRoute.ORDINARY_COACH
    assert decision.priority is SafetyPriority.ORDINARY
    assert decision.ordinary_coach_allowed
    assert not decision.ordinary_coach_paused


def test_possible_current_danger_pauses_coach_for_brief_clarification() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(possible_current_danger=True),
        at=NOW,
    )

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY
    assert decision.ordinary_coach_paused


def test_partial_negative_answers_still_require_immediate_safety_clarification() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(possible_current_danger=True, harm_already_occurred=False),
        at=NOW,
    )

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


def test_possible_current_danger_is_clarified_before_ongoing_distress_routing() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(possible_current_danger=True, ongoing_safety_concern=True),
        at=NOW,
    )

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


def test_possible_current_danger_is_clarified_before_non_imminent_support_routing() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(possible_current_danger=True, current_intent=True),
        at=NOW,
    )

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


def test_explicitly_ruled_out_immediate_danger_can_route_ongoing_support() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(
            possible_current_danger=True,
            immediate_danger=False,
            ongoing_safety_concern=True,
        ),
        at=NOW,
    )

    assert decision.route is SafetyRoute.ONGOING_HUMAN_SUPPORT


def test_crisis_route_pauses_all_ordinary_coaching() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(
            current_intent=True,
            plan_present=True,
            accessible_means=True,
            imminent_timeframe=True,
        ),
        at=NOW,
    )

    assert decision.route is SafetyRoute.IMMEDIATE_DANGER
    assert decision.ordinary_coach_paused
    assert not decision.ordinary_coach_allowed
    assert not decision.automatically_contacts_third_party


@pytest.mark.parametrize("current_signal", ["current_intent", "plan_present", "accessible_means"])
def test_each_current_signal_requires_clarification_while_immediacy_is_unknown(
    current_signal: str,
) -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(**{current_signal: True}),
        at=NOW,
    )

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY
    assert decision.ordinary_coach_paused


def test_complete_current_signal_set_does_not_infer_imminence() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(current_intent=True, plan_present=True, accessible_means=True),
        at=NOW,
    )

    assert decision.route is SafetyRoute.CLARIFY_IMMEDIATE_SAFETY


@pytest.mark.parametrize("current_signal", ["current_intent", "plan_present", "accessible_means"])
@pytest.mark.parametrize("exclusion", ["immediate_danger", "imminent_timeframe"])
def test_current_signal_routes_to_ongoing_support_only_after_immediacy_is_excluded(
    current_signal: str,
    exclusion: str,
) -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(**{current_signal: True, exclusion: False}),
        at=NOW,
    )

    assert decision.route is SafetyRoute.ONGOING_HUMAN_SUPPORT


@pytest.mark.parametrize("current_signal", ["current_intent", "plan_present", "accessible_means"])
def test_each_current_signal_with_imminent_timeframe_is_immediate_danger(
    current_signal: str,
) -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(**{current_signal: True, "imminent_timeframe": True}),
        at=NOW,
    )

    assert decision.route is SafetyRoute.IMMEDIATE_DANGER


def test_possible_medical_emergency_has_priority_after_harm() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(
            harm_already_occurred=True,
            possible_medical_emergency=True,
            immediate_danger=True,
        ),
        at=NOW,
    )

    assert decision.route is SafetyRoute.MEDICAL_EMERGENCY
    assert decision.priority is SafetyPriority.MEDICAL_FIRST
    assert decision.possible_medical_emergency_takes_priority
    assert not decision.may_continue_psychological_questionnaire
    assert decision.ordinary_coach_paused


def test_harm_with_unknown_medical_status_fails_safe_to_medical_first() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(harm_already_occurred=True, possible_medical_emergency=None),
        at=NOW,
    )

    assert decision.route is SafetyRoute.MEDICAL_EMERGENCY
    assert not decision.may_continue_psychological_questionnaire


def test_abuse_context_never_assumes_a_contact_is_safe() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(
            possible_current_danger=True,
            abuse_or_coercive_control_context=True,
            supporter_selected_by_user=True,
            supporter_confirmed_safe=False,
        ),
        at=NOW,
    )

    assert decision.ordinary_coach_paused
    assert not decision.may_suggest_selected_supporter


def test_explicit_user_selection_and_safety_confirmation_are_both_required() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(
            possible_current_danger=True,
            abuse_or_coercive_control_context=True,
            supporter_selected_by_user=True,
            supporter_confirmed_safe=True,
            contacting_supporter_feasible=True,
        ),
        at=NOW,
    )

    assert decision.may_suggest_selected_supporter


def test_unconfirmed_supporter_feasibility_is_not_treated_as_feasible() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        state(
            possible_current_danger=True,
            supporter_selected_by_user=True,
            supporter_confirmed_safe=True,
            contacting_supporter_feasible=None,
        ),
        at=NOW,
    )

    assert not decision.may_suggest_selected_supporter


def test_expired_short_term_state_must_not_be_reused() -> None:
    short_lived = state()

    with pytest.raises(ExpiredSafetyStateError):
        RuleBasedSafetyGateway().evaluate(short_lived, at=short_lived.expires_at)


def test_future_state_must_not_be_used_before_creation() -> None:
    future = state(created_at=NOW + timedelta(minutes=1))

    with pytest.raises(InactiveSafetyStateError):
        RuleBasedSafetyGateway().evaluate(future, at=NOW)


def test_truthy_string_is_not_accepted_as_a_safety_boolean() -> None:
    with pytest.raises(TypeError, match="possible_current_danger must be a bool"):
        state(possible_current_danger="true")
