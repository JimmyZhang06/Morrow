"""Pure lifecycle state machine and deterministic verdict reducer."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .enums import LifecycleState, VerdictType
from .exceptions import InvalidLifecycleTransitionError, InvalidVerdictError

ALLOWED_TRANSITIONS: frozenset[tuple[LifecycleState, LifecycleState]] = frozenset(
    {
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
)

TERMINAL_STATES = frozenset({LifecycleState.SUPERSEDED, LifecycleState.RETRACTED})


class VerdictEventLike(Protocol):
    id: uuid.UUID
    sequence_no: int
    verdict: VerdictType
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReducedVerdict:
    lifecycle_state: LifecycleState
    current_verdict: VerdictType | None
    last_decisive_verdict: VerdictType | None
    last_sequence_no: int

    @property
    def user_confirmed(self) -> bool:
        return self.last_decisive_verdict is VerdictType.CONFIRM


def transition_lifecycle(current: LifecycleState, target: LifecycleState) -> LifecycleState:
    """Apply the explicit public state machine; self-transitions are not transitions."""

    if (current, target) not in ALLOWED_TRANSITIONS:
        raise InvalidLifecycleTransitionError(
            f"Lifecycle transition {current.value} -> {target.value} is not allowed"
        )
    return target


def replacement_transition(current: LifecycleState) -> LifecycleState:
    """Supersede a version during correction without widening the public state machine."""

    if current in TERMINAL_STATES:
        raise InvalidLifecycleTransitionError(
            f"Terminal lifecycle state {current.value} cannot be replaced in place"
        )
    return LifecycleState.SUPERSEDED


def reduce_verdicts(
    initial_state: LifecycleState,
    events: Iterable[VerdictEventLike],
    *,
    can_activate: bool = True,
) -> ReducedVerdict:
    """Reduce an append-only stream in its authoritative monotonic sequence order.

    `can_activate` is a deterministic policy input. It lets user confirmation be
    recorded while a high-risk hypothesis remains candidate until its independent
    evidence gate is also satisfied.
    """

    ordered = sorted(events, key=lambda event: (event.sequence_no, event.created_at, str(event.id)))
    seen_sequences: set[int] = set()
    state = initial_state
    current_verdict: VerdictType | None = None
    last_decisive: VerdictType | None = None
    last_sequence = 0

    for event in ordered:
        if event.sequence_no <= 0 or event.sequence_no in seen_sequences:
            raise InvalidVerdictError("Verdict sequence numbers must be unique and positive")
        if event.sequence_no <= last_sequence:
            raise InvalidVerdictError("Verdict sequence numbers must increase monotonically")
        if state in TERMINAL_STATES:
            raise InvalidVerdictError(
                f"Verdict {event.verdict.value} cannot target terminal state {state.value}"
            )

        seen_sequences.add(event.sequence_no)
        last_sequence = event.sequence_no
        current_verdict = event.verdict

        if event.verdict is VerdictType.SNOOZE:
            continue
        if event.verdict is VerdictType.CONFIRM:
            last_decisive = event.verdict
            if can_activate and state in {LifecycleState.CANDIDATE, LifecycleState.DISPUTED}:
                state = transition_lifecycle(state, LifecycleState.ACTIVE)
            continue
        if event.verdict is VerdictType.REJECT:
            last_decisive = event.verdict
            if state in {LifecycleState.CANDIDATE, LifecycleState.ACTIVE}:
                state = transition_lifecycle(state, LifecycleState.DISPUTED)
            continue
        if event.verdict is VerdictType.RETRACT:
            last_decisive = event.verdict
            state = transition_lifecycle(state, LifecycleState.RETRACTED)
            continue
        if event.verdict is VerdictType.CORRECT:
            last_decisive = event.verdict
            state = replacement_transition(state)
            continue
        raise InvalidVerdictError(f"Unknown verdict {event.verdict!r}")

    return ReducedVerdict(
        lifecycle_state=state,
        current_verdict=current_verdict,
        last_decisive_verdict=last_decisive,
        last_sequence_no=last_sequence,
    )
