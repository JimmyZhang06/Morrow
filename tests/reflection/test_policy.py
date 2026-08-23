"""Domain tests for the session-only reflection policy."""

from __future__ import annotations

from datetime import timedelta

import pytest

from life_coach.modules.reflection import (
    Construal,
    PauseOption,
    ReflectionPolicy,
    ReflectionRecommendation,
    ReflectionSignal,
    ReflectionState,
)


def test_actionability_false_alone_is_not_a_stop_signal() -> None:
    result = ReflectionPolicy().assess(ReflectionState(session_id="grief", actionability=False))

    assert result.recommendation is ReflectionRecommendation.CONTINUE
    assert result.signals == ()
    assert result.todo_required is False
    assert "action or task" in result.message


def test_grief_without_action_is_not_treated_as_unhealthy_reflection() -> None:
    state = ReflectionState(
        session_id="mourning",
        topic_repetition=4,
        new_information_ratio=0.1,
        construal=Construal.ABSTRACT,
        actionability=False,
        session_duration=timedelta(minutes=8),
    )

    result = ReflectionPolicy().assess(state)

    assert result.recommendation is ReflectionRecommendation.GENTLE_CHECK_IN
    assert result.creates_todo is False


@pytest.mark.parametrize(
    "state",
    [
        ReflectionState(session_id="repeat", topic_repetition=3),
        ReflectionState(session_id="abstract", construal=Construal.ABSTRACT),
        ReflectionState(session_id="low-info", new_information_ratio=0.2),
        ReflectionState(session_id="unhelpful", user_reports_unhelpful=True),
        ReflectionState(session_id="stuck", user_reports_more_stuck=True),
        ReflectionState(session_id="pain", user_reports_more_painful=True),
        ReflectionState(session_id="distress", distress_before=3, distress_after=4),
        ReflectionState(session_id="duration", session_duration=timedelta(minutes=16)),
    ],
)
def test_a_single_signal_never_pauses_deepening(state: ReflectionState) -> None:
    result = ReflectionPolicy().assess(state)

    assert result.recommendation is not ReflectionRecommendation.PAUSE_DEEPENING
    assert result.next_state.deepening_paused is False


def test_multiple_loop_signals_without_user_report_only_prompt_a_check_in() -> None:
    state = ReflectionState(
        session_id="loop-only",
        topic_repetition=3,
        new_information_ratio=0.05,
        construal=Construal.ABSTRACT,
    )

    result = ReflectionPolicy().assess(state)

    assert result.recommendation is ReflectionRecommendation.GENTLE_CHECK_IN
    assert result.may_ask_gentle_check_in is True
    assert result.may_ask_deepening_question is False


def test_combined_loop_and_user_report_pauses_with_explainable_signals() -> None:
    state = ReflectionState(
        session_id="combined",
        topic_repetition=4,
        new_information_ratio=0.1,
        construal=Construal.ABSTRACT,
        user_reports_more_stuck=True,
        session_duration=timedelta(minutes=11),
    )

    result = ReflectionPolicy().assess(state)

    assert result.recommendation is ReflectionRecommendation.PAUSE_DEEPENING
    assert result.next_state.deepening_paused is True
    assert {signal.code for signal in result.signals} >= {
        ReflectionSignal.TOPIC_REPETITION,
        ReflectionSignal.ABSTRACT_CONSTRUAL,
        ReflectionSignal.LOW_NEW_INFORMATION,
        ReflectionSignal.USER_REPORTS_MORE_STUCK,
        ReflectionSignal.SESSION_DURATION,
    }
    assert result.may_ask_deepening_question is False
    assert result.pause_options == (
        PauseOption.REST,
        PauseOption.CONTINUE_EXPRESSING,
        PauseOption.SEEK_SAFE_COMPANY,
        PauseOption.SMALL_REAL_WORLD_ACTION,
    )
    assert result.todo_required is False
    assert result.creates_todo is False
    assert result.diagnostic_claim is False


def test_fifteen_minute_heuristic_only_joins_other_signal_groups() -> None:
    state = ReflectionState(
        session_id="time-combination",
        topic_repetition=3,
        user_reports_unhelpful=True,
        session_duration=timedelta(minutes=15),
    )

    result = ReflectionPolicy().assess(state)

    assert result.recommendation is ReflectionRecommendation.PAUSE_DEEPENING
    assert {signal.code for signal in result.signals} >= {
        ReflectionSignal.TOPIC_REPETITION,
        ReflectionSignal.USER_REPORTS_UNHELPFUL,
        ReflectionSignal.SESSION_DURATION,
    }


def test_ten_minute_duration_alone_is_only_a_gentle_check_in() -> None:
    result = ReflectionPolicy().assess(
        ReflectionState(session_id="time-only", session_duration=timedelta(minutes=10))
    )

    assert result.recommendation is ReflectionRecommendation.GENTLE_CHECK_IN
    assert result.next_state.deepening_paused is False


def test_user_can_continue_after_a_time_only_gentle_check_in() -> None:
    policy = ReflectionPolicy()
    first = policy.assess(
        ReflectionState(session_id="time-choice", session_duration=timedelta(minutes=10))
    )

    continued = policy.assess(first.next_state.continue_after_gentle_check_in())

    assert continued.recommendation is ReflectionRecommendation.CONTINUE
    assert continued.may_deepen is True
    assert ReflectionSignal.USER_CHOSE_TO_CONTINUE in {signal.code for signal in continued.signals}


def test_check_in_acknowledgement_cannot_bypass_combined_stop_rule() -> None:
    state = ReflectionState(
        session_id="combined-after-choice",
        topic_repetition=4,
        new_information_ratio=0.1,
        construal=Construal.ABSTRACT,
        user_reports_more_stuck=True,
        session_duration=timedelta(minutes=11),
    ).continue_after_gentle_check_in()

    result = ReflectionPolicy().assess(state)

    assert result.recommendation is ReflectionRecommendation.PAUSE_DEEPENING
    assert result.next_state.deepening_paused is True


@pytest.mark.parametrize(
    "state",
    [
        ReflectionState(
            session_id="new-impact-after-choice",
            session_duration=timedelta(minutes=11),
            user_reports_more_painful=True,
        ),
        ReflectionState(
            session_id="loop-after-choice",
            session_duration=timedelta(minutes=11),
            topic_repetition=4,
            new_information_ratio=0.1,
        ),
    ],
)
def test_duration_acknowledgement_does_not_hide_new_protective_signals(
    state: ReflectionState,
) -> None:
    acknowledged = state.continue_after_gentle_check_in()

    result = ReflectionPolicy().assess(acknowledged)

    assert result.recommendation is ReflectionRecommendation.GENTLE_CHECK_IN
    assert result.may_deepen is False


def test_explicit_exit_locks_turn_and_overrides_combined_stop_rule() -> None:
    state = ReflectionState(
        session_id="exit",
        topic_repetition=5,
        new_information_ratio=0.0,
        construal=Construal.ABSTRACT,
        user_reports_more_painful=True,
        session_duration=timedelta(minutes=20),
        exit_requested=True,
    )

    result = ReflectionPolicy().assess(state)

    assert result.recommendation is ReflectionRecommendation.HONOR_EXIT
    assert result.turn_locked is True
    assert result.next_state.turn_follow_up_locked is True
    assert result.may_deepen is False
    assert result.may_ask_gentle_check_in is False
    assert result.may_ask_deepening_question is False
    assert result.max_questions == 0
    assert result.pause_options == ()
    assert "?" not in result.message


def test_locked_turn_cannot_be_reopened_by_rephrasing() -> None:
    policy = ReflectionPolicy()
    locked_state = policy.assess(
        ReflectionState(session_id="locked").mark_exit_requested()
    ).next_state

    repeated_result = policy.evaluate(locked_state)

    assert repeated_result.recommendation is ReflectionRecommendation.HONOR_EXIT
    assert repeated_result.max_questions == 0
    assert repeated_result.next_state.turn_follow_up_locked is True


def test_only_a_new_user_turn_releases_exit_lock_not_the_pause() -> None:
    state = ReflectionState(session_id="new-turn", deepening_paused=True).mark_exit_requested()

    new_turn = state.begin_user_turn()

    assert new_turn.exit_requested is False
    assert new_turn.turn_follow_up_locked is False
    assert new_turn.deepening_paused is True
    assert (
        ReflectionPolicy().assess(new_turn).recommendation
        is ReflectionRecommendation.PAUSE_DEEPENING
    )


def test_detailed_state_declares_session_only_data_boundary() -> None:
    boundary = ReflectionState(session_id="boundary").data_boundary

    assert boundary.retention_scope == "session_only"
    assert boundary.allow_long_term_profile is False
    assert boundary.allow_embedding is False
    assert boundary.allow_memoir is False
    assert boundary.allow_proactive_recommendation is False
    assert boundary.purge_details_at_session_end is True
