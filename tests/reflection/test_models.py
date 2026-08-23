"""Invariants of short-lived reflection domain values."""

from __future__ import annotations

from datetime import timedelta

import pytest

from life_coach.modules.reflection import ReflectionState


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"session_id": ""}, "session_id"),
        ({"session_id": "x", "topic_repetition": -1}, "topic_repetition"),
        ({"session_id": "x", "new_information_ratio": 1.1}, "new_information_ratio"),
        ({"session_id": "x", "new_information_ratio": float("nan")}, "finite"),
        ({"session_id": "x", "session_duration": timedelta(seconds=-1)}, "duration"),
        ({"session_id": "x", "distress_before": 11}, "distress_before"),
        ({"session_id": "x", "distress_after": -1}, "distress_after"),
        ({"session_id": "x", "construal": "abstract"}, "construal"),
        ({"session_id": "x", "user_reports_more_stuck": "false"}, "bool"),
        ({"session_id": "x", "duration_check_in_acknowledged": "true"}, "bool"),
        ({"session_id": "x", "distress_before": True}, "integer"),
    ],
)
def test_invalid_session_observations_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ReflectionState(**kwargs)  # type: ignore[arg-type]


def test_distress_change_requires_two_user_reported_observations() -> None:
    assert (
        ReflectionState(
            session_id="before-only",
            distress_before=4,
        ).user_reported_distress_increased
        is False
    )
    assert (
        ReflectionState(
            session_id="both",
            distress_before=4,
            distress_after=5,
        ).user_reported_distress_increased
        is True
    )


def test_resume_method_does_not_override_a_turn_exit_lock() -> None:
    state = ReflectionState(
        session_id="locked-and-paused",
        deepening_paused=True,
    ).mark_exit_requested()

    resumed = state.resume_deepening_at_user_request()

    assert resumed.deepening_paused is False
    assert resumed.exit_requested is True
    assert resumed.turn_follow_up_locked is True
