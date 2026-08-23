from datetime import UTC, datetime
from typing import cast

import pytest

from life_coach.modules.action import (
    ActionCandidate,
    ActionSafetyBlockedError,
    ActionSafetyOutcome,
    ActionSafetyRequiredError,
    ActionSafetySubjectMismatchError,
    ActionSafetyVerdict,
    ConfirmationRequiredError,
    EndorsedGoal,
    ExperimentState,
    GoalCandidate,
    GoalNotEndorsedError,
    IfThenPlanCandidate,
    IfThenPlanState,
    InvalidActionCandidateError,
    InvalidIfThenPlanError,
    UserConfirmation,
    accept_experiment,
    accept_if_then_plan,
    endorse_goal,
    to_experiment,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _confirmation(candidate_id: str) -> UserConfirmation:
    return UserConfirmation(candidate_id=candidate_id, confirmed_at=NOW)


def _verdict(
    subject_id: str,
    outcome: ActionSafetyOutcome = ActionSafetyOutcome.ALLOWED,
) -> ActionSafetyVerdict:
    return ActionSafetyVerdict(
        subject_id=subject_id,
        outcome=outcome,
        policy_version="action-safety-v1",
        decided_at=NOW,
        reason="Low-risk check result",
    )


def _endorsed_goal() -> EndorsedGoal:
    goal = GoalCandidate("goal-1", "Have a calmer transition into sleep")
    return endorse_goal(goal, _confirmation(goal.goal_id))


def test_experiment_candidate_requires_reason_cost_and_exit_plan() -> None:
    with pytest.raises(InvalidActionCandidateError):
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Try putting the phone away before bed",
            rationale="See what changes",
            cost="Three evenings",
            exit_plan="",
            is_reversible=True,
            safety_verdict=_verdict("experiment-1"),
        )


def test_unconfirmed_experiment_is_not_accepted() -> None:
    candidate = ActionCandidate.experiment_candidate(
        "experiment-1",
        "Try putting the phone away before bed",
        rationale="See whether bedtime feels less rushed",
        cost="Three evenings without the phone in bed",
        exit_plan="Stop at any point if it is not useful",
        is_reversible=True,
        safety_verdict=_verdict("experiment-1"),
    )

    with pytest.raises(InvalidActionCandidateError):
        accept_experiment(
            ActionCandidate.idea("idea-1", "Put my phone elsewhere"),
            _confirmation("idea-1"),
        )
    with pytest.raises(ConfirmationRequiredError, match="unconfirmed experiment"):
        to_experiment(candidate)


def test_user_acceptance_creates_experiment_with_the_visible_terms() -> None:
    candidate = ActionCandidate.experiment_candidate(
        "experiment-1",
        "Try putting the phone away before bed",
        rationale="See whether bedtime feels less rushed",
        cost="Three evenings without the phone in bed",
        exit_plan="Stop at any point if it is not useful",
        is_reversible=True,
        safety_verdict=_verdict("experiment-1"),
    )

    experiment = accept_experiment(candidate, _confirmation(candidate.candidate_id))

    assert experiment.state is ExperimentState.ACCEPTED
    assert experiment.user_accepted
    assert experiment.terms.rationale == "See whether bedtime feels less rushed"
    assert experiment.terms.cost == "Three evenings without the phone in bed"
    assert experiment.terms.exit_plan == "Stop at any point if it is not useful"


def test_if_then_plan_rejects_a_goal_without_user_endorsement() -> None:
    unendorsed = GoalCandidate("goal-1", "Have a calmer transition into sleep")

    with pytest.raises(GoalNotEndorsedError):
        IfThenPlanCandidate(
            plan_id="plan-1",
            goal=cast(EndorsedGoal, unendorsed),
            observable_cue="When I place my phone on the charger",
            then_action="Open the book on my bedside table",
            cue_is_observable=True,
            action_is_small_and_concrete=True,
            safety_verdict=_verdict("plan-1"),
        )


@pytest.mark.parametrize(
    ("cue_is_observable", "action_is_small_and_concrete"),
    [(False, True), (True, False)],
)
def test_if_then_requires_observable_cue_and_small_concrete_action(
    cue_is_observable: bool,
    action_is_small_and_concrete: bool,
) -> None:
    with pytest.raises(InvalidIfThenPlanError):
        IfThenPlanCandidate(
            plan_id="plan-1",
            goal=_endorsed_goal(),
            observable_cue="When the cue occurs",
            then_action="Do the next behavior",
            cue_is_observable=cue_is_observable,
            action_is_small_and_concrete=action_is_small_and_concrete,
            safety_verdict=_verdict("plan-1"),
        )


def test_if_then_plan_stays_candidate_until_user_accepts_the_exact_plan() -> None:
    candidate = IfThenPlanCandidate(
        plan_id="plan-1",
        goal=_endorsed_goal(),
        observable_cue="When I place my phone on the charger",
        then_action="Open the book on my bedside table and read one page",
        cue_is_observable=True,
        action_is_small_and_concrete=True,
        safety_verdict=_verdict("plan-1"),
    )

    assert candidate.state is IfThenPlanState.CANDIDATE
    assert not candidate.is_executable
    assert candidate.location_or_time is None
    assert candidate.review_at is None
    assert candidate.reminder_at is None

    plan = accept_if_then_plan(candidate, _confirmation(candidate.plan_id))

    assert plan.state is IfThenPlanState.ACCEPTED
    assert plan.user_accepted


def test_experiment_fails_closed_without_a_safety_verdict() -> None:
    with pytest.raises(ActionSafetyRequiredError):
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Try one small change for an evening",
            rationale="Learn whether the change is useful",
            cost="One evening",
            exit_plan="Stop immediately if it is not useful",
            is_reversible=True,
        )


def test_blocked_high_risk_experiment_is_never_created() -> None:
    with pytest.raises(ActionSafetyBlockedError):
        ActionCandidate.experiment_candidate(
            "high-risk-experiment",
            "Try a behavior the safety policy identified as high-risk",
            rationale="The proposed rationale does not override safety policy",
            cost="Potential serious harm",
            exit_plan="An exit description does not make this safe",
            is_reversible=True,
            safety_verdict=_verdict(
                "high-risk-experiment",
                ActionSafetyOutcome.BLOCKED,
            ),
        )


def test_experiment_requires_a_verdict_for_the_same_subject() -> None:
    with pytest.raises(ActionSafetySubjectMismatchError):
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Try one small change for an evening",
            rationale="Learn whether the change is useful",
            cost="One evening",
            exit_plan="Stop immediately if it is not useful",
            is_reversible=True,
            safety_verdict=_verdict("experiment-2"),
        )


@pytest.mark.parametrize("is_reversible", [False, cast(bool, "yes")])
def test_experiment_must_be_explicitly_reversible_even_when_allowed(
    is_reversible: bool,
) -> None:
    with pytest.raises(InvalidActionCandidateError, match="reversible"):
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Try one small change for an evening",
            rationale="Learn whether the change is useful",
            cost="One evening",
            exit_plan="Stop immediately if it is not useful",
            is_reversible=is_reversible,
            safety_verdict=_verdict("experiment-1"),
        )


@pytest.mark.parametrize(
    ("verdict", "expected_error"),
    [
        (None, ActionSafetyRequiredError),
        (
            _verdict("plan-1", ActionSafetyOutcome.BLOCKED),
            ActionSafetyBlockedError,
        ),
        (_verdict("another-plan"), ActionSafetySubjectMismatchError),
    ],
    ids=["missing", "blocked-high-risk", "subject-mismatch"],
)
def test_if_then_plan_safety_gate_is_fail_closed(
    verdict: ActionSafetyVerdict | None,
    expected_error: type[Exception],
) -> None:
    with pytest.raises(expected_error):
        IfThenPlanCandidate(
            plan_id="plan-1",
            goal=_endorsed_goal(),
            observable_cue="When I see the observable cue",
            then_action="Perform one concrete small behavior",
            cue_is_observable=True,
            action_is_small_and_concrete=True,
            safety_verdict=verdict,
        )


def test_action_safety_outcomes_are_non_scored_policy_results() -> None:
    assert {outcome.value for outcome in ActionSafetyOutcome} == {"allowed", "blocked"}
    assert "risk_score" not in ActionSafetyVerdict.__dataclass_fields__


def test_action_safety_outcome_rejects_raw_string_at_runtime() -> None:
    with pytest.raises(InvalidActionCandidateError):
        ActionSafetyVerdict(
            subject_id="experiment-1",
            outcome=cast(ActionSafetyOutcome, "allowed"),
            policy_version="action-safety-v1",
            decided_at=NOW,
            reason="Raw strings must not pass the safety gate",
        )


@pytest.mark.parametrize(
    ("cue_is_observable", "action_is_small_and_concrete"),
    [
        (cast(bool, 1), True),
        (True, cast(bool, "yes")),
    ],
    ids=["truthy-non-bool-cue", "truthy-non-bool-action"],
)
def test_if_then_control_flags_require_exact_bool_runtime_types(
    cue_is_observable: bool,
    action_is_small_and_concrete: bool,
) -> None:
    with pytest.raises(InvalidIfThenPlanError, match="must be a bool"):
        IfThenPlanCandidate(
            plan_id="plan-1",
            goal=_endorsed_goal(),
            observable_cue="When I see the observable cue",
            then_action="Perform one concrete small behavior",
            cue_is_observable=cue_is_observable,
            action_is_small_and_concrete=action_is_small_and_concrete,
            safety_verdict=_verdict("plan-1"),
        )
