from datetime import UTC, datetime
from typing import cast

import pytest

from life_coach.modules.action import (
    ActionCandidate,
    ActionIntent,
    ActionSafetyOutcome,
    ActionSafetyVerdict,
    CandidateState,
    ConfirmationActor,
    ConfirmationMismatchError,
    ConfirmationRequiredError,
    IntentCannotBecomeTaskError,
    InvalidActionCandidateError,
    TaskState,
    UserConfirmation,
    confirm_candidate,
    to_task,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _confirmation(candidate_id: str) -> UserConfirmation:
    return UserConfirmation(candidate_id=candidate_id, confirmed_at=NOW)


def _allowed(subject_id: str) -> ActionSafetyVerdict:
    return ActionSafetyVerdict(
        subject_id=subject_id,
        outcome=ActionSafetyOutcome.ALLOWED,
        policy_version="action-safety-v1",
        decided_at=NOW,
        reason="Low-risk and reversible behavior",
    )


def test_intent_types_are_distinct_and_all_start_as_non_executable_candidates() -> None:
    candidates = (
        ActionCandidate.commitment("commitment-1", "I will reply to Li"),
        ActionCandidate.wish("wish-1", "I wish I had time to learn piano"),
        ActionCandidate.concern("concern-1", "The clutter is bothering me"),
        ActionCandidate.idea("idea-1", "Maybe I could make a podcast"),
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Try putting the phone away before bed for three days",
            rationale="See whether bedtime feels less rushed",
            cost="Three evenings without the phone in bed",
            exit_plan="Stop at any time if it is not useful",
            is_reversible=True,
            safety_verdict=_allowed("experiment-1"),
        ),
        ActionCandidate.external_action_candidate(
            "external-1",
            "Create a calendar event",
            scope="calendar.events.create",
            payload={"title": "Call Li"},
        ),
    )

    assert {candidate.intent for candidate in candidates} == set(ActionIntent)
    assert all(candidate.state is CandidateState.CANDIDATE for candidate in candidates)
    assert all(not candidate.is_executable for candidate in candidates)


@pytest.mark.parametrize(
    "candidate",
    [
        ActionCandidate.wish("wish-1", "I wish I could learn piano"),
        ActionCandidate.concern("concern-1", "The room is stressing me out"),
        ActionCandidate.idea("idea-1", "Maybe I could start a podcast"),
    ],
    ids=["wish", "concern", "idea"],
)
def test_confirmed_non_commitment_never_directly_becomes_a_todo(
    candidate: ActionCandidate,
) -> None:
    confirmed = confirm_candidate(candidate, _confirmation(candidate.candidate_id))

    with pytest.raises(IntentCannotBecomeTaskError):
        to_task(confirmed)


def test_unconfirmed_commitment_does_not_create_a_task() -> None:
    candidate = ActionCandidate.commitment("commitment-1", "Reply to Li")

    with pytest.raises(ConfirmationRequiredError):
        to_task(candidate)


def test_explicit_user_confirmation_converts_commitment_without_invented_metadata() -> None:
    candidate = ActionCandidate.commitment("commitment-1", "Reply to Li")
    confirmed = confirm_candidate(candidate, _confirmation(candidate.candidate_id))

    task = to_task(confirmed, task_id="task-1")

    assert task.state is TaskState.OPEN
    assert task.description == "Reply to Li"
    assert task.source_candidate_id == candidate.candidate_id
    assert task.deadline is None
    assert task.priority is None


@pytest.mark.parametrize(
    ("explicit", "actor"),
    [
        (False, ConfirmationActor.USER),
        (True, ConfirmationActor.MODEL),
        (True, ConfirmationActor.SYSTEM),
    ],
)
def test_only_explicit_user_confirmation_counts(
    explicit: bool,
    actor: ConfirmationActor,
) -> None:
    candidate = ActionCandidate.commitment("commitment-1", "Reply to Li")
    confirmation = UserConfirmation(
        candidate_id=candidate.candidate_id,
        confirmed_at=NOW,
        explicit=explicit,
        actor=actor,
    )

    with pytest.raises(ConfirmationRequiredError):
        confirm_candidate(candidate, confirmation)


def test_confirmation_is_bound_to_the_candidate() -> None:
    candidate = ActionCandidate.commitment("commitment-1", "Reply to Li")

    with pytest.raises(ConfirmationMismatchError):
        confirm_candidate(candidate, _confirmation("commitment-2"))


def test_audit_timestamps_and_supplied_deadlines_must_be_timezone_aware() -> None:
    naive = datetime(2026, 8, 23, 12, 0)

    with pytest.raises(InvalidActionCandidateError):
        UserConfirmation(candidate_id="commitment-1", confirmed_at=naive)
    with pytest.raises(InvalidActionCandidateError):
        ActionCandidate.commitment("commitment-1", "Reply to Li", deadline=naive)


@pytest.mark.parametrize(
    ("explicit", "actor"),
    [
        (cast(bool, 1), ConfirmationActor.USER),
        (True, cast(ConfirmationActor, "user")),
    ],
    ids=["truthy-non-bool-explicit", "raw-string-actor"],
)
def test_confirmation_control_fields_require_exact_runtime_types(
    explicit: bool,
    actor: ConfirmationActor,
) -> None:
    with pytest.raises(InvalidActionCandidateError):
        UserConfirmation(
            candidate_id="commitment-1",
            confirmed_at=NOW,
            explicit=explicit,
            actor=actor,
        )


def test_action_intent_rejects_raw_string_at_runtime() -> None:
    with pytest.raises(InvalidActionCandidateError):
        ActionCandidate(
            candidate_id="commitment-1",
            intent=cast(ActionIntent, "commitment"),
            description="Reply to Li",
        )
