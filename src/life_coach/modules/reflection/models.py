"""Value objects for reflection-loop protection."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from typing import Literal, Self


class Construal(StrEnum):
    """The level at which the current reflection is expressed."""

    CONCRETE = "concrete"
    ABSTRACT = "abstract"


class ReflectionRecommendation(StrEnum):
    """A non-diagnostic recommendation for the current reflection turn."""

    CONTINUE = "continue"
    GENTLE_CHECK_IN = "gentle_check_in"
    PAUSE_DEEPENING = "pause_deepening"
    HONOR_EXIT = "honor_exit"


class ReflectionSignalGroup(StrEnum):
    """Independent evidence groups used by the combination rule."""

    LOOP_PATTERN = "loop_pattern"
    USER_REPORTED_IMPACT = "user_reported_impact"
    TIME_HEURISTIC = "time_heuristic"
    USER_CONTROL = "user_control"


class ReflectionSignal(StrEnum):
    """Explainable observations that can be returned to the caller."""

    TOPIC_REPETITION = "topic_repetition"
    ABSTRACT_CONSTRUAL = "abstract_construal"
    LOW_NEW_INFORMATION = "low_new_information"
    USER_REPORTS_UNHELPFUL = "user_reports_unhelpful"
    USER_REPORTS_MORE_STUCK = "user_reports_more_stuck"
    USER_REPORTS_MORE_PAINFUL = "user_reports_more_painful"
    USER_REPORTED_DISTRESS_INCREASE = "user_reported_distress_increase"
    SESSION_DURATION = "session_duration"
    DEEPENING_ALREADY_PAUSED = "deepening_already_paused"
    USER_REQUESTED_EXIT = "user_requested_exit"
    USER_CHOSE_TO_CONTINUE = "user_chose_to_continue"


class PauseOption(StrEnum):
    """Optional, non-prescriptive choices after deepening is paused."""

    REST = "rest"
    CONTINUE_EXPRESSING = "continue_expressing"
    SEEK_SAFE_COMPANY = "seek_safe_company"
    SMALL_REAL_WORLD_ACTION = "small_real_world_action"


@dataclass(frozen=True, slots=True)
class ReflectionDataBoundary:
    """Machine-readable limits on all detailed reflection-state fields."""

    retention_scope: Literal["session_only"] = "session_only"
    allow_long_term_profile: Literal[False] = False
    allow_embedding: Literal[False] = False
    allow_memoir: Literal[False] = False
    allow_proactive_recommendation: Literal[False] = False
    purge_details_at_session_end: Literal[True] = True


@dataclass(frozen=True, slots=True)
class ReflectionState:
    """A session-level snapshot used only to protect the current interaction.

    Distress values may be populated only from explicit user self-report. They are
    observations, not a clinical scale or prediction. ``actionability`` is retained
    for context but intentionally excluded from every stop/check-in rule.
    """

    session_id: str
    topic_repetition: int = 0
    new_information_ratio: float = 1.0
    construal: Construal = Construal.CONCRETE
    actionability: bool | None = None
    session_duration: timedelta = timedelta()
    distress_before: int | None = None
    distress_after: int | None = None
    user_reports_unhelpful: bool = False
    user_reports_more_stuck: bool = False
    user_reports_more_painful: bool = False
    exit_requested: bool = False
    deepening_paused: bool = False
    turn_follow_up_locked: bool = False
    duration_check_in_acknowledged: bool = False
    data_boundary: ReflectionDataBoundary = field(
        default_factory=ReflectionDataBoundary,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be blank")
        if not isinstance(self.topic_repetition, int) or isinstance(self.topic_repetition, bool):
            raise ValueError("topic_repetition must be an integer")
        if self.topic_repetition < 0:
            raise ValueError("topic_repetition must be non-negative")
        if not isinstance(self.new_information_ratio, (int, float)) or isinstance(
            self.new_information_ratio, bool
        ):
            raise ValueError("new_information_ratio must be numeric")
        if not math.isfinite(self.new_information_ratio):
            raise ValueError("new_information_ratio must be finite")
        if not 0.0 <= self.new_information_ratio <= 1.0:
            raise ValueError("new_information_ratio must be between 0 and 1")
        if not isinstance(self.construal, Construal):
            raise ValueError("construal must be a Construal value")
        if self.actionability is not None and not isinstance(self.actionability, bool):
            raise ValueError("actionability must be a bool when provided")
        if not isinstance(self.session_duration, timedelta):
            raise ValueError("session_duration must be a timedelta")
        if self.session_duration < timedelta():
            raise ValueError("session_duration must be non-negative")
        for name, value in (
            ("user_reports_unhelpful", self.user_reports_unhelpful),
            ("user_reports_more_stuck", self.user_reports_more_stuck),
            ("user_reports_more_painful", self.user_reports_more_painful),
            ("exit_requested", self.exit_requested),
            ("deepening_paused", self.deepening_paused),
            ("turn_follow_up_locked", self.turn_follow_up_locked),
            ("duration_check_in_acknowledged", self.duration_check_in_acknowledged),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a bool")
        self._validate_distress("distress_before", self.distress_before)
        self._validate_distress("distress_after", self.distress_after)

    @staticmethod
    def _validate_distress(name: str, value: int | None) -> None:
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer when provided")
        if not 0 <= value <= 10:
            raise ValueError(f"{name} must be between 0 and 10 when provided")

    @property
    def user_reported_distress_increased(self) -> bool:
        """Compare distress only when the user supplied both observations."""

        return (
            self.distress_before is not None
            and self.distress_after is not None
            and self.distress_after > self.distress_before
        )

    def mark_exit_requested(self) -> Self:
        """Lock all follow-up and deepening for the remainder of this turn."""

        return replace(self, exit_requested=True, turn_follow_up_locked=True)

    def mark_deepening_paused(self) -> Self:
        """Persist a protective pause within this short-lived session state."""

        return replace(self, deepening_paused=True)

    def begin_user_turn(self) -> Self:
        """Open a new user-initiated turn without silently resuming deepening."""

        return replace(self, exit_requested=False, turn_follow_up_locked=False)

    def resume_deepening_at_user_request(self) -> Self:
        """Record the user's choice to continue after a protective check-in.

        The policy still recomputes the combination rule, so this transition cannot
        override an explicit exit or a loop combined with user-reported harm.
        """

        return self.continue_after_gentle_check_in()

    def continue_after_gentle_check_in(self) -> Self:
        """Acknowledge a check-in so time alone cannot stall the whole session."""

        return replace(
            self,
            deepening_paused=False,
            duration_check_in_acknowledged=True,
        )


@dataclass(frozen=True, slots=True)
class TriggeredSignal:
    """An explainable input to a recommendation."""

    code: ReflectionSignal
    group: ReflectionSignalGroup
    reason: str


@dataclass(frozen=True, slots=True)
class ReflectionAssessment:
    """Structured, non-diagnostic policy output for one turn."""

    recommendation: ReflectionRecommendation
    reason: str
    message: str
    signals: tuple[TriggeredSignal, ...]
    next_state: ReflectionState
    may_deepen: bool
    may_ask_gentle_check_in: bool
    may_ask_deepening_question: bool
    max_questions: Literal[0, 1]
    turn_locked: bool
    pause_options: tuple[PauseOption, ...] = ()
    todo_required: Literal[False] = False
    creates_todo: Literal[False] = False
    diagnostic_claim: Literal[False] = False
    policy_version: str = "reflection-v1"
