from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest

from life_coach.modules.action import (
    ActionCandidate,
    ActionSafetyBlockedError,
    ActionSafetyNotCurrentError,
    ActionSafetyOutcome,
    ActionSafetyRequiredError,
    ActionSafetySubjectMismatchError,
    ConfirmationMismatchError,
    EndorsedGoal,
    GoalCandidate,
    GoalNotEndorsedError,
    IfThenPlanCandidate,
    IfThenPlanState,
    InvalidActionCandidateError,
    InvalidIfThenPlanError,
    accept_experiment,
    accept_if_then_plan,
    endorse_goal,
)
from tests.action.helpers import (
    CONTEXT,
    NOW,
    allowed_action,
    confirmation_for_action,
    confirmation_for_goal,
    confirmation_for_plan,
    verdict_for_action,
    verdict_for_plan,
)


def _experiment() -> ActionCandidate:
    return ActionCandidate.experiment_candidate(
        "experiment-1",
        "Put the phone away before bed",
        rationale="See whether bedtime feels less rushed",
        cost="Three evenings",
        exit_plan="Stop whenever it is not useful",
        is_reversible=True,
        **CONTEXT,
    )


def _goal(description: str = "Have a calmer transition into sleep") -> GoalCandidate:
    return GoalCandidate(goal_id="goal-1", description=description, **CONTEXT)


def _endorsed_goal() -> EndorsedGoal:
    goal = _goal()
    return endorse_goal(goal, confirmation_for_goal(goal), at=NOW)


def _plan(then_action: str = "Open the bedside book and read one page") -> IfThenPlanCandidate:
    return IfThenPlanCandidate(
        plan_id="plan-1",
        goal=_endorsed_goal(),
        observable_cue="When I place my phone on the charger",
        then_action=then_action,
        cue_is_observable=True,
        action_is_small_and_concrete=True,
    )


def test_experiment_candidate_requires_complete_reversible_terms() -> None:
    proposal = _experiment().experiment
    assert proposal is not None
    with pytest.raises(InvalidActionCandidateError):
        replace(_experiment(), experiment=replace(proposal, exit_plan=""))
    with pytest.raises(InvalidActionCandidateError, match="reversible"):
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Try a change",
            rationale="Learn",
            cost="One evening",
            exit_plan="Stop",
            is_reversible=False,
            **CONTEXT,
        )


def test_experiment_acceptance_requires_exact_current_allowed_verdict() -> None:
    candidate = allowed_action(_experiment())

    experiment = accept_experiment(
        candidate,
        confirmation_for_action(candidate),
        at=NOW,
    )

    assert experiment.user_accepted
    assert experiment.terms.cost == "Three evenings"


def test_experiment_fails_closed_without_verdict() -> None:
    candidate = _experiment()
    with pytest.raises(ActionSafetyRequiredError):
        accept_experiment(candidate, confirmation_for_action(candidate), at=NOW)


def test_blocked_experiment_is_never_accepted() -> None:
    candidate = _experiment()
    candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, ActionSafetyOutcome.BLOCKED)
    )
    with pytest.raises(ActionSafetyBlockedError):
        accept_experiment(candidate, confirmation_for_action(candidate), at=NOW)


def test_expired_experiment_verdict_is_rejected() -> None:
    candidate = _experiment()
    candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(seconds=1))
    )
    with pytest.raises(ActionSafetyNotCurrentError):
        accept_experiment(
            candidate,
            confirmation_for_action(candidate),
            at=NOW + timedelta(seconds=1),
        )


def test_if_then_rejects_goal_without_user_endorsement() -> None:
    with pytest.raises(GoalNotEndorsedError):
        IfThenPlanCandidate(
            plan_id="plan-1",
            goal=cast(object, _goal()),  # type: ignore[arg-type]
            observable_cue="When the cue occurs",
            then_action="Do one small thing",
            cue_is_observable=True,
            action_is_small_and_concrete=True,
        )


@pytest.mark.parametrize(
    ("cue_is_observable", "action_is_small_and_concrete"),
    [(False, True), (True, False), (cast(bool, 1), True), (True, cast(bool, "yes"))],
)
def test_if_then_requires_exact_bool_evidence_flags(
    cue_is_observable: bool,
    action_is_small_and_concrete: bool,
) -> None:
    with pytest.raises(InvalidIfThenPlanError):
        replace(
            _plan(),
            cue_is_observable=cue_is_observable,
            action_is_small_and_concrete=action_is_small_and_concrete,
        )


def test_if_then_stays_candidate_until_exact_confirmation_and_verdict() -> None:
    candidate = _plan()
    candidate = candidate.with_safety_verdict(verdict_for_plan(candidate))

    assert candidate.state is IfThenPlanState.CANDIDATE
    assert not candidate.is_executable
    plan = accept_if_then_plan(candidate, confirmation_for_plan(candidate), at=NOW)
    assert plan.state is IfThenPlanState.ACCEPTED
    assert plan.user_accepted


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, ActionSafetyRequiredError),
        (ActionSafetyOutcome.BLOCKED, ActionSafetyBlockedError),
    ],
)
def test_if_then_safety_gate_fails_closed(
    outcome: ActionSafetyOutcome | None,
    expected: type[Exception],
) -> None:
    candidate = _plan()
    if outcome is not None:
        candidate = candidate.with_safety_verdict(verdict_for_plan(candidate, outcome))
    with pytest.raises(expected):
        accept_if_then_plan(candidate, confirmation_for_plan(candidate), at=NOW)


def test_if_then_rejects_verdict_for_same_id_but_old_content() -> None:
    original = _plan("Read one page")
    old_verdict = verdict_for_plan(original)
    changed = _plan("Read ten pages")
    with pytest.raises(ActionSafetySubjectMismatchError):
        changed.with_safety_verdict(old_verdict)


def test_if_then_rejects_confirmation_for_same_id_but_changed_content() -> None:
    original = _plan("Read one page")
    confirmation = confirmation_for_plan(original)
    changed = _plan("Read ten pages")
    changed = changed.with_safety_verdict(verdict_for_plan(changed))
    with pytest.raises(ConfirmationMismatchError, match="confirmation does not bind"):
        accept_if_then_plan(changed, confirmation, at=NOW)


def test_goal_endorsement_rejects_same_id_with_changed_content() -> None:
    original = _goal("Sleep calmly")
    confirmation = confirmation_for_goal(original)
    changed = _goal("Wake earlier")
    with pytest.raises(ConfirmationMismatchError, match="confirmation does not bind"):
        endorse_goal(changed, confirmation, at=NOW)


def test_action_safety_outcome_rejects_raw_string() -> None:
    with pytest.raises(InvalidActionCandidateError):
        verdict_for_action(_experiment(), cast(ActionSafetyOutcome, "allowed"))
