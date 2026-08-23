from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import pytest

from life_coach.modules.action import (
    ActionCandidate,
    ActionIntent,
    ActionSafetyNotCurrentError,
    ActionSafetyOutcome,
    ActionSafetyRequiredError,
    ActionSafetySubjectMismatchError,
    CandidateState,
    ConfirmationActor,
    ConfirmationMismatchError,
    ConfirmationNotCurrentError,
    ConfirmationRequiredError,
    IntentCannotBecomeTaskError,
    InvalidActionCandidateError,
    TaskState,
    UserConfirmation,
    confirm_candidate,
    to_task,
)
from tests.action.helpers import (
    CONTEXT,
    NOW,
    VALID_UNTIL,
    allowed_action,
    confirmation_for_action,
    verdict_for_action,
)


def _commitment(description: str = "Reply to Li") -> ActionCandidate:
    return ActionCandidate.commitment("commitment-1", description, **CONTEXT)


def test_intent_types_are_distinct_and_all_start_non_executable() -> None:
    candidates = (
        _commitment(),
        ActionCandidate.wish("wish-1", "I wish I had time to learn piano", **CONTEXT),
        ActionCandidate.concern("concern-1", "The clutter is bothering me", **CONTEXT),
        ActionCandidate.idea("idea-1", "Maybe I could make a podcast", **CONTEXT),
        ActionCandidate.experiment_candidate(
            "experiment-1",
            "Put the phone away before bed for three days",
            rationale="See whether bedtime feels less rushed",
            cost="Three evenings",
            exit_plan="Stop whenever it is not useful",
            is_reversible=True,
            **CONTEXT,
        ),
        ActionCandidate.external_action_candidate(
            "external-1",
            "Create a calendar event",
            scope="calendar.events.create",
            payload={"title": "Call Li"},
            **CONTEXT,
        ),
    )

    assert {candidate.intent for candidate in candidates} == set(ActionIntent)
    assert all(candidate.state is CandidateState.CANDIDATE for candidate in candidates)
    assert all(not candidate.is_executable for candidate in candidates)


@pytest.mark.parametrize("intent", [ActionIntent.WISH, ActionIntent.CONCERN, ActionIntent.IDEA])
@pytest.mark.parametrize("field", ["deadline", "priority"])
def test_non_actionable_intents_forbid_deadline_and_priority(
    intent: ActionIntent,
    field: str,
) -> None:
    values: dict[str, object] = {
        "candidate_id": "non-action-1",
        "intent": intent,
        "description": "This is not a commitment",
        **CONTEXT,
    }
    values[field] = NOW if field == "deadline" else "high"

    with pytest.raises(InvalidActionCandidateError, match="deadline or priority"):
        ActionCandidate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory", [ActionCandidate.wish, ActionCandidate.concern, ActionCandidate.idea]
)
def test_non_commitment_never_directly_becomes_todo(factory: object) -> None:
    candidate = factory("candidate-1", "Not a commitment", **CONTEXT)  # type: ignore[operator]
    confirmed = confirm_candidate(candidate, confirmation_for_action(candidate), at=NOW)

    with pytest.raises(IntentCannotBecomeTaskError):
        to_task(confirmed, at=NOW)


def test_unconfirmed_commitment_does_not_create_task() -> None:
    with pytest.raises(ConfirmationRequiredError):
        to_task(allowed_action(_commitment()), at=NOW)


def test_final_commitment_to_task_fails_closed_without_verdict() -> None:
    candidate = _commitment()
    confirmed = confirm_candidate(candidate, confirmation_for_action(candidate), at=NOW)
    with pytest.raises(ActionSafetyRequiredError):
        to_task(confirmed, at=NOW)


def test_commitment_requires_current_exact_allowed_verdict_and_confirmation() -> None:
    candidate = allowed_action(_commitment())
    confirmed = confirm_candidate(candidate, confirmation_for_action(candidate), at=NOW)

    task = to_task(confirmed, at=NOW, task_id="task-1")

    assert task.state is TaskState.OPEN
    assert task.description == "Reply to Li"
    assert task.source_candidate_id == "commitment-1"
    assert task.deadline is None
    assert task.priority is None


def test_same_candidate_id_with_changed_content_rejects_old_confirmation() -> None:
    original = allowed_action(_commitment("Reply to Li"))
    confirmation = confirmation_for_action(original)
    changed = _commitment("Reply to someone else").with_safety_verdict(
        verdict_for_action(_commitment("Reply to someone else"))
    )

    with pytest.raises(ConfirmationMismatchError):
        confirm_candidate(changed, confirmation, at=NOW)


def test_same_candidate_id_with_changed_content_rejects_old_verdict() -> None:
    original = _commitment("Reply to Li")
    changed = _commitment("Reply to someone else")

    with pytest.raises(ActionSafetySubjectMismatchError):
        changed.with_safety_verdict(verdict_for_action(original))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vault_id", "vault-2"),
        ("principal_id", "user-2"),
        ("purpose", "different-purpose"),
        ("policy_version", "action-policy-v1"),
        ("policy_snapshot", "different-snapshot"),
    ],
)
def test_confirmation_context_and_policy_binding_is_exact(field: str, value: str) -> None:
    candidate = allowed_action(_commitment())
    confirmation = confirmation_for_action(candidate, **{field: value})

    with pytest.raises(ConfirmationMismatchError):
        confirm_candidate(candidate, confirmation, at=NOW)


def test_confirmation_and_verdict_are_rechecked_when_task_is_created() -> None:
    candidate = allowed_action(_commitment())
    confirmed = confirm_candidate(candidate, confirmation_for_action(candidate), at=NOW)

    with pytest.raises(ConfirmationNotCurrentError):
        to_task(confirmed, at=VALID_UNTIL)

    short_verdict = verdict_for_action(candidate, expires_at=NOW + timedelta(seconds=1))
    short_lived = candidate.with_safety_verdict(short_verdict)
    confirmed = confirm_candidate(short_lived, confirmation_for_action(short_lived), at=NOW)
    with pytest.raises(ActionSafetyNotCurrentError):
        to_task(confirmed, at=NOW + timedelta(seconds=1))


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
    candidate = allowed_action(_commitment())
    confirmation = confirmation_for_action(candidate, explicit=explicit, actor=actor)

    with pytest.raises(ConfirmationRequiredError):
        confirm_candidate(candidate, confirmation, at=NOW)


def test_user_confirmation_has_no_approval_defaults() -> None:
    assert UserConfirmation.__dataclass_fields__["explicit"].default is not True
    assert UserConfirmation.__dataclass_fields__["actor"].default is not ConfirmationActor.USER


def test_confirmation_constructor_requires_explicit_and_actor() -> None:
    candidate = _commitment()
    with pytest.raises(TypeError):
        UserConfirmation(  # type: ignore[call-arg]
            confirmation_id="confirmation-1",
            generation=1,
            candidate_id=candidate.candidate_id,
            vault_id=candidate.vault_id,
            principal_id=candidate.principal_id,
            purpose=candidate.purpose,
            candidate_fingerprint=candidate.fingerprint,
            policy_version=candidate.policy_version,
            policy_snapshot=candidate.policy_snapshot,
            confirmed_at=NOW,
            expires_at=VALID_UNTIL,
        )


def test_audit_timestamps_and_deadlines_must_be_timezone_aware() -> None:
    naive = datetime(2026, 8, 23, 12, 0)
    candidate = _commitment()

    with pytest.raises(InvalidActionCandidateError):
        confirmation_for_action(candidate, confirmed_at=naive)
    with pytest.raises(InvalidActionCandidateError):
        ActionCandidate.commitment("commitment-1", "Reply", deadline=naive, **CONTEXT)


@pytest.mark.parametrize(
    ("explicit", "actor"),
    [(cast(bool, 1), ConfirmationActor.USER), (True, cast(ConfirmationActor, "user"))],
)
def test_confirmation_control_fields_require_exact_runtime_types(
    explicit: bool,
    actor: ConfirmationActor,
) -> None:
    with pytest.raises(InvalidActionCandidateError):
        confirmation_for_action(_commitment(), explicit=explicit, actor=actor)


def test_action_intent_rejects_raw_string_at_runtime() -> None:
    candidate = _commitment()
    with pytest.raises(InvalidActionCandidateError):
        replace(candidate, intent=cast(ActionIntent, "commitment"))


def test_action_safety_outcomes_remain_non_scored() -> None:
    assert {outcome.value for outcome in ActionSafetyOutcome} == {"allowed", "blocked"}
    assert "risk_score" not in verdict_for_action(_commitment()).__dataclass_fields__
