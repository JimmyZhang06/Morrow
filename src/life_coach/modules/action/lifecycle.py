"""Deterministic lifecycle for one local, reversible micro-experiment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ReversibleActionError(ValueError):
    """Base class for safe, expected action-loop failures."""


class ActionNotFoundError(ReversibleActionError):
    """The action is absent or unavailable in the current Vault."""


class MemoryNotEligibleForActionError(ReversibleActionError):
    """The current Memory has not been confirmed or corrected by the user."""


class ActionGenerationUnavailableError(ReversibleActionError):
    """The governed model could not produce a durable reversible action."""


class ActionAuthenticationRequiredError(ReversibleActionError):
    """The action command did not authenticate."""


class ActionVaultUnavailableError(ReversibleActionError):
    """The requested Vault is not available to the principal."""


class ActionRevisionConflictError(ReversibleActionError):
    """The caller did not target the current action revision."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("action revision is no longer current")
        self.current_revision = current_revision


class ActionIdempotencyConflictError(ReversibleActionError):
    """An idempotency key was reused for a different command."""


class InvalidActionTransitionError(ReversibleActionError):
    """The requested verdict cannot be applied to the current state."""


class ReversibleActionState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REVOKED = "revoked"


class ReversibleActionVerdict(StrEnum):
    ACCEPT = "accept"
    COMPLETE = "complete"
    REVOKE = "revoke"


_TRANSITIONS: dict[tuple[ReversibleActionState, ReversibleActionVerdict], ReversibleActionState] = {
    (ReversibleActionState.PROPOSED, ReversibleActionVerdict.ACCEPT): (
        ReversibleActionState.ACCEPTED
    ),
    (ReversibleActionState.ACCEPTED, ReversibleActionVerdict.COMPLETE): (
        ReversibleActionState.COMPLETED
    ),
    (ReversibleActionState.PROPOSED, ReversibleActionVerdict.REVOKE): (
        ReversibleActionState.REVOKED
    ),
    (ReversibleActionState.ACCEPTED, ReversibleActionVerdict.REVOKE): (
        ReversibleActionState.REVOKED
    ),
    (ReversibleActionState.COMPLETED, ReversibleActionVerdict.REVOKE): (
        ReversibleActionState.REVOKED
    ),
}


def transition_action(
    current: ReversibleActionState,
    verdict: ReversibleActionVerdict,
) -> ReversibleActionState:
    """Apply the closed transition table; no transition has an external effect."""

    try:
        return _TRANSITIONS[(current, verdict)]
    except KeyError:
        raise InvalidActionTransitionError(
            f"{verdict.value} cannot be applied to {current.value}"
        ) from None


@dataclass(frozen=True, slots=True)
class ReversibleActionView:
    action_id: UUID
    memory_id: UUID
    source_derived_object_id: UUID
    state: ReversibleActionState
    revision: int
    kind: str
    title: str
    description: str
    rationale: str
    exit_plan: str
    estimated_minutes: int
    is_reversible: bool
    template_version: str
    created_at: datetime
    updated_at: datetime
    model_run_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReversibleActionPage:
    items: tuple[ReversibleActionView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ReversibleActionVerdictOutcome:
    verdict_id: UUID
    action: ReversibleActionView


__all__ = [
    "ActionAuthenticationRequiredError",
    "ActionGenerationUnavailableError",
    "ActionIdempotencyConflictError",
    "ActionNotFoundError",
    "ActionRevisionConflictError",
    "ActionVaultUnavailableError",
    "InvalidActionTransitionError",
    "MemoryNotEligibleForActionError",
    "ReversibleActionError",
    "ReversibleActionPage",
    "ReversibleActionState",
    "ReversibleActionVerdict",
    "ReversibleActionVerdictOutcome",
    "ReversibleActionView",
    "transition_action",
]
