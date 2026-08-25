"""HTTP contract tests for injectable candidate-insight generation."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from life_coach.api.candidate_insight_composition import build_candidate_insight_router
from life_coach.api.routers.entry_candidate_insights import (
    create_entry_candidate_insight_router,
)
from life_coach.application.candidate_insight import CANDIDATE_INSIGHT_TASK_TYPE
from life_coach.application.model_runtime import (
    ModelRunProviderUnavailable,
    ModelRunReplayInProgress,
    ModelRunReplayTerminal,
)
from life_coach.modules.model_runs.contracts import ModelRunArtifactRef
from life_coach.modules.model_runs.models import ModelRunState
from life_coach.platform.errors import install_error_handlers


class _Runtime:
    def __init__(self, outcome: ModelRunArtifactRef | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def run(self, **kwargs: object) -> ModelRunArtifactRef:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _EntryCommand:
    async def execute(self, **_kwargs: object) -> object:
        raise ModelRunProviderUnavailable("provider unavailable")


def _app(runtime: _Runtime) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(build_candidate_insight_router(runtime=runtime))
    return app


def _entry_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(create_entry_candidate_insight_router(command=_EntryCommand()))
    return app


async def _post(
    app: FastAPI,
    *,
    vault_id: uuid.UUID,
    key: uuid.UUID,
    fragment_ids: list[uuid.UUID],
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/v1/candidate-insights",
            headers={
                "Authorization": "Bearer opaque-token",
                "X-Vault-ID": str(vault_id),
                "Idempotency-Key": str(key),
            },
            json={"fragment_ids": [str(item) for item in fragment_ids]},
        )


@pytest.mark.asyncio
async def test_success_response_contains_only_durable_identity_and_forwards_auth() -> None:
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

    response = await _post(
        _app(runtime),
        vault_id=vault_id,
        key=key,
        fragment_ids=[fragment_id],
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "status": "succeeded",
        "run_id": str(run_id),
        "memory_id": str(memory_id),
        "derived_object_id": str(derived_id),
    }
    assert runtime.calls == [
        {
            "authorization": "Bearer opaque-token",
            "vault_id": vault_id,
            "task_type": CANDIDATE_INSIGHT_TASK_TYPE,
            "fragment_ids": (fragment_id,),
            "idempotency_key": str(key),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status_code", "body_status"),
    [
        (ModelRunReplayInProgress(uuid.UUID(int=1)), 202, "processing"),
        (ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.FAILED), 409, "failed"),
        (ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.UNKNOWN), 409, "unknown"),
        (ModelRunReplayTerminal(uuid.UUID(int=1), ModelRunState.DENIED), 409, "denied"),
    ],
)
async def test_replay_http_states_are_stable_and_content_free(
    outcome: Exception,
    status_code: int,
    body_status: str,
) -> None:
    response = await _post(
        _app(_Runtime(outcome)),
        vault_id=uuid.uuid4(),
        key=uuid.uuid4(),
        fragment_ids=[uuid.uuid4()],
    )

    assert response.status_code == status_code
    assert response.json() == {
        "status": body_status,
        "run_id": str(uuid.UUID(int=1)),
        "memory_id": None,
        "derived_object_id": None,
    }


@pytest.mark.asyncio
async def test_provider_unavailable_returns_safe_retryable_service_response() -> None:
    response = await _post(
        _app(_Runtime(ModelRunProviderUnavailable("provider unavailable"))),
        vault_id=uuid.uuid4(),
        key=uuid.uuid4(),
        fragment_ids=[uuid.uuid4()],
    )

    assert response.status_code == 503
    assert response.json()["code"] == "MODEL_PROVIDER_UNAVAILABLE"
    assert response.json()["safe_detail"].startswith("没有发送模型请求")


@pytest.mark.asyncio
async def test_entry_provider_unavailable_returns_retryable_service_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_entry_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/v1/entries/{uuid.uuid4()}/candidate-insights",
            headers={
                "Authorization": "Bearer opaque-token",
                "X-Vault-ID": str(uuid.uuid4()),
                "Idempotency-Key": str(uuid.uuid4()),
                "If-Match": '"1"',
            },
            json={},
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["code"] == "MODEL_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_duplicate_fragment_request_is_rejected_before_runtime() -> None:
    fragment_id = uuid.uuid4()
    runtime = _Runtime(AssertionError("runtime must not be called"))

    response = await _post(
        _app(runtime),
        vault_id=uuid.uuid4(),
        key=uuid.uuid4(),
        fragment_ids=[fragment_id, fragment_id],
    )

    assert response.status_code == 422
    assert runtime.calls == []
