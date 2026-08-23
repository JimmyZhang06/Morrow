from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.action import (
    ActionCandidate,
    ConfirmationActor,
    ConfirmationMismatchError,
    ConfirmationRequiredError,
    ExternalActionAlreadyConsumedError,
    ExternalActionConfirmation,
    ExternalActionExecutionTimeError,
    ExternalActionState,
    InvalidActionCandidateError,
    UserConfirmation,
    authorize_external_action,
    confirm_candidate,
    record_external_action_execution,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _calendar_candidate() -> ActionCandidate:
    return ActionCandidate.external_action_candidate(
        "external-1",
        "Create one calendar event",
        scope="calendar.events.create",
        payload={"title": "Call Li", "starts_at": "2026-08-24T10:00:00+08:00"},
    )


def _confirmation(
    *,
    scope: str = "calendar.events.create",
    title: str = "Call Li",
) -> ExternalActionConfirmation:
    return ExternalActionConfirmation(
        candidate_id="external-1",
        confirmed_at=NOW,
        scope=scope,
        payload={"title": title, "starts_at": "2026-08-24T10:00:00+08:00"},
    )


def test_external_action_is_not_executable_before_exact_confirmation() -> None:
    candidate = _calendar_candidate()

    assert not candidate.is_executable
    with pytest.raises(ConfirmationRequiredError):
        confirm_candidate(
            candidate,
            UserConfirmation(candidate_id=candidate.candidate_id, confirmed_at=NOW),
        )


@pytest.mark.parametrize(
    "confirmation",
    [
        _confirmation(scope="calendar.events.update"),
        _confirmation(title="Call someone else"),
    ],
    ids=["different-scope", "different-payload"],
)
def test_external_action_confirmation_must_match_scope_and_payload(
    confirmation: ExternalActionConfirmation,
) -> None:
    with pytest.raises(ConfirmationMismatchError):
        authorize_external_action(_calendar_candidate(), confirmation)


def test_external_action_becomes_ready_only_after_exact_user_confirmation() -> None:
    authorization = authorize_external_action(_calendar_candidate(), _confirmation())

    assert authorization.state is ExternalActionState.READY
    assert authorization.is_executable
    assert authorization.scope == "calendar.events.create"
    assert authorization.payload == {
        "title": "Call Li",
        "starts_at": "2026-08-24T10:00:00+08:00",
    }


def test_external_payload_is_detached_from_the_callers_mutable_mapping() -> None:
    payload: dict[str, object] = {"title": "Call Li"}
    candidate = ActionCandidate.external_action_candidate(
        "external-1",
        "Create one calendar event",
        scope="calendar.events.create",
        payload=payload,
    )
    payload["title"] = "Changed after proposal"
    confirmation = ExternalActionConfirmation(
        candidate_id="external-1",
        confirmed_at=NOW,
        scope="calendar.events.create",
        payload={"title": "Call Li"},
    )

    authorization = authorize_external_action(candidate, confirmation)

    assert authorization.payload == {"title": "Call Li"}


def test_model_cannot_authorize_an_external_action() -> None:
    confirmation = ExternalActionConfirmation(
        candidate_id="external-1",
        confirmed_at=NOW,
        scope="calendar.events.create",
        payload={"title": "Call Li", "starts_at": "2026-08-24T10:00:00+08:00"},
        actor=ConfirmationActor.MODEL,
    )

    with pytest.raises(ConfirmationRequiredError):
        authorize_external_action(_calendar_candidate(), confirmation)


def test_execution_is_recorded_only_from_authorized_action() -> None:
    authorization = authorize_external_action(_calendar_candidate(), _confirmation())

    execution = record_external_action_execution(
        authorization,
        executed_at=NOW,
        receipt_id="calendar-receipt-1",
    )

    assert execution.state is ExternalActionState.EXECUTED
    assert not execution.is_executable
    assert authorization.state is ExternalActionState.EXECUTED
    assert not authorization.is_executable


def test_external_authorization_is_single_use() -> None:
    authorization = authorize_external_action(_calendar_candidate(), _confirmation())
    record_external_action_execution(
        authorization,
        executed_at=NOW,
        receipt_id="calendar-receipt-1",
    )

    with pytest.raises(ExternalActionAlreadyConsumedError):
        record_external_action_execution(
            authorization,
            executed_at=NOW + timedelta(seconds=1),
            receipt_id="calendar-receipt-2",
        )


def test_execution_cannot_predate_the_exact_user_confirmation() -> None:
    authorization = authorize_external_action(_calendar_candidate(), _confirmation())

    with pytest.raises(ExternalActionExecutionTimeError):
        record_external_action_execution(
            authorization,
            executed_at=NOW - timedelta(microseconds=1),
            receipt_id="calendar-receipt-1",
        )

    assert authorization.state is ExternalActionState.READY
    assert authorization.is_executable


def test_execution_timestamp_must_be_timezone_aware() -> None:
    authorization = authorize_external_action(_calendar_candidate(), _confirmation())

    with pytest.raises(InvalidActionCandidateError):
        record_external_action_execution(
            authorization,
            executed_at=datetime(2026, 8, 23, 12, 0),
            receipt_id="calendar-receipt-1",
        )
