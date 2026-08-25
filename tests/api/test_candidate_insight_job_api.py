"""HTTP contract tests for the durable candidate-insight job resource."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from life_coach.api.routers.candidate_insight_jobs import (
    create_candidate_insight_job_router,
)
from life_coach.application.candidate_insight_jobs import (
    CandidateInsightJobProjection,
    CandidateInsightJobStatus,
)
from life_coach.platform.errors import install_error_handlers


class _Jobs:
    def __init__(self) -> None:
        self.job_id = uuid.uuid4()
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, **kwargs: object) -> CandidateInsightJobProjection:
        self.calls.append(("enqueue", kwargs))
        return CandidateInsightJobProjection(
            job_id=self.job_id,
            status=CandidateInsightJobStatus.QUEUED,
            stage="queued",
            progress=5,
            retryable=False,
        )

    async def get(self, **kwargs: object) -> CandidateInsightJobProjection:
        self.calls.append(("get", kwargs))
        return CandidateInsightJobProjection(
            job_id=self.job_id,
            status=CandidateInsightJobStatus.SUCCEEDED,
            stage="ready",
            progress=100,
            retryable=False,
            run_id=uuid.UUID(int=2),
            memory_id=uuid.UUID(int=3),
            derived_object_id=uuid.UUID(int=4),
        )

    async def cancel(self, **kwargs: object) -> CandidateInsightJobProjection:
        self.calls.append(("cancel", kwargs))
        return CandidateInsightJobProjection(
            job_id=self.job_id,
            status=CandidateInsightJobStatus.CANCELED,
            stage="canceled",
            progress=100,
            retryable=False,
        )


@pytest.mark.asyncio
async def test_job_resource_exposes_queue_progress_result_and_cancel() -> None:
    jobs = _Jobs()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(create_candidate_insight_job_router(jobs=jobs))
    vault_id, entry_id, key = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    headers = {
        "Authorization": "Bearer opaque-token",
        "X-Vault-ID": str(vault_id),
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        queued = await client.post(
            f"/v1/entries/{entry_id}/candidate-insights",
            headers={**headers, "Idempotency-Key": str(key), "If-Match": 'W/"7"'},
            json={},
        )
        finished = await client.get(
            f"/v1/candidate-insight-jobs/{jobs.job_id}",
            headers=headers,
        )
        canceled = await client.delete(
            f"/v1/candidate-insight-jobs/{jobs.job_id}",
            headers=headers,
        )

    assert queued.status_code == 202
    assert queued.headers["cache-control"] == "private, no-store"
    assert queued.json() == {
        "job_id": str(jobs.job_id),
        "status": "queued",
        "stage": "queued",
        "progress": 5,
        "retryable": False,
        "run_id": None,
        "memory_id": None,
        "derived_object_id": None,
    }
    assert finished.json()["status"] == "succeeded"
    assert finished.json()["memory_id"] == str(uuid.UUID(int=3))
    assert canceled.json()["status"] == "canceled"
    assert jobs.calls[0] == (
        "enqueue",
        {
            "authorization": "Bearer opaque-token",
            "vault_id": vault_id,
            "entry_id": entry_id,
            "expected_revision": 7,
            "idempotency_key": key,
        },
    )

