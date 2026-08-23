from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from life_coach.api.routers.memories import create_memory_router
from life_coach.modules.knowledge.contracts import (
    ClaimVersionView,
    EvidenceView,
    InboxItem,
    InboxPage,
    MemoryDetail,
    VerdictOutcome,
)
from life_coach.modules.knowledge.enums import (
    Attribution,
    ConfidenceBand,
    EpistemicType,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    ValidTimePrecision,
    VerdictType,
)
from life_coach.modules.knowledge.exceptions import (
    MemoryNotFoundError,
    RevisionConflictError,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def version(derived_id: uuid.UUID) -> ClaimVersionView:
    return ClaimVersionView(
        derived_object_id=derived_id,
        version_no=1,
        statement="You may be reconsidering management roles.",
        structured_payload={"conditional": True},
        epistemic_type=EpistemicType.INFERRED,
        attribution=Attribution.MODEL_HYPOTHESIS,
        uncertainty="One possible interpretation.",
        state=LifecycleState.CANDIDATE,
        valid_from=NOW,
        valid_to=None,
        valid_time_precision=ValidTimePrecision.DAY,
        valid_time_original="today",
        valid_timezone="Asia/Shanghai",
        system_from=NOW,
        system_to=None,
        confidence_band=ConfidenceBand.MEDIUM,
        pipeline_version="test",
    )


class FakeMemoryService:
    def __init__(self) -> None:
        self.memory_id = uuid.uuid4()
        self.derived_id = uuid.uuid4()
        self.etag = f'"cv:{self.derived_id}:1:0"'
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failure: Exception | None = None
        support = EvidenceView(
            id=uuid.uuid4(),
            source_fragment_id=uuid.uuid4(),
            relation=EvidenceRelation.SUPPORTS,
            quote_start=0,
            quote_end=5,
            quote_hash="a" * 64,
            extractor_reason="supported span",
            strength_band=EvidenceStrength.STRONG,
            source_recorded_at=NOW,
        )
        counter = EvidenceView(
            id=uuid.uuid4(),
            source_fragment_id=uuid.uuid4(),
            relation=EvidenceRelation.CONTRADICTS,
            quote_start=0,
            quote_end=5,
            quote_hash="b" * 64,
            extractor_reason="counter span",
            strength_band=EvidenceStrength.MODERATE,
            source_recorded_at=NOW,
        )
        current = version(self.derived_id)
        self.detail = MemoryDetail(
            memory_id=self.memory_id,
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            subject_entity_id=None,
            version=current,
            history=(current,),
            evidence=(support,),
            counterevidence=(counter,),
            contextual_evidence=(),
            verdicts=(),
            current_verdict=None,
            etag=self.etag,
            allowed_uses=("review_only", "answer_when_asked"),
        )

    async def list_inbox(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage:
        self.calls.append(("list_inbox", {"vault_id": vault_id, "limit": limit, "cursor": cursor}))
        return InboxPage(
            items=(
                InboxItem(
                    memory_id=self.memory_id,
                    kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
                    version=self.detail.version,
                    support_count=1,
                    counterevidence_count=1,
                    current_verdict=None,
                    etag=self.etag,
                ),
            ),
            next_cursor=None,
        )

    async def get_detail(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime | None = None,
        system_at: datetime | None = None,
    ) -> MemoryDetail:
        self.calls.append(
            (
                "get_detail",
                {
                    "vault_id": vault_id,
                    "memory_id": memory_id,
                    "real_at": real_at,
                    "system_at": system_at,
                },
            )
        )
        if self.failure is not None:
            raise self.failure
        return self.detail

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        correction_text: str | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome:
        self.calls.append(
            (
                "record_verdict",
                {
                    "vault_id": vault_id,
                    "memory_id": memory_id,
                    "verdict": verdict,
                    "expected_etag": expected_etag,
                    "correction_text": correction_text,
                    "reason": reason,
                },
            )
        )
        if self.failure is not None:
            raise self.failure
        return VerdictOutcome(
            verdict_id=uuid.uuid4(),
            memory_id=memory_id,
            target_derived_object_id=self.derived_id,
            current_derived_object_id=self.derived_id,
            state=LifecycleState.ACTIVE,
            version_no=1,
            etag=f'"cv:{self.derived_id}:1:1"',
        )


def client_for(fake: FakeMemoryService, vault_id: uuid.UUID) -> TestClient:
    app = FastAPI()

    def get_service() -> FakeMemoryService:
        return fake

    def get_vault_id() -> uuid.UUID:
        return vault_id

    app.include_router(create_memory_router(get_service=get_service, get_vault_id=get_vault_id))
    return TestClient(app)


def test_router_factory_injects_service_and_authenticated_vault() -> None:
    vault_id = uuid.uuid4()
    fake = FakeMemoryService()
    client = client_for(fake, vault_id)

    response = client.get("/v1/memory-inbox?limit=20")

    assert response.status_code == 200
    assert fake.calls == [("list_inbox", {"vault_id": vault_id, "limit": 20, "cursor": None})]
    body = response.json()
    assert body["items"][0]["memory_id"] == str(fake.memory_id)
    assert body["items"][0]["version"]["epistemic_type"] == "inferred"


def test_memory_detail_exposes_provenance_support_and_counterevidence() -> None:
    vault_id = uuid.uuid4()
    fake = FakeMemoryService()
    client = client_for(fake, vault_id)

    response = client.get(f"/v1/memories/{fake.memory_id}")

    assert response.status_code == 200
    assert response.headers["etag"] == fake.etag
    body = response.json()
    assert body["version"]["attribution"] == "model_hypothesis"
    assert body["version"]["uncertainty"] == "One possible interpretation."
    assert body["version"]["valid_time"]["from"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["evidence"][0]["relation"] == "supports"
    assert body["counterevidence"][0]["relation"] == "contradicts"
    assert "does not by itself establish objective truth" in body["source_semantics"]


def test_verdict_contract_forwards_if_match_and_returns_new_etag() -> None:
    vault_id = uuid.uuid4()
    fake = FakeMemoryService()
    client = client_for(fake, vault_id)

    response = client.post(
        f"/v1/memories/{fake.memory_id}/verdicts",
        headers={"If-Match": fake.etag},
        json={"verdict": "confirm"},
    )

    assert response.status_code == 201
    assert response.headers["etag"].endswith(':1:1"')
    call = fake.calls[-1]
    assert call[0] == "record_verdict"
    assert call[1]["vault_id"] == vault_id
    assert call[1]["expected_etag"] == fake.etag
    assert call[1]["verdict"] is VerdictType.CONFIRM


def test_stale_verdict_returns_safe_revision_conflict() -> None:
    fake = FakeMemoryService()
    fake.failure = RevisionConflictError("private old statement")
    client = client_for(fake, uuid.uuid4())

    response = client.post(
        f"/v1/memories/{fake.memory_id}/verdicts",
        headers={"If-Match": fake.etag},
        json={"verdict": "reject"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVISION_CONFLICT"
    assert "private old statement" not in response.text


def test_correct_requires_text_and_if_match() -> None:
    fake = FakeMemoryService()
    client = client_for(fake, uuid.uuid4())

    missing_text = client.post(
        f"/v1/memories/{fake.memory_id}/verdicts",
        headers={"If-Match": fake.etag},
        json={"verdict": "correct"},
    )
    missing_etag = client.post(
        f"/v1/memories/{fake.memory_id}/verdicts",
        json={"verdict": "confirm"},
    )

    assert missing_text.status_code == 422
    assert missing_etag.status_code == 422
    assert fake.calls == []


def test_not_found_does_not_reveal_cross_vault_existence() -> None:
    fake = FakeMemoryService()
    fake.failure = MemoryNotFoundError("exists elsewhere")
    client = client_for(fake, uuid.uuid4())

    response = client.get(f"/v1/memories/{fake.memory_id}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "MEMORY_NOT_FOUND"
    assert "exists elsewhere" not in response.text
