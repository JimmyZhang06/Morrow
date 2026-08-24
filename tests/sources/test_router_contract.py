from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from life_coach.api.routers.sources import AppendEntryRevisionCommand, create_sources_router
from life_coach.jobs.contracts import IdempotencyConflict
from life_coach.modules.sources.exceptions import (
    InvalidSourceData,
    RevisionConflict,
    SourceDeleted,
    SourceNotFound,
)
from life_coach.platform.errors import TRACE_HEADER, install_error_handlers


class FakeEntryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.entry_id = uuid4()
        self.revision_id = uuid4()
        self.missing = False
        self.deleted = False
        self.conflict = False
        self.invalid = False
        self.idempotency_conflict = False

    def create_entry(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create", kwargs))
        if self.invalid:
            raise InvalidSourceData("private source content must not be reflected")
        if self.idempotency_conflict:
            raise IdempotencyConflict("private request fingerprint must not be reflected")
        return self._write_result(1)

    def list_entries(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list", kwargs))
        return {"items": [self._read_result()], "next_cursor": None}

    def get_entry(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get", kwargs))
        if self.missing:
            raise SourceNotFound
        if self.deleted:
            raise SourceDeleted
        return self._read_result()

    def append_entry_revision(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("append", kwargs))
        if self.conflict:
            raise RevisionConflict(expected_revision=1, current_revision=3)
        return self._write_result(2)

    def delete_entry(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("delete", kwargs))
        return {
            "id": self.entry_id,
            "tombstoned": True,
            "source_generation": 3,
            "policy_epoch": 1,
            "cascade_state": "planned",
            "sinks": [{"sink": "postgres_source", "state": "planned"}],
        }

    def _write_result(self, revision: int) -> dict[str, object]:
        return {
            "id": self.entry_id,
            "revision": revision,
            "saved": True,
            "processing": {"state": "pending"},
        }

    def _read_result(self) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "id": self.entry_id,
            "title": None,
            "source_type": "note",
            "data_class": "sensitive",
            "captured_at": now,
            "capture_timezone": "Asia/Shanghai",
            "content": "用户记录的内容",
            "revision": 1,
            "revision_id": self.revision_id,
            "created_at": now,
            "processing": {"state": "pending"},
            "revisions": [
                {
                    "id": self.revision_id,
                    "revision": 1,
                    "created_at": now,
                    "content_mime": "text/plain",
                    "language": "zh-CN",
                }
            ],
        }


def make_client(vault_id: UUID, service: FakeEntryService) -> tuple[TestClient, FakeEntryService]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_sources_router(
            get_vault_id=lambda: vault_id,
            get_source_service=lambda: service,
        )
    )
    return TestClient(app), service


def test_create_uses_injected_vault_and_forbids_client_vault_override() -> None:
    vault_id = uuid4()
    client, service = make_client(vault_id, FakeEntryService())
    payload = {
        "content": "我今天记录了一件事",
        "captured_at": "2026-08-23T20:10:00+08:00",
        "memory_policy": "default",
        "client_id": "local-uuid",
    }

    response = client.post(
        "/v1/entries",
        json=payload,
        headers={"Idempotency-Key": "entry-create-1"},
    )

    assert response.status_code == 201
    assert response.json()["revision"] == 1
    assert response.headers["etag"] == '"1"'
    call_name, call = service.calls[-1]
    assert call_name == "create"
    assert call["vault_id"] == vault_id
    assert call["idempotency_key"] == "entry-create-1"

    rejected = client.post(
        "/v1/entries",
        json={**payload, "vault_id": str(uuid4())},
        headers={"Idempotency-Key": "entry-create-2"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "REQUEST_VALIDATION_ERROR"


def test_read_append_and_delete_contracts() -> None:
    vault_id = uuid4()
    client, service = make_client(vault_id, FakeEntryService())

    read = client.get(f"/v1/entries/{service.entry_id}")
    assert read.status_code == 200
    assert read.json()["content"] == "用户记录的内容"
    assert read.headers["etag"] == '"1"'

    appended = client.patch(
        f"/v1/entries/{service.entry_id}",
        json={"content": "这是追加的新版本"},
        headers={"If-Match": '"1"', "Idempotency-Key": "entry-append-1"},
    )
    assert appended.status_code == 200
    assert appended.json()["revision"] == 2
    append_command = service.calls[-1][1]["command"]
    assert isinstance(append_command, AppendEntryRevisionCommand)
    assert append_command.expected_revision == 1
    assert "这是追加的新版本" not in repr(append_command)

    deleted = client.delete(
        f"/v1/entries/{service.entry_id}",
        headers={"If-Match": 'W/"2"', "Idempotency-Key": "entry-delete-1"},
    )
    assert deleted.status_code == 202
    assert deleted.json()["cascade_state"] == "planned"
    assert deleted.json()["tombstoned"] is True
    assert service.calls[-1][1]["expected_revision"] == 2


def test_absent_cross_vault_and_tombstoned_entries_share_safe_404() -> None:
    client, service = make_client(uuid4(), FakeEntryService())
    service.missing = True

    response = client.get(f"/v1/entries/{service.entry_id}")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]

    service.missing = False
    service.deleted = True
    tombstoned = client.get(f"/v1/entries/{service.entry_id}")
    assert tombstoned.status_code == 404
    assert tombstoned.json()["code"] == response.json()["code"]
    assert tombstoned.json()["safe_detail"] == response.json()["safe_detail"]


def test_append_requires_a_revision_precondition() -> None:
    client, service = make_client(uuid4(), FakeEntryService())

    response = client.patch(
        f"/v1/entries/{service.entry_id}",
        json={"content": "new version"},
        headers={"Idempotency-Key": "entry-append-precondition-1"},
    )

    assert response.status_code == 428
    assert service.calls == []

    coerced_boolean = client.patch(
        f"/v1/entries/{service.entry_id}",
        json={"content": "new version", "expected_revision": True},
        headers={"Idempotency-Key": "entry-append-precondition-2"},
    )
    assert coerced_boolean.status_code == 422
    assert service.calls == []


def test_revision_conflict_reports_the_mergeable_current_revision() -> None:
    client, service = make_client(uuid4(), FakeEntryService())
    service.conflict = True

    response = client.patch(
        f"/v1/entries/{service.entry_id}",
        json={"content": "conflicting version", "expected_revision": 1},
        headers={"Idempotency-Key": "entry-append-conflict-1"},
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "REVISION_CONFLICT"
    assert response.json()["current_revision"] == 3
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]


def test_list_rejects_unknown_source_type_before_calling_service() -> None:
    client, service = make_client(uuid4(), FakeEntryService())

    response = client.get("/v1/entries?source_type=untrusted")

    assert response.status_code == 422
    assert service.calls == []


def test_write_endpoints_require_nonblank_idempotency_key() -> None:
    client, service = make_client(uuid4(), FakeEntryService())
    create_payload = {"content": "new entry", "client_id": "client-1"}

    create = client.post("/v1/entries", json=create_payload)
    append = client.patch(
        f"/v1/entries/{service.entry_id}",
        json={"content": "new revision"},
        headers={"If-Match": '"1"'},
    )
    delete = client.delete(
        f"/v1/entries/{service.entry_id}",
        headers={"If-Match": '"1"', "Idempotency-Key": "   "},
    )

    for response in (create, append, delete):
        assert response.status_code == 422
        assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
        assert response.headers["content-type"].startswith("application/problem+json")
    assert service.calls == []


def test_memory_policy_is_defaulted_and_unknown_policies_are_rejected() -> None:
    client, service = make_client(uuid4(), FakeEntryService())

    accepted = client.post(
        "/v1/entries",
        json={"content": "new entry", "client_id": "client-1"},
        headers={"Idempotency-Key": "entry-default-policy"},
    )

    assert accepted.status_code == 201
    command = service.calls[-1][1]["command"]
    assert command.memory_policy == "default"

    service.calls.clear()
    rejected = client.post(
        "/v1/entries",
        json={
            "content": "private payload",
            "client_id": "client-2",
            "memory_policy": "forever",
        },
        headers={"Idempotency-Key": "entry-invalid-policy"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert "private payload" not in rejected.text
    assert service.calls == []


def test_invalid_source_data_uses_safe_problem_contract() -> None:
    client, service = make_client(uuid4(), FakeEntryService())
    service.invalid = True

    response = client.post(
        "/v1/entries",
        json={"content": "private input", "client_id": "client-1"},
        headers={"Idempotency-Key": "entry-invalid-source"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SOURCE_COMMAND"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert "private source content" not in response.text
    assert "private input" not in response.text


def test_idempotency_conflict_uses_safe_problem_contract() -> None:
    client, service = make_client(uuid4(), FakeEntryService())
    service.idempotency_conflict = True

    response = client.post(
        "/v1/entries",
        json={"content": "private input", "client_id": "client-1"},
        headers={"Idempotency-Key": "private-idempotency-key"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["trace_id"] == response.headers[TRACE_HEADER]
    assert "private request fingerprint" not in response.text
    assert "private-idempotency-key" not in response.text
    assert "private input" not in response.text
