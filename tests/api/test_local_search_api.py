from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from life_coach.api.local_search_composition import build_local_search_router
from life_coach.modules.identity import create_vault
from life_coach.modules.identity.models import Principal
from life_coach.platform.auth import AuthenticationDenied, VaultMembershipDenied
from life_coach.platform.errors import install_error_handlers
from life_coach.platform.model_registry import load_model_registry
from tests.application.test_local_search import PROTECTOR, entry
from tests.sources.test_source_service import session as source_session


@pytest.fixture
def session():
    load_model_registry()
    yield from source_session.__wrapped__()


async def test_first_use_permission_query_and_stale_permission_change(session):
    vault_id, principal_id = create_vault(session).id, uuid4()
    session.add(Principal(id=principal_id, issuer="synthetic", subject_fingerprint="a" * 64))
    session.flush()
    diary = entry(session, vault_id, "今天把项目做完了")

    class Sessions:
        async def run_sync(self, fn):
            return fn(session)

        @asynccontextmanager
        async def open(self, *, authorization, vault_id):
            if authorization != "Bearer synthetic":
                raise AuthenticationDenied("denied")
            if vault_id != diary.vault_id:
                raise VaultMembershipDenied("denied")
            with session.begin_nested():
                yield SimpleNamespace(
                    session=self,
                    context=SimpleNamespace(vault_id=vault_id, principal_id=principal_id,
                                            membership_generation=1),
                )

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(build_local_search_router(sessions=Sessions(), protector=PROTECTOR))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"X-Vault-ID": str(vault_id), "Authorization": "Bearer synthetic"},
    ) as client:
        status = await client.get("/v1/local-search/status")
        assert status.json()["policy_epoch"] == 0
        assert status.headers["cache-control"] == "private, no-store"
        assert (await client.get("/v1/local-search/index-job")).json() is None
        assert (await client.post("/v1/local-search/index-job")).status_code == 409
        grant = await client.post("/v1/local-search/permission", json={
            "enabled": True, "expected_policy_epoch": 0,
        })
        assert grant.status_code == 200
        assert grant.json()["policy_epoch"] > 0
        replay = await client.post("/v1/local-search/permission", json={
            "enabled": True, "expected_policy_epoch": 0,
        })
        assert replay.json() == grant.json()
        task = await client.post("/v1/local-search/index-job")
        assert task.status_code == 202
        assert (await client.post("/v1/local-search/index-job")).json() == task.json()
        cancel = await client.post(f"/v1/local-search/index-job/{task.json()['id']}/cancel")
        assert cancel.status_code == 204
        assert (await client.get("/v1/local-search/index-job")).json()["state"] == "canceled"
        stale = await client.post("/v1/local-search/permission", json={
            "enabled": False, "expected_policy_epoch": 0,
        })
        assert stale.status_code == 409
        assert (await client.post("/v1/local-search/rebuild")).json()["rebuilt"] == 1
        query = await client.post("/v1/local-search/query", json={"query": "项目"})
        assert query.json()["items"][0]["entry_id"] == str(diary.id)
        unauthorized = await client.post("/v1/local-search/query", json={"query": "项目"},
                                         headers={"Authorization": "Bearer wrong"})
        assert unauthorized.status_code == 401
        forbidden = await client.get(
            "/v1/local-search/status", headers={"X-Vault-ID": str(uuid4())},
        )
        assert forbidden.status_code == 404
