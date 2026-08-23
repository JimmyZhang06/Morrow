from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.knowledge.enums import LifecycleState, VerdictType
from life_coach.modules.knowledge.exceptions import InvalidLifecycleTransitionError
from life_coach.modules.knowledge.reducer import (
    ALLOWED_TRANSITIONS,
    reduce_verdicts,
    transition_lifecycle,
)


@dataclass(frozen=True)
class Event:
    id: uuid.UUID
    sequence_no: int
    verdict: VerdictType
    created_at: datetime


def event(sequence: int, verdict: VerdictType) -> Event:
    return Event(
        id=uuid.UUID(int=sequence),
        sequence_no=sequence,
        verdict=verdict,
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=sequence),
    )


def test_allowed_lifecycle_transitions_are_exact() -> None:
    expected = {
        (LifecycleState.CANDIDATE, LifecycleState.ACTIVE),
        (LifecycleState.CANDIDATE, LifecycleState.DISPUTED),
        (LifecycleState.CANDIDATE, LifecycleState.RETRACTED),
        (LifecycleState.ACTIVE, LifecycleState.DISPUTED),
        (LifecycleState.ACTIVE, LifecycleState.SUPERSEDED),
        (LifecycleState.ACTIVE, LifecycleState.RETRACTED),
        (LifecycleState.DISPUTED, LifecycleState.ACTIVE),
        (LifecycleState.DISPUTED, LifecycleState.SUPERSEDED),
        (LifecycleState.DISPUTED, LifecycleState.RETRACTED),
    }
    assert expected == ALLOWED_TRANSITIONS
    for current, target in expected:
        assert transition_lifecycle(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in LifecycleState
        for target in LifecycleState
        if (current, target) not in ALLOWED_TRANSITIONS
    ],
)
def test_every_other_lifecycle_transition_is_illegal(
    current: LifecycleState, target: LifecycleState
) -> None:
    with pytest.raises(InvalidLifecycleTransitionError):
        transition_lifecycle(current, target)


def test_reducer_is_order_independent_because_sequence_is_authoritative() -> None:
    events = [
        event(1, VerdictType.SNOOZE),
        event(2, VerdictType.CONFIRM),
        event(3, VerdictType.REJECT),
    ]
    expected = reduce_verdicts(LifecycleState.CANDIDATE, events)
    random.Random(42).shuffle(events)

    actual = reduce_verdicts(LifecycleState.CANDIDATE, events)

    assert actual == expected
    assert actual.lifecycle_state is LifecycleState.DISPUTED
    assert actual.current_verdict is VerdictType.REJECT
    assert actual.last_sequence_no == 3


def test_confirmation_can_be_recorded_while_policy_blocks_activation() -> None:
    result = reduce_verdicts(
        LifecycleState.CANDIDATE,
        [event(1, VerdictType.CONFIRM)],
        can_activate=False,
    )

    assert result.lifecycle_state is LifecycleState.CANDIDATE
    assert result.user_confirmed is True


def test_correction_uses_replacement_path_without_widening_public_machine() -> None:
    with pytest.raises(InvalidLifecycleTransitionError):
        transition_lifecycle(LifecycleState.CANDIDATE, LifecycleState.SUPERSEDED)

    result = reduce_verdicts(
        LifecycleState.CANDIDATE,
        [event(1, VerdictType.CORRECT)],
    )

    assert result.lifecycle_state is LifecycleState.SUPERSEDED


@pytest.mark.parametrize("terminal", [LifecycleState.SUPERSEDED, LifecycleState.RETRACTED])
def test_terminal_state_cannot_be_revived_by_verdict(terminal: LifecycleState) -> None:
    with pytest.raises(Exception, match="terminal state"):
        reduce_verdicts(terminal, [event(1, VerdictType.CONFIRM)])
