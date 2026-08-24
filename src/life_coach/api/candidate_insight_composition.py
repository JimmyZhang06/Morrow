"""Injectable composition seam for candidate-insight generation."""

from fastapi import APIRouter

from life_coach.api.routers.candidate_insights import create_candidate_insight_router
from life_coach.application.candidate_insight_command import (
    CandidateInsightRuntime,
    GenerateCandidateInsight,
)


def build_candidate_insight_router(*, runtime: CandidateInsightRuntime) -> APIRouter:
    """Bind the application command without constructing a provider or classifier."""

    return create_candidate_insight_router(command=GenerateCandidateInsight(runtime))


__all__ = ["build_candidate_insight_router"]
