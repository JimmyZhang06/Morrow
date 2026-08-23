from __future__ import annotations

from datetime import timedelta

import pytest

from life_coach.jobs.contracts import FenceSnapshot, FenceViolation
from life_coach.jobs.enums import (
    FailureClass,
    FenceCheckpoint,
    JobState,
    OutboundNextAction,
    OutboundOperationState,
    ReconciliationOutcome,
)
from life_coach.jobs.retry import full_jitter_delay, retry_decision
from life_coach.jobs.state_machine import (
    InvalidStateTransition,
    assert_job_transition,
    assert_outbound_transition,
    check_execution_fence,
    outbound_next_action,
    reconciliation_target,
)


def test_job_state_vocabulary_and_terminal_states() -> None:
    assert {state.value for state in JobState} == {
        "queued",
        "running",
        "waiting",
        "retrying",
        "done",
        "dead",
        "canceled",
    }
    for terminal in (JobState.DONE, JobState.DEAD, JobState.CANCELED):
        with pytest.raises(InvalidStateTransition):
            assert_job_transition(terminal, JobState.RUNNING)


def test_waiting_requires_explicit_resume_instead_of_automatic_retry() -> None:
    assert_job_transition(JobState.WAITING, JobState.QUEUED)
    with pytest.raises(InvalidStateTransition):
        assert_job_transition(JobState.WAITING, JobState.RUNNING)


@pytest.mark.parametrize("checkpoint", list(FenceCheckpoint))
@pytest.mark.parametrize(
    "current,reason",
    [
        (FenceSnapshot(8, 3), "policy_epoch_changed"),
        (FenceSnapshot(7, 4), "source_generation_changed"),
        (FenceSnapshot(7, 3, tombstoned=True), "source_tombstoned"),
    ],
)
def test_policy_source_and_tombstone_fences_apply_at_all_three_checkpoints(
    checkpoint: FenceCheckpoint, current: FenceSnapshot, reason: str
) -> None:
    expected = FenceSnapshot(policy_epoch=7, source_generation=3)

    with pytest.raises(FenceViolation) as exc_info:
        check_execution_fence(expected, current, checkpoint)

    assert exc_info.value.checkpoint is checkpoint
    assert exc_info.value.reason == reason


def test_matching_fence_allows_execution() -> None:
    snapshot = FenceSnapshot(policy_epoch=7, source_generation=3)
    for checkpoint in FenceCheckpoint:
        check_execution_fence(snapshot, snapshot, checkpoint)


@pytest.mark.parametrize(
    ("attempt", "sample", "expected_seconds"),
    [(1, 0.0, 0.0), (3, 0.5, 2.0), (10, 1.0, 10.0)],
)
def test_full_jitter_is_bounded_and_capped(
    attempt: int, sample: float, expected_seconds: float
) -> None:
    delay = full_jitter_delay(
        attempt=attempt,
        base_seconds=1,
        cap_seconds=10,
        random_sample=lambda: sample,
    )

    assert delay == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize("failure", [FailureClass.TRANSIENT, FailureClass.RATE_LIMITED])
def test_retryable_failure_uses_jitter(failure: FailureClass) -> None:
    decision = retry_decision(
        failure,
        attempts=2,
        max_attempts=5,
        random_sample=lambda: 0.25,
    )

    assert decision.target_state is JobState.RETRYING
    assert decision.delay == timedelta(seconds=0.5)


def test_deterministic_and_exhausted_failures_are_dead() -> None:
    deterministic = retry_decision(
        FailureClass.DETERMINISTIC,
        attempts=1,
        max_attempts=5,
        random_sample=lambda: 0.5,
    )
    exhausted = retry_decision(
        FailureClass.TRANSIENT,
        attempts=5,
        max_attempts=5,
        random_sample=lambda: 0.5,
    )

    assert deterministic.target_state is JobState.DEAD
    assert exhausted.target_state is JobState.DEAD


def test_unknown_external_outcome_requires_reconciliation_not_retry() -> None:
    decision = retry_decision(
        FailureClass.EXTERNAL_OUTCOME_UNKNOWN,
        attempts=1,
        max_attempts=5,
        random_sample=lambda: 0.5,
    )

    assert decision.target_state is JobState.WAITING
    assert decision.delay is None
    assert decision.requires_reconciliation
    assert outbound_next_action(OutboundOperationState.UNKNOWN) is OutboundNextAction.RECONCILE
    with pytest.raises(InvalidStateTransition):
        assert_outbound_transition(OutboundOperationState.UNKNOWN, OutboundOperationState.EXECUTING)


@pytest.mark.parametrize(
    ("outcome", "target"),
    [
        (ReconciliationOutcome.FOUND, OutboundOperationState.SUCCEEDED),
        (ReconciliationOutcome.CONFIRMED_NOT_FOUND, OutboundOperationState.PENDING),
        (ReconciliationOutcome.STILL_UNKNOWN, OutboundOperationState.UNKNOWN),
        (ReconciliationOutcome.PERMANENT_FAILURE, OutboundOperationState.FAILED),
    ],
)
def test_reconciliation_is_the_only_path_back_to_pending(
    outcome: ReconciliationOutcome, target: OutboundOperationState
) -> None:
    assert reconciliation_target(outcome) is target
    assert_outbound_transition(OutboundOperationState.RECONCILING, target)
