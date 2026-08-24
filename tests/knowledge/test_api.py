from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from life_coach.api.routers.memories import create_memory_router
from life_coach.modules.knowledge.contracts import (
    AuthorizationSnapshot,
    ClaimVersionView,
    CorrectionReplacement,
    EvidenceView,
    InboxItem,
    InboxPage,
    MemoryDetail,
    VerdictOutcome,
)
from life_coach.modules.knowledge.enums import (
    Attribution,
    AuthorizationPurpose,
    ClaimVersionOrigin,
    ConfidenceBand,
    CorrectionMode,
    DataClass,
    EpistemicType,
    EvidenceExtractionReason,
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
from life_coach.platform.errors import TRACE_HEADER, install_error_handlers

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
        data_class=DataClass.SENSITIVE,
        origin=ClaimVersionOrigin.PIPELINE_DERIVED,
        correction_mode=None,
        supersedes_derived_object_id=None,
        origin_verdict_id=None,
        normalized_fingerprint="f" * 64,
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
            source_document_id=uuid.uuid4(),
            source_revision_id=uuid.uuid4(),
            source_fragment_id=uuid.uuid4(),
            relation=EvidenceRelation.SUPPORTS,
            quote_start=0,
            quote_end=5,
            quote_hash="a" * 64,
            extractor_reason=EvidenceExtractionReason.EXPLICIT_STATEMENT,
            strength_band=EvidenceStrength.STRONG,
            source_recorded_at=NOW,
            source_data_class=DataClass.SENSITIVE,
            normalized_fingerprint="a" * 64,
            authorization_snapshot_id=uuid.uuid4(),
            policy_epoch=1,
            source_generation=1,
        )
        counter = EvidenceView(
            id=uuid.uuid4(),
            source_document_id=uuid.uuid4(),
            source_revision_id=uuid.uuid4(),
            source_fragment_id=uuid.uuid4(),
            relation=EvidenceRelation.CONTRADICTS,
            quote_start=0,
            quote_end=5,
            quote_hash="b" * 64,
            extractor_reason=EvidenceExtractionReason.CONTRADICTION,
            strength_band=EvidenceStrength.MODERATE,
            source_recorded_at=NOW,
            source_data_class=DataClass.SENSITIVE,
            normalized_fingerprint="b" * 64,
            authorization_snapshot_id=uuid.uuid4(),
            policy_epoch=1,
            source_generation=1,
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
            governance_verdict=None,
            data_class=DataClass.SENSITIVE,
            authorization_snapshot=AuthorizationSnapshot(
                snapshot_id=uuid.uuid4(),
                vault_id=uuid.UUID(int=0),
                purpose=AuthorizationPurpose.MEMORY_REVIEW,
                policy_epoch=1,
                source_generation=1,
                data_class=DataClass.SENSITIVE,
                allows_read=True,
                allows_proactive=False,
            ),
            is_current=True,
            etag=self.etag,
            snapshot_token=None,
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
        replacement: CorrectionReplacement | None = None,
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
                    "replacement": replacement,
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
    install_error_handlers(app)

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
    assert response.headers["cache-control"] == "private, no-store"
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
    assert response.headers["cache-control"] == "private, no-store"
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
    assert response.headers["cache-control"] == "private, no-store"
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
    assert response.json()["code"] == "REVISION_CONFLICT"
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert response.headers["cache-control"] == "private, no-store"
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
    assert response.json()["code"] == "MEMORY_NOT_FOUND"
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert "exists elsewhere" not in response.text


def test_correct_forwards_structured_replacement_without_reflecting_it() -> None:
    vault_id = uuid.uuid4()
    fake = FakeMemoryService()
    client = client_for(fake, vault_id)

    response = client.post(
        f"/v1/memories/{fake.memory_id}/verdicts",
        headers={"If-Match": fake.etag},
        json={
            "verdict": "correct",
            "replacement": {
                "statement": "I prefer an individual-contributor path.",
                "mode": "interpretation_error",
                "confidence_band": "medium",
            },
        },
    )

    assert response.status_code == 201
    replacement = fake.calls[-1][1]["replacement"]
    assert replacement is not None
    assert replacement.mode is CorrectionMode.INTERPRETATION_ERROR
    assert replacement.statement == "I prefer an individual-contributor path."
