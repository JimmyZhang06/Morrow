"""Short-lived safety state and deterministic routing interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ._time import require_aware
from ._validation import (
    require_bool,
    require_optional_bool,
    require_optional_string,
    require_string,
)


class SafetyRoute(StrEnum):
    """Mutually exclusive states in the safety routing machine."""

    ORDINARY_COACH = "ordinary_coach"
    CLARIFY_IMMEDIATE_SAFETY = "clarify_immediate_safety"
    MEDICAL_EMERGENCY = "medical_emergency"
    IMMEDIATE_DANGER = "immediate_danger"
    ONGOING_HUMAN_SUPPORT = "ongoing_human_support"
    USER_DECLINED_CLARIFICATION = "user_declined_clarification"


class SafetyPriority(StrEnum):
    """Operational priority without predictive risk tiers or scores."""

    ORDINARY = "ordinary"
    SAFETY = "safety"
    MEDICAL_FIRST = "medical_first"


class ExpiredSafetyStateError(ValueError):
    """Raised when a caller tries to route using expired short-lived details."""


class InactiveSafetyStateError(ValueError):
    """Raised when a caller evaluates state before its creation instant."""


@dataclass(frozen=True, slots=True)
class SafetyState:
    """Current-session facts used only for immediate routing.

    Values are explicit user/current-operation observations, not predictions.
    ``None`` means that the question has not been answered.  Persistence layers
    must expire and delete this detailed state rather than place it in profiles,
    embeddings, memoirs, or recommendation features.
    """

    session_id: str
    created_at: datetime
    expires_at: datetime
    harm_already_occurred: bool | None = None
    possible_medical_emergency: bool | None = None
    possible_current_danger: bool = False
    immediate_danger: bool | None = None
    current_intent: bool | None = None
    plan_present: bool | None = None
    accessible_means: bool | None = None
    imminent_timeframe: bool | None = None
    ongoing_distress: bool = False
    ongoing_safety_concern: bool = False
    user_declined_clarification: bool = False
    abuse_or_coercive_control_context: bool = False
    supporter_selected_by_user: bool = False
    supporter_confirmed_safe: bool = False
    contacting_supporter_feasible: bool | None = None
    country: str | None = None
    region: str | None = None

    def __post_init__(self) -> None:
        require_string(self.session_id, field_name="session_id")
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        require_aware(self.created_at, field_name="created_at")
        require_aware(self.expires_at, field_name="expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        for field_name in (
            "possible_current_danger",
            "ongoing_distress",
            "ongoing_safety_concern",
            "user_declined_clarification",
            "abuse_or_coercive_control_context",
            "supporter_selected_by_user",
            "supporter_confirmed_safe",
        ):
            require_bool(getattr(self, field_name), field_name=field_name)
        for field_name in (
            "harm_already_occurred",
            "possible_medical_emergency",
            "immediate_danger",
            "current_intent",
            "plan_present",
            "accessible_means",
            "imminent_timeframe",
            "contacting_supporter_feasible",
        ):
            require_optional_bool(getattr(self, field_name), field_name=field_name)
        require_optional_string(self.country, field_name="country")
        require_optional_string(self.region, field_name="region")
        if self.supporter_confirmed_safe and not self.supporter_selected_by_user:
            raise ValueError("a safe supporter must first be selected by the user")
        if self.contacting_supporter_feasible is not None and not self.supporter_selected_by_user:
            raise ValueError("supporter feasibility requires a user-selected supporter")

    def is_expired(self, *, at: datetime) -> bool:
        """Treat the state as expired at exactly ``expires_at``."""

        require_aware(at, field_name="at")
        return at >= self.expires_at

    @property
    def can_suggest_selected_supporter(self) -> bool:
        """Never assume family or another contact is safe.

        Abuse/coercive-control context does not change the rule: a contact must
        be user-selected and explicitly confirmed safe in the current context.
        """

        return (
            self.supporter_selected_by_user
            and self.supporter_confirmed_safe
            and self.contacting_supporter_feasible is True
        )


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Side-effect-free routing result from a safety gateway."""

    route: SafetyRoute
    priority: SafetyPriority
    may_suggest_selected_supporter: bool
    evaluated_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        require_aware(self.evaluated_at, field_name="evaluated_at")
        require_aware(self.valid_until, field_name="valid_until")
        if self.valid_until <= self.evaluated_at:
            raise ValueError("valid_until must be after evaluated_at")

    def is_current(self, *, at: datetime) -> bool:
        """Return whether ``at`` is in the half-open decision validity window."""

        require_aware(at, field_name="at")
        return self.evaluated_at <= at < self.valid_until

    @property
    def ordinary_coach_allowed(self) -> bool:
        """Ordinary analysis runs only in the explicit ordinary state."""

        return self.route is SafetyRoute.ORDINARY_COACH

    @property
    def ordinary_coach_paused(self) -> bool:
        """Whether reflection, memoir, and life analysis must pause."""

        return not self.ordinary_coach_allowed

    @property
    def possible_medical_emergency_takes_priority(self) -> bool:
        """Whether the response must direct to immediate medical help first."""

        return self.route is SafetyRoute.MEDICAL_EMERGENCY

    @property
    def may_continue_psychological_questionnaire(self) -> bool:
        """Do not delay possible medical emergencies with more questions."""

        return self.route is not SafetyRoute.MEDICAL_EMERGENCY

    @property
    def automatically_contacts_third_party(self) -> bool:
        """The consumer product never performs an automatic third-party contact."""

        return False


@runtime_checkable
class SafetyGateway(Protocol):
    """Interface that keeps safety routing separate from ordinary coaching."""

    def evaluate(self, state: SafetyState, *, at: datetime) -> SafetyDecision:
        """Route the current short-lived state without calculating a score."""


class RuleBasedSafetyGateway:
    """Deterministic implementation over explicitly supplied current facts."""

    def evaluate(self, state: SafetyState, *, at: datetime) -> SafetyDecision:
        require_aware(at, field_name="at")
        if at < state.created_at:
            raise InactiveSafetyStateError("SafetyState cannot be evaluated before created_at")
        if state.is_expired(at=at):
            raise ExpiredSafetyStateError("expired SafetyState must be discarded and recollected")

        can_suggest_supporter = state.can_suggest_selected_supporter
        if state.possible_medical_emergency is True or (
            state.harm_already_occurred is True and state.possible_medical_emergency is not False
        ):
            return SafetyDecision(
                SafetyRoute.MEDICAL_EMERGENCY,
                SafetyPriority.MEDICAL_FIRST,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        if state.harm_already_occurred is True:
            return SafetyDecision(
                SafetyRoute.IMMEDIATE_DANGER,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        if state.immediate_danger is True or self._has_confirmed_imminent_danger(state):
            return SafetyDecision(
                SafetyRoute.IMMEDIATE_DANGER,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        if state.user_declined_clarification:
            return SafetyDecision(
                SafetyRoute.USER_DECLINED_CLARIFICATION,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        if state.possible_current_danger and state.immediate_danger is not False:
            return SafetyDecision(
                SafetyRoute.CLARIFY_IMMEDIATE_SAFETY,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        if self._current_concern_needs_immediacy_clarification(state):
            return SafetyDecision(
                SafetyRoute.CLARIFY_IMMEDIATE_SAFETY,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        if self._has_non_imminent_current_concern(state) or state.ongoing_safety_concern:
            return SafetyDecision(
                SafetyRoute.ONGOING_HUMAN_SUPPORT,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
                evaluated_at=at,
                valid_until=state.expires_at,
            )
        return SafetyDecision(
            SafetyRoute.ORDINARY_COACH,
            SafetyPriority.ORDINARY,
            False,
            evaluated_at=at,
            valid_until=state.expires_at,
        )

    @staticmethod
    def _has_confirmed_imminent_danger(state: SafetyState) -> bool:
        return state.imminent_timeframe is True and any(
            value is True
            for value in (state.current_intent, state.plan_present, state.accessible_means)
        )

    @classmethod
    def _current_concern_needs_immediacy_clarification(cls, state: SafetyState) -> bool:
        """Do not downgrade a current signal while immediacy remains unknown."""

        return cls._has_non_imminent_current_concern(state) and not (
            state.immediate_danger is False or state.imminent_timeframe is False
        )

    @staticmethod
    def _has_non_imminent_current_concern(state: SafetyState) -> bool:
        return any(
            value is True
            for value in (state.current_intent, state.plan_present, state.accessible_means)
        )
