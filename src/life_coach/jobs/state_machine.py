"""Pure, exhaustively testable state and execution-fence rules."""

from __future__ import annotations

from collections.abc import Mapping

from life_coach.jobs.contracts import FenceSnapshot, FenceViolation
from life_coach.jobs.enums import (
    FenceCheckpoint,
    JobState,
    OutboundNextAction,
    OutboundOperationState,
    ReconciliationOutcome,
)


class InvalidStateTransition(RuntimeError):
    pass


JOB_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELED}),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING,
            JobState.RETRYING,
            JobState.DONE,
            JobState.DEAD,
            JobState.CANCELED,
        }
    ),
    JobState.WAITING: frozenset({JobState.QUEUED, JobState.DEAD, JobState.CANCELED}),
    JobState.RETRYING: frozenset({JobState.RUNNING, JobState.DEAD, JobState.CANCELED}),
    JobState.DONE: frozenset(),
    JobState.DEAD: frozenset(),
    JobState.CANCELED: frozenset(),
}

OUTBOUND_TRANSITIONS: Mapping[OutboundOperationState, frozenset[OutboundOperationState]] = {
    OutboundOperationState.PENDING: frozenset(
        {OutboundOperationState.EXECUTING, OutboundOperationState.CANCELED}
    ),
    OutboundOperationState.EXECUTING: frozenset(
        {
            OutboundOperationState.UNKNOWN,
            OutboundOperationState.SUCCEEDED,
            OutboundOperationState.FAILED,
            OutboundOperationState.CANCELED,
        }
    ),
    OutboundOperationState.UNKNOWN: frozenset(
        {OutboundOperationState.RECONCILING, OutboundOperationState.CANCELED}
    ),
    OutboundOperationState.RECONCILING: frozenset(
        {
            OutboundOperationState.UNKNOWN,
            OutboundOperationState.PENDING,
            OutboundOperationState.SUCCEEDED,
            OutboundOperationState.FAILED,
            OutboundOperationState.CANCELED,
        }
    ),
    OutboundOperationState.SUCCEEDED: frozenset(),
    OutboundOperationState.FAILED: frozenset(),
    OutboundOperationState.CANCELED: frozenset(),
}


def assert_job_transition(current: JobState, target: JobState) -> None:
    if target not in JOB_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"job cannot transition from {current.value} to {target.value}"
        )


def assert_outbound_transition(
    current: OutboundOperationState, target: OutboundOperationState
) -> None:
    if target not in OUTBOUND_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"outbound operation cannot transition from {current.value} to {target.value}"
        )


def outbound_next_action(state: OutboundOperationState) -> OutboundNextAction:
    return {
        OutboundOperationState.PENDING: OutboundNextAction.EXECUTE,
        OutboundOperationState.EXECUTING: OutboundNextAction.AWAIT_RESULT,
        OutboundOperationState.UNKNOWN: OutboundNextAction.RECONCILE,
        OutboundOperationState.RECONCILING: OutboundNextAction.QUERY_PROVIDER,
        OutboundOperationState.SUCCEEDED: OutboundNextAction.NONE,
        OutboundOperationState.FAILED: OutboundNextAction.NONE,
        OutboundOperationState.CANCELED: OutboundNextAction.NONE,
    }[state]


def reconciliation_target(outcome: ReconciliationOutcome) -> OutboundOperationState:
    return {
        ReconciliationOutcome.FOUND: OutboundOperationState.SUCCEEDED,
        ReconciliationOutcome.CONFIRMED_NOT_FOUND: OutboundOperationState.PENDING,
        ReconciliationOutcome.STILL_UNKNOWN: OutboundOperationState.UNKNOWN,
        ReconciliationOutcome.PERMANENT_FAILURE: OutboundOperationState.FAILED,
    }[outcome]


def check_execution_fence(
    expected: FenceSnapshot,
    current: FenceSnapshot,
    checkpoint: FenceCheckpoint,
) -> None:
    """Reject stale work at each required privacy checkpoint."""

    if current.tombstoned:
        raise FenceViolation(checkpoint, "source_tombstoned")
    if current.policy_epoch != expected.policy_epoch:
        raise FenceViolation(checkpoint, "policy_epoch_changed")
    if current.source_generation != expected.source_generation:
        raise FenceViolation(checkpoint, "source_generation_changed")
