from __future__ import annotations

import pytest
from pydantic import ValidationError

from life_coach.application.action_generation import ReversibleActionOutput


def _output(**overrides: object) -> ReversibleActionOutput:
    payload: dict[str, object] = {
        "title": "画一个最小流程图",
        "description": "用十分钟画出三个方框，然后只观察自己是否更容易开始。",  # noqa: RUF001
        "rationale": "用低成本方式检验已经确认的认识。",
        "exit_plan": "随时停止并丢弃草稿，不会产生外部影响。",  # noqa: RUF001
        "estimated_minutes": 10,
    }
    payload.update(overrides)
    return ReversibleActionOutput.model_validate(payload, strict=True)


def test_action_output_accepts_one_bounded_reversible_experiment() -> None:
    result = _output()

    assert result.estimated_minutes == 10
    assert result.title == "画一个最小流程图"


@pytest.mark.parametrize("minutes", [0, 16])
def test_action_output_rejects_work_outside_the_small_timebox(minutes: int) -> None:
    with pytest.raises(ValidationError):
        _output(estimated_minutes=minutes)


@pytest.mark.parametrize(
    "description",
    [
        "把第一版发送给同事并等待回复。",
        "购买一个新工具来开始这项练习。",
        "Email a colleague and schedule a meeting.",
    ],
)
def test_action_output_rejects_external_side_effects(description: str) -> None:
    with pytest.raises(ValidationError, match="external side effect"):
        _output(description=description)


def test_action_output_allows_explicitly_not_sending_a_private_draft() -> None:
    result = _output(
        description="把想说的话写进私人草稿，暂不发送，只观察自己的感受。"  # noqa: RUF001
    )

    assert "暂不发送" in result.description
