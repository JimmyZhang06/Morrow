"""Content-free command and replay normalization tests."""

from __future__ import annotations

import uuid

import pytest

from life_coach.application.candidate_insight import CANDIDATE_INSIGHT_TASK_TYPE
from life_coach.application.candidate_insight_command import (
    CandidateInsightGenerationStatus,
    GenerateCandidateInsight,
)
from life_coach.application.model_runtime import (
    ModelRunReplayInProgress,
    ModelRunReplayTerminal,
)
from life_coach.modules.model_runs.contracts import ModelRunArtifactRef
from life_coach.modules.model_runs.models import ModelRunState


class _Runtime:
    def __init__(self, outcome: ModelRunArtifactRef | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def run(self, **kwargs: object) -> ModelRunArtifactRef:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.asyncio
async def test_command_fixes_task_and_returns_only_server_artifact_identity() -> None:
    vault_id, fragment_id, run_id, memory_id, derived_id = (uuid.uuid4() for _ in range(5))
    key = uuid.uuid4()
    runtime = _Runtime(
        ModelRunArtifactRef(
            artifact_id=uuid.uuid4(),
            vault_id=vault_id,
            model_run_id=run_id,
            derived_object_id=derived_id,
            memory_claim_id=memory_id,
        )
    )

    result = await GenerateCandidateInsight(runtime).execute(
        authorization="Bearer opaque",
        vault_id=vault_id,
        fragment_ids=(fragment_id,),
        idempotency_key=key,
    )

    assert result.status is CandidateInsightGenerationStatus.SUCCEEDED
    assert result.run_id == run_id
    assert result.memory_id == memory_id
    assert result.derived_object_id == derived_id
    assert runtime.calls == [
        {
            "authorization": "Bearer opaque",
            "vault_id": vault_id,
            "task_type": CANDIDATE_INSIGHT_TASK_TYPE,
            "fragment_ids": (fragment_id,),
            "idempotency_key": str(key),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_error", "expected_status"),
    [
        (ModelRunReplayInProgress(uuid.UUID(int=1)), CandidateInsightGenerationStatus.PROCESSING),
        (
            ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.FAILED),
            CandidateInsightGenerationStatus.FAILED,
        ),
        (
            ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.UNKNOWN),
            CandidateInsightGenerationStatus.UNKNOWN,
        ),
        (
            ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.DENIED),
            CandidateInsightGenerationStatus.DENIED,
        ),
        (
            ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.CANCELED),
            CandidateInsightGenerationStatus.CANCELED,
        ),
    ],
)
async def test_command_exposes_only_stable_replay_state(
    runtime_error: Exception,
    expected_status: CandidateInsightGenerationStatus,
) -> None:
    result = await GenerateCandidateInsight(_Runtime(runtime_error)).execute(
        authorization="Bearer opaque",
        vault_id=uuid.uuid4(),
        fragment_ids=(uuid.uuid4(),),
        idempotency_key=uuid.uuid4(),
    )

    assert result.status is expected_status
    assert result.run_id == uuid.UUID(int=1)
    assert result.memory_id is None
    assert result.derived_object_id is None
