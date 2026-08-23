"""Explainable combination policy for reflective-loop protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from life_coach.modules.reflection.models import (
    Construal,
    PauseOption,
    ReflectionAssessment,
    ReflectionRecommendation,
    ReflectionSignal,
    ReflectionSignalGroup,
    ReflectionState,
    TriggeredSignal,
)

_PAUSE_OPTIONS = (
    PauseOption.REST,
    PauseOption.CONTINUE_EXPRESSING,
    PauseOption.SEEK_SAFE_COMPANY,
    PauseOption.SMALL_REAL_WORLD_ACTION,
)


@dataclass(frozen=True, slots=True)
class ReflectionPolicy:
    """Apply non-diagnostic safeguards to one session-level snapshot.

    Deepening is paused only when observations from independent groups combine.
    Missing action, one signal, or elapsed time alone never triggers the stop rule.
    """

    repetition_threshold: int = 3
    low_information_threshold: float = 0.2
    check_in_after: timedelta = timedelta(minutes=10)
    protective_duration: timedelta = timedelta(minutes=15)
    policy_version: str = "reflection-v1"

    def __post_init__(self) -> None:
        if self.repetition_threshold < 2:
            raise ValueError("repetition_threshold must represent multiple turns")
        if not 0.0 <= self.low_information_threshold <= 1.0:
            raise ValueError("low_information_threshold must be between 0 and 1")
        if self.check_in_after <= timedelta():
            raise ValueError("check_in_after must be positive")
        if self.protective_duration < self.check_in_after:
            raise ValueError("protective_duration must be at or after check_in_after")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be blank")

    def assess(self, state: ReflectionState) -> ReflectionAssessment:
        """Return the next safe conversation mode and every contributing signal."""

        signals = self._collect_signals(state)
        if state.exit_requested or state.turn_follow_up_locked:
            return self._honor_exit(state, signals)
        if state.deepening_paused:
            return self._keep_pause(state, signals)

        loop_signals = self._group(signals, ReflectionSignalGroup.LOOP_PATTERN)
        impact_signals = self._group(signals, ReflectionSignalGroup.USER_REPORTED_IMPACT)
        duration_signals = self._group(signals, ReflectionSignalGroup.TIME_HEURISTIC)

        # Normal path: at least two loop features plus explicit user-reported impact.
        combined_loop_and_impact = len(loop_signals) >= 2 and bool(impact_signals)
        # At about 15 minutes, time may join one loop feature and user-reported impact.
        # It remains a protective heuristic and can never decide by itself.
        protective_time_combination = (
            state.session_duration >= self.protective_duration
            and bool(loop_signals)
            and bool(impact_signals)
            and bool(duration_signals)
        )

        if combined_loop_and_impact:
            return self._pause(
                state,
                signals,
                reason=(
                    "At least two loop features combine with the user's report of impact; "
                    "stop asking for deeper causes for now."
                ),
            )
        if protective_time_combination:
            return self._pause(
                state,
                signals,
                reason=(
                    "A loop feature and the user's report of impact continue at the protective "
                    "duration check. Time is only a heuristic joining the other signals."
                ),
            )

        needs_check_in = (
            bool(impact_signals)
            or len(loop_signals) >= 2
            or (
                state.session_duration >= self.check_in_after
                and not state.duration_check_in_acknowledged
            )
        )
        if needs_check_in:
            return self._gentle_check_in(state, signals)
        return self._continue(state, signals)

    def evaluate(self, state: ReflectionState) -> ReflectionAssessment:
        """Alias for callers that use evaluator terminology."""

        return self.assess(state)

    def _collect_signals(self, state: ReflectionState) -> tuple[TriggeredSignal, ...]:
        signals: list[TriggeredSignal] = []
        if state.topic_repetition >= self.repetition_threshold:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.TOPIC_REPETITION,
                    ReflectionSignalGroup.LOOP_PATTERN,
                    "The same topic has repeated across several turns.",
                )
            )
        if state.construal is Construal.ABSTRACT:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.ABSTRACT_CONSTRUAL,
                    ReflectionSignalGroup.LOOP_PATTERN,
                    "The current reflection is mainly framed as abstract causes.",
                )
            )
        if state.new_information_ratio <= self.low_information_threshold:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.LOW_NEW_INFORMATION,
                    ReflectionSignalGroup.LOOP_PATTERN,
                    "Recent turns contain little new evidence or understanding.",
                )
            )

        if state.user_reports_unhelpful:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.USER_REPORTS_UNHELPFUL,
                    ReflectionSignalGroup.USER_REPORTED_IMPACT,
                    "The user reports that the reflection is not helping.",
                )
            )
        if state.user_reports_more_stuck:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.USER_REPORTS_MORE_STUCK,
                    ReflectionSignalGroup.USER_REPORTED_IMPACT,
                    "The user reports feeling more stuck.",
                )
            )
        if state.user_reports_more_painful:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.USER_REPORTS_MORE_PAINFUL,
                    ReflectionSignalGroup.USER_REPORTED_IMPACT,
                    "The user reports that continuing feels more painful.",
                )
            )
        if state.user_reported_distress_increased:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.USER_REPORTED_DISTRESS_INCREASE,
                    ReflectionSignalGroup.USER_REPORTED_IMPACT,
                    "The user's before-and-after self-report increased.",
                )
            )

        if state.session_duration >= self.check_in_after:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.SESSION_DURATION,
                    ReflectionSignalGroup.TIME_HEURISTIC,
                    "The reflection has continued for about 10-15 minutes or longer.",
                )
            )
        if state.deepening_paused:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.DEEPENING_ALREADY_PAUSED,
                    ReflectionSignalGroup.USER_CONTROL,
                    "Deepening is already paused for this session.",
                )
            )
        if state.exit_requested or state.turn_follow_up_locked:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.USER_REQUESTED_EXIT,
                    ReflectionSignalGroup.USER_CONTROL,
                    "The user asked to stop this turn.",
                )
            )
        if state.duration_check_in_acknowledged:
            signals.append(
                TriggeredSignal(
                    ReflectionSignal.USER_CHOSE_TO_CONTINUE,
                    ReflectionSignalGroup.USER_CONTROL,
                    "The user chose to continue after the protective check-in.",
                )
            )
        return tuple(signals)

    @staticmethod
    def _group(
        signals: tuple[TriggeredSignal, ...], group: ReflectionSignalGroup
    ) -> tuple[TriggeredSignal, ...]:
        return tuple(signal for signal in signals if signal.group is group)

    def _continue(
        self, state: ReflectionState, signals: tuple[TriggeredSignal, ...]
    ) -> ReflectionAssessment:
        return ReflectionAssessment(
            recommendation=ReflectionRecommendation.CONTINUE,
            reason="No combination indicates that deepening should pause.",
            message=(
                "You can continue at your own pace. This reflection does not need to end in "
                "an action or task."
            ),
            signals=signals,
            next_state=state,
            may_deepen=True,
            may_ask_gentle_check_in=False,
            may_ask_deepening_question=True,
            max_questions=1,
            turn_locked=False,
            policy_version=self.policy_version,
        )

    def _gentle_check_in(
        self, state: ReflectionState, signals: tuple[TriggeredSignal, ...]
    ) -> ReflectionAssessment:
        return ReflectionAssessment(
            recommendation=ReflectionRecommendation.GENTLE_CHECK_IN,
            reason=(
                "Some protective signals are present, but the cross-group combination needed "
                "to pause deepening is not present."
            ),
            message=(
                "A gentle check-in may help: is continuing in this way useful right now? "
                "Pausing or continuing to express yourself are both valid choices."
            ),
            signals=signals,
            next_state=state,
            may_deepen=False,
            may_ask_gentle_check_in=True,
            may_ask_deepening_question=False,
            max_questions=1,
            turn_locked=False,
            policy_version=self.policy_version,
        )

    def _pause(
        self,
        state: ReflectionState,
        signals: tuple[TriggeredSignal, ...],
        *,
        reason: str,
    ) -> ReflectionAssessment:
        next_state = state.mark_deepening_paused()
        return ReflectionAssessment(
            recommendation=ReflectionRecommendation.PAUSE_DEEPENING,
            reason=reason,
            message=(
                "Let's stop digging into causes for now. You may rest, keep expressing without "
                "further analysis, seek company from someone you consider safe, or choose a "
                "small real-world action. None is required, and no task is created."
            ),
            signals=signals,
            next_state=next_state,
            may_deepen=False,
            may_ask_gentle_check_in=True,
            may_ask_deepening_question=False,
            max_questions=1,
            turn_locked=False,
            pause_options=_PAUSE_OPTIONS,
            policy_version=self.policy_version,
        )

    def _keep_pause(
        self, state: ReflectionState, signals: tuple[TriggeredSignal, ...]
    ) -> ReflectionAssessment:
        return ReflectionAssessment(
            recommendation=ReflectionRecommendation.PAUSE_DEEPENING,
            reason="Deepening remains paused unless the user explicitly asks to resume it.",
            message=(
                "We can leave the deeper analysis paused. Resting, simply expressing what is "
                "here, safe company, or a small real-world action are optional."
            ),
            signals=signals,
            next_state=state,
            may_deepen=False,
            may_ask_gentle_check_in=True,
            may_ask_deepening_question=False,
            max_questions=1,
            turn_locked=False,
            pause_options=_PAUSE_OPTIONS,
            policy_version=self.policy_version,
        )

    def _honor_exit(
        self, state: ReflectionState, signals: tuple[TriggeredSignal, ...]
    ) -> ReflectionAssessment:
        next_state = state.mark_exit_requested()
        return ReflectionAssessment(
            recommendation=ReflectionRecommendation.HONOR_EXIT,
            reason="The user's explicit exit overrides every reflection recommendation.",
            message="Okay. We will stop this turn here.",
            signals=signals,
            next_state=next_state,
            may_deepen=False,
            may_ask_gentle_check_in=False,
            may_ask_deepening_question=False,
            max_questions=0,
            turn_locked=True,
            policy_version=self.policy_version,
        )
