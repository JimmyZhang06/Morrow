from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from life_coach.api.routers.actions import create_action_router
from life_coach.modules.action.lifecycle import (
    ReversibleActionState,
    ReversibleActionVerdict,
    ReversibleActionVerdictOutcome,
    ReversibleActionView,
)
from life_coach.platform.errors import install_error_handlers


class FakeActionService:
    def __init__(self) -> None:
        self.action_id = uuid.uuid4()
        self.memory_id = uuid.uuid4()
        self.revision = 1
        self.state = ReversibleActionState.PROPOSED

    def _view(self) -> ReversibleActionView:
        now = datetime(2026, 8, 24, tzinfo=UTC)
        return ReversibleActionView(
            action_id=self.action_id,
            memory_id=self.memory_id,
            source_derived_object_id=uuid.uuid4(),
            state=self.state,
            revision=self.revision,
            kind="reversible_experiment",
            title="Observe one example",
            description="Observe one example for ten minutes.",
            rationale="Test the memory at low cost.",
            exit_plan="Stop at any time.",
            estimated_minutes=10,
            is_reversible=True,
            template_version="memory-observation-v1",
            created_at=now,
            updated_at=now,
        )

    async def create_for_memory(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionView:
        del vault_id, idempotency_key
        self.memory_id = memory_id
        return self._view()

    async def get(
        self, *, vault_id: uuid.UUID, action_id: uuid.UUID
    ) -> ReversibleActionView:
        del vault_id
        assert action_id == self.action_id
        return self._view()

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        action_id: uuid.UUID,
        verdict: ReversibleActionVerdict,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionVerdictOutcome:
        del vault_id, idempotency_key
        assert action_id == self.action_id
        assert expected_revision == self.revision
        assert verdict is ReversibleActionVerdict.ACCEPT
        self.revision += 1
        self.state = ReversibleActionState.ACCEPTED
        return ReversibleActionVerdictOutcome(verdict_id=uuid.uuid4(), action=self._view())


@pytest.fixture
def api() -> tuple[FastAPI, FakeActionService, uuid.UUID]:
    app = FastAPI()
    # Error handlers are installed after contract debugging in production.
    install_error_handlers(app)
    service = FakeActionService()
    vault_id = uuid.uuid4()

    def get_service() -> FakeActionService:
        return service

    def get_vault_id() -> uuid.UUID:
        return vault_id

    app.include_router(
        create_action_router(get_service=get_service, get_vault_id=get_vault_id)
    )
    return app, service, vault_id


async def test_http_contract_uses_empty_create_body_and_etag_verdicts(
    api: tuple[FastAPI, FakeActionService, uuid.UUID],
) -> None:
    app, service, _vault_id = api
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/v1/memories/{service.memory_id}/actions",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={},
        )
        assert created.status_code == 201, created.text
        assert created.json()["state"] == "proposed"
        assert created.headers["cache-control"] == "private, no-store"
        assert created.headers["etag"] == f'"action:{service.action_id}:1"'

        accepted = await client.post(
            f"/v1/actions/{service.action_id}/verdicts",
            headers={
                "Idempotency-Key": str(uuid.uuid4()),
                "If-Match": created.headers["etag"],
            },
            json={"verdict": "accept"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["state"] == "accepted"
        assert accepted.json()["revision"] == 2
        assert accepted.headers["etag"] == f'"action:{service.action_id}:2"'


async def test_http_contract_rejects_extra_create_fields(
    api: tuple[FastAPI, FakeActionService, uuid.UUID],
) -> None:
    app, service, _vault_id = api
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/memories/{service.memory_id}/actions",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"description": "model-controlled side effect"},
        )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
