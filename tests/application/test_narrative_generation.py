# ruff: noqa: RUF001
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from life_coach.api.routers.narratives import CalendarCandidateCreateRequest
from life_coach.application.narrative_generation import (
    LifeLineOutput,
    LifeLineThemeOutput,
    MemoirChapterOutput,
)
from life_coach.modules.model_runs.contracts import ModelRunArtifactKind, ModelRunArtifactSpec


def theme(**overrides: object) -> LifeLineThemeOutput:
    return LifeLineThemeOutput.model_validate(
        {
            "title": "先缩小，再开始",
            "interpretation": "你可能更容易在问题缩小后开始行动。",
            "supporting_ordinals": [1],
            "counterexample_ordinals": [2],
            "counterpoint": "这种方式不一定适用于所有情境。",
            "uncovered_period": "没有材料覆盖的时期保持空白。",
            **overrides,
        }
    )


def test_life_line_keeps_multiple_hypotheses_bounded() -> None:
    output = LifeLineOutput(
        overview="这些是可以并存、也可以被推翻的候选解释。",
        themes=[theme(), theme(title="在表达前寻找确定感")],
    )

    assert len(output.themes) == 2
    assert output.themes[0].supporting_ordinals == [1]


def test_life_line_rejects_duplicate_material_ordinals() -> None:
    with pytest.raises(ValidationError, match="ordinals must be unique"):
        theme(supporting_ordinals=[1, 1])


def test_memoir_rejects_diagnostic_claims() -> None:
    with pytest.raises(ValidationError, match="diagnostic claims"):
        MemoirChapterOutput(
            title="一段草稿",
            body="这些记录证明你被诊断为人格障碍。" * 10,
            uncertainty="材料有限。",
            citation_ordinals=[1],
        )


def test_memoir_requires_at_least_one_citation() -> None:
    with pytest.raises(ValidationError):
        MemoirChapterOutput(
            title="一段草稿",
            body="这是一段只基于已有材料、并明确保留空白的回忆录草稿。" * 8,
            uncertainty="材料有限。",
            citation_ordinals=[],
        )


def test_narrative_artifact_can_only_point_to_one_generation() -> None:
    generation_id = uuid.uuid4()
    artifact = ModelRunArtifactSpec(
        vault_id=uuid.uuid4(),
        artifact_kind=ModelRunArtifactKind.NARRATIVE,
        narrative_generation_id=generation_id,
    )

    assert artifact.narrative_generation_id == generation_id
    with pytest.raises(ValueError, match="artifact kind"):
        ModelRunArtifactSpec(
            vault_id=uuid.uuid4(),
            artifact_kind=ModelRunArtifactKind.NARRATIVE,
            narrative_generation_id=generation_id,
            action_id=uuid.uuid4(),
        )


def test_calendar_candidate_requires_explicit_short_aware_interval() -> None:
    start = datetime.now(UTC) + timedelta(days=1)
    value = CalendarCandidateCreateRequest(
        title="回看这一章",
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        timezone="Asia/Shanghai",
    )
    assert value.ends_at > value.starts_at

    with pytest.raises(ValidationError, match="24 hours"):
        CalendarCandidateCreateRequest(
            title="过长事件",
            starts_at=start,
            ends_at=start + timedelta(days=2),
            timezone="Asia/Shanghai",
        )
