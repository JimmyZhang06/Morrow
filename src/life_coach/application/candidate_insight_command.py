"""Application command for the first evidence-backed candidate-insight loop."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from life_coach.application.candidate_insight import CANDIDATE_INSIGHT_TASK_TYPE
from life_coach.application.model_runtime import (
    ModelRunReplayInProgress,
    ModelRunReplayTerminal,
)
from life_coach.modules.model_runs.contracts import ModelRunArtifactRef
from life_coach.modules.model_runs.models import ModelRunState


class CandidateInsightRuntime(Protocol):
    async def run(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: str,
    ) -> ModelRunArtifactRef: ...


class CandidateInsightGenerationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PROCESSING = "processing"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DENIED = "denied"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class CandidateInsightGenerationResult:
    """Content-free result safe for first-party clients and idempotent replay."""

    status: CandidateInsightGenerationStatus
    run_id: uuid.UUID
    memory_id: uuid.UUID | None = None
    derived_object_id: uuid.UUID | None = None


class GenerateCandidateInsight:
    """Invoke the one server-defined candidate task and normalize replay state."""

    def __init__(self, runtime: CandidateInsightRuntime) -> None:
        self._runtime = runtime

    async def execute(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: uuid.UUID,
    ) -> CandidateInsightGenerationResult:
        try:
            artifact = await self._runtime.run(
                authorization=authorization,
                vault_id=vault_id,
                task_type=CANDIDATE_INSIGHT_TASK_TYPE,
                fragment_ids=fragment_ids,
                idempotency_key=str(idempotency_key),
            )
        except ModelRunReplayInProgress as exc:
            return CandidateInsightGenerationResult(
                status=CandidateInsightGenerationStatus.PROCESSING,
                run_id=exc.run_id,
            )
        except ModelRunReplayTerminal as exc:
            return CandidateInsightGenerationResult(
                status=_terminal_status(exc.state),
                run_id=exc.run_id,
            )
        return CandidateInsightGenerationResult(
            status=CandidateInsightGenerationStatus.SUCCEEDED,
            run_id=artifact.model_run_id,
            memory_id=artifact.memory_claim_id,
            derived_object_id=artifact.derived_object_id,
        )


def _terminal_status(state: ModelRunState) -> CandidateInsightGenerationStatus:
    mapping = {
        ModelRunState.FAILED: CandidateInsightGenerationStatus.FAILED,
        ModelRunState.UNKNOWN: CandidateInsightGenerationStatus.UNKNOWN,
        ModelRunState.DENIED: CandidateInsightGenerationStatus.DENIED,
        ModelRunState.CANCELED: CandidateInsightGenerationStatus.CANCELED,
    }
    try:
        return mapping[state]
    except KeyError:
        # The runtime guarantees only non-success terminal states reach here.
        # Fail closed if that invariant changes rather than inventing a client state.
        raise RuntimeError("unsupported model run replay state") from None


__all__ = [
    "CandidateInsightGenerationResult",
    "CandidateInsightGenerationStatus",
    "CandidateInsightRuntime",
    "GenerateCandidateInsight",
]
