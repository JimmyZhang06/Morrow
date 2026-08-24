"""Injectable composition seam for candidate-insight generation."""

from fastapi import APIRouter

from life_coach.api.routers.candidate_insights import create_candidate_insight_router
from life_coach.api.routers.entry_candidate_insights import (
    create_entry_candidate_insight_router,
)
from life_coach.application.candidate_insight_command import (
    CandidateInsightRuntime,
    GenerateCandidateInsight,
)
from life_coach.application.candidate_insight_entry import GenerateCandidateInsightForEntry
from life_coach.application.model_runtime import AuthorizedVaultSessionOpener


def build_candidate_insight_router(
    *,
    runtime: CandidateInsightRuntime,
    sessions: AuthorizedVaultSessionOpener | None = None,
) -> APIRouter:
    """Bind the application command without constructing a provider or classifier."""

    router = APIRouter()
    router.include_router(
        create_candidate_insight_router(command=GenerateCandidateInsight(runtime))
    )
    if sessions is not None:
        router.include_router(
            create_entry_candidate_insight_router(
                command=GenerateCandidateInsightForEntry(sessions=sessions, runtime=runtime)
            )
        )
    return router


__all__ = ["build_candidate_insight_router"]
