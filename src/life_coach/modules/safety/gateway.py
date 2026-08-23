"""Short-lived safety state and deterministic routing interfaces."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ._time import require_aware
from ._validation import (
    require_bool,
    require_optional_bool,
    require_optional_string,
    require_string,
)
from .authority import (
    SafetyAuthorityPort,
    SafetyAuthorityReceipt,
    SafetyAuthorityVerificationError,
    SafetyBinding,
    SafetyDecisionClaims,
    TrustedClock,
    read_trusted_time,
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

    vault_id: str
    principal_id: str
    session_id: str
    created_at: datetime
    expires_at: datetime
    input_fingerprint: str = field(init=False)
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
        for field_name in ("vault_id", "principal_id", "session_id"):
            require_string(getattr(self, field_name), field_name=field_name)
            normalized = getattr(self, field_name).strip()
            if not normalized:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, normalized)
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
        object.__setattr__(self, "input_fingerprint", self._canonical_input_fingerprint())

    def _canonical_input_fingerprint(self) -> str:
        """Hash every immutable fact that can affect safety routing or support."""

        canonical = json.dumps(
            {
                "schema": "safety-routing-input-v1",
                "vault_id": self.vault_id,
                "principal_id": self.principal_id,
                "session_id": self.session_id,
                "created_at": self.created_at.astimezone(UTC).isoformat(),
                "expires_at": self.expires_at.astimezone(UTC).isoformat(),
                "harm_already_occurred": self.harm_already_occurred,
                "possible_medical_emergency": self.possible_medical_emergency,
                "possible_current_danger": self.possible_current_danger,
                "immediate_danger": self.immediate_danger,
                "current_intent": self.current_intent,
                "plan_present": self.plan_present,
                "accessible_means": self.accessible_means,
                "imminent_timeframe": self.imminent_timeframe,
                "ongoing_distress": self.ongoing_distress,
                "ongoing_safety_concern": self.ongoing_safety_concern,
                "user_declined_clarification": self.user_declined_clarification,
                "abuse_or_coercive_control_context": self.abuse_or_coercive_control_context,
                "supporter_selected_by_user": self.supporter_selected_by_user,
                "supporter_confirmed_safe": self.supporter_confirmed_safe,
                "contacting_supporter_feasible": self.contacting_supporter_feasible,
                "country": self.country,
                "region": self.region,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
    """Authority-backed routing result from a safety gateway."""

    claims: SafetyDecisionClaims
    authority_receipt: SafetyAuthorityReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.claims, SafetyDecisionClaims):
            raise TypeError("claims must be SafetyDecisionClaims")
        if not isinstance(self.authority_receipt, SafetyAuthorityReceipt):
            raise TypeError("authority_receipt must be a SafetyAuthorityReceipt")
        try:
            SafetyRoute(self.claims.route)
        except ValueError as error:
            raise ValueError("decision claims contain an invalid route") from error
        try:
            SafetyPriority(self.claims.priority)
        except ValueError as error:
            raise ValueError("decision claims contain an invalid priority") from error

    @property
    def binding(self) -> SafetyBinding:
        return self.claims.binding

    @property
    def route(self) -> SafetyRoute:
        return SafetyRoute(self.claims.route)

    @property
    def priority(self) -> SafetyPriority:
        return SafetyPriority(self.claims.priority)

    @property
    def may_suggest_selected_supporter(self) -> bool:
        return self.claims.may_suggest_selected_supporter

    @property
    def evaluated_at(self) -> datetime:
        return self.claims.evaluated_at

    @property
    def expires_at(self) -> datetime:
        return self.claims.expires_at

    @property
    def valid_until(self) -> datetime:
        """Compatibility alias for the signed expiry."""

        return self.expires_at

    def is_current(self, *, at: datetime) -> bool:
        """Return whether ``at`` is in the half-open decision validity window."""

        require_aware(at, field_name="at")
        return self.evaluated_at <= at < self.expires_at

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

    def evaluate(self, state: SafetyState) -> SafetyDecision:
        """Route state using injected trusted time without calculating a score."""


@dataclass(frozen=True, slots=True)
class RuleBasedSafetyGateway:
    """Deterministic routing with trusted time and opaque authority issuance."""

    clock: TrustedClock
    authority: SafetyAuthorityPort
    policy_generation: str

    def __post_init__(self) -> None:
        if not isinstance(self.clock, TrustedClock):
            raise TypeError("clock must implement TrustedClock")
        if not isinstance(self.authority, SafetyAuthorityPort):
            raise TypeError("authority must implement SafetyAuthorityPort")
        require_string(self.policy_generation, field_name="policy_generation")
        normalized_generation = self.policy_generation.strip()
        if not normalized_generation:
            raise ValueError("policy_generation must not be empty")
        object.__setattr__(self, "policy_generation", normalized_generation)

    def evaluate(self, state: SafetyState) -> SafetyDecision:
        """Read the trusted clock once, route, and issue an exact receipt."""

        at = read_trusted_time(self.clock)
        if at < state.created_at:
            raise InactiveSafetyStateError("SafetyState cannot be evaluated before created_at")
        if state.is_expired(at=at):
            raise ExpiredSafetyStateError("expired SafetyState must be discarded and recollected")

        route, priority, can_suggest_supporter = self._route(state)
        binding = SafetyBinding(
            vault_id=state.vault_id,
            principal_id=state.principal_id,
            session_id=state.session_id,
            input_fingerprint=state.input_fingerprint,
            policy_generation=self.policy_generation,
        )
        claims = SafetyDecisionClaims(
            binding=binding,
            route=route.value,
            priority=priority.value,
            may_suggest_selected_supporter=can_suggest_supporter,
            evaluated_at=at,
            expires_at=state.expires_at,
        )
        receipt = self.authority.issue_decision(claims)
        if not isinstance(receipt, SafetyAuthorityReceipt):
            raise SafetyAuthorityVerificationError(
                "authority returned an invalid safety-decision receipt"
            )
        decision = SafetyDecision(claims=claims, authority_receipt=receipt)
        if self.authority.verify_decision(claims, receipt) is not True:
            raise SafetyAuthorityVerificationError("issued safety decision could not be verified")
        return decision

    @classmethod
    def _route(cls, state: SafetyState) -> tuple[SafetyRoute, SafetyPriority, bool]:
        can_suggest_supporter = state.can_suggest_selected_supporter
        if state.possible_medical_emergency is True or (
            state.harm_already_occurred is True and state.possible_medical_emergency is not False
        ):
            return (
                SafetyRoute.MEDICAL_EMERGENCY,
                SafetyPriority.MEDICAL_FIRST,
                can_suggest_supporter,
            )
        if state.harm_already_occurred is True:
            return (
                SafetyRoute.IMMEDIATE_DANGER,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
            )
        if state.immediate_danger is True or cls._has_confirmed_imminent_danger(state):
            return (
                SafetyRoute.IMMEDIATE_DANGER,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
            )
        if state.user_declined_clarification:
            return (
                SafetyRoute.USER_DECLINED_CLARIFICATION,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
            )
        if state.possible_current_danger and state.immediate_danger is not False:
            return (
                SafetyRoute.CLARIFY_IMMEDIATE_SAFETY,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
            )
        if cls._current_concern_needs_immediacy_clarification(state):
            return (
                SafetyRoute.CLARIFY_IMMEDIATE_SAFETY,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
            )
        if cls._has_non_imminent_current_concern(state) or state.ongoing_safety_concern:
            return (
                SafetyRoute.ONGOING_HUMAN_SUPPORT,
                SafetyPriority.SAFETY,
                can_suggest_supporter,
            )
        return (
            SafetyRoute.ORDINARY_COACH,
            SafetyPriority.ORDINARY,
            False,
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
