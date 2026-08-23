"""Dependency-injected HTTP contract for user-recorded source entries.

This module deliberately has no application-global ``router`` and imports no
session or authentication dependency. The composition root must supply both
the authenticated vault and an application service that protects/reveals
content at the storage boundary. A Source records what a user recorded; it is
not an assertion that the described event objectively happened.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from life_coach.modules.sources.exceptions import (
    RevisionConflict,
    SourceDeleted,
    SourceNotFound,
)

DataClassValue = Literal["normal", "sensitive", "highly_sensitive"]
EntrySourceTypeValue = Literal["note", "conversation"]
SourceTypeValue = Literal["note", "conversation", "audio", "image", "file", "import"]
ProcessingStateValue = Literal["ready", "pending", "partial", "failed", "delayed"]


class EntryCreateRequest(BaseModel):
    """Plaintext accepted at the API boundary for immediate protected storage."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, repr=False)
    captured_at: datetime | None = None
    memory_policy: str = Field(default="default", min_length=1, max_length=100)
    client_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=500, repr=False)
    source_type: EntrySourceTypeValue = "note"
    data_class: DataClassValue = "sensitive"

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("captured_at must include a timezone")
        return value


class EntryAppendRevisionRequest(BaseModel):
    """A replacement recording that creates, rather than overwrites, a revision."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, repr=False)
    expected_revision: int | None = Field(default=None, ge=1, strict=True)


class ProcessingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    state: ProcessingStateValue


class EntryWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    revision: int = Field(ge=1)
    saved: Literal[True]
    processing: ProcessingResponse


class EntryRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    revision: int = Field(ge=1)
    created_at: datetime
    content_mime: str
    language: str | None = None


class EntryReadResponse(BaseModel):
    """A user record and its current version; no truth/verification claim is implied."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    title: str | None = None
    source_type: SourceTypeValue
    data_class: DataClassValue
    captured_at: datetime | None = None
    capture_timezone: str
    content: str = Field(repr=False)
    revision: int = Field(ge=1)
    revision_id: UUID
    created_at: datetime
    processing: ProcessingResponse
    revisions: list[EntryRevisionResponse] = Field(default_factory=list)


class EntryPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    items: list[EntryReadResponse]
    next_cursor: str | None = None


class DeletionSinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    sink: str
    state: Literal["planned"]


class EntryDeleteResponse(BaseModel):
    """Immediate isolation plus a plan; this never claims physical erasure is done."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    tombstoned: Literal[True]
    source_generation: int = Field(ge=0)
    policy_epoch: int = Field(ge=0)
    cascade_state: Literal["planned"]
    sinks: list[DeletionSinkResponse]


@dataclass(frozen=True, slots=True)
class CreateEntryCommand:
    content: str = field(repr=False)
    captured_at: datetime | None
    memory_policy: str
    client_id: str
    title: str | None
    source_type: EntrySourceTypeValue
    data_class: DataClassValue


@dataclass(frozen=True, slots=True)
class AppendEntryRevisionCommand:
    content: str = field(repr=False)
    expected_revision: int


class SourceEntryService(Protocol):
    """Application port implemented by the future encryption/storage adapter.

    Implementations may be synchronous or asynchronous. They must not persist
    ``command.content`` as ciphertext without actually protecting it.
    """

    def create_entry(
        self,
        *,
        vault_id: UUID,
        command: CreateEntryCommand,
        idempotency_key: str | None,
    ) -> object: ...

    def list_entries(
        self,
        *,
        vault_id: UUID,
        cursor: str | None,
        limit: int,
        source_type: SourceTypeValue | None,
    ) -> object: ...

    def get_entry(self, *, vault_id: UUID, entry_id: UUID) -> object: ...

    def append_entry_revision(
        self,
        *,
        vault_id: UUID,
        entry_id: UUID,
        command: AppendEntryRevisionCommand,
        idempotency_key: str | None,
    ) -> object: ...

    def delete_entry(
        self,
        *,
        vault_id: UUID,
        entry_id: UUID,
        expected_revision: int | None,
        idempotency_key: str | None,
    ) -> object: ...


async def _resolve(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_service(method: Callable[..., object], **kwargs: object) -> object:
    """Call async ports directly and keep synchronous ports off the event loop."""

    if inspect.iscoroutinefunction(method):
        return await _resolve(method(**kwargs))
    return await _resolve(await run_in_threadpool(method, **kwargs))


def _parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == '"':
        candidate = candidate[1:-1]
    try:
        revision = int(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match must contain a revision number.",
        ) from exc
    if revision < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match must contain a positive revision number.",
        )
    return revision


def _expected_revision(body_revision: int | None, if_match: str | None) -> int:
    header_revision = _parse_if_match(if_match)
    if body_revision is None and header_revision is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="A current revision is required to append a revision.",
        )
    if (
        body_revision is not None
        and header_revision is not None
        and body_revision != header_revision
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The revision preconditions disagree.",
        )
    if body_revision is not None:
        return body_revision
    if header_revision is None:  # narrowed above; kept explicit for type checkers
        raise AssertionError("unreachable")
    return header_revision


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found.")


def _conflict(error: RevisionConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "type": "https://product.example/problems/revision-conflict",
            "title": "The entry changed on another writer",
            "status": status.HTTP_409_CONFLICT,
            "code": "REVISION_CONFLICT",
            "safe_detail": "Refresh the entry before retrying.",
            "current_revision": error.current_revision,
        },
    )


def create_sources_router(
    *,
    get_vault_id: Callable[..., UUID],
    get_source_service: Callable[..., SourceEntryService],
    prefix: str = "/v1/entries",
) -> APIRouter:
    """Build the Source router without importing a platform composition root."""

    router = APIRouter(prefix=prefix, tags=["entries"])
    VaultDependency = Annotated[UUID, Depends(get_vault_id)]
    ServiceDependency = Annotated[SourceEntryService, Depends(get_source_service)]
    IdempotencyDependency = Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r".*\S.*"),
    ]
    IfMatchDependency = Annotated[str | None, Header(alias="If-Match")]

    @router.post("", response_model=EntryWriteResponse, status_code=status.HTTP_201_CREATED)
    async def create_entry(
        payload: EntryCreateRequest,
        response: Response,
        vault_id: VaultDependency,
        service: ServiceDependency,
        idempotency_key: IdempotencyDependency = None,
    ) -> EntryWriteResponse:
        command = CreateEntryCommand(**payload.model_dump())
        try:
            raw_result = await _call_service(
                service.create_entry,
                vault_id=vault_id,
                command=command,
                idempotency_key=idempotency_key,
            )
        except (SourceNotFound, SourceDeleted) as exc:
            raise _not_found() from exc
        result = EntryWriteResponse.model_validate(raw_result)
        response.headers["ETag"] = f'"{result.revision}"'
        return result

    @router.get("", response_model=EntryPageResponse)
    async def list_entries(
        vault_id: VaultDependency,
        service: ServiceDependency,
        cursor: Annotated[str | None, Query(max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        source_type: Annotated[SourceTypeValue | None, Query()] = None,
    ) -> EntryPageResponse:
        raw_result = await _call_service(
            service.list_entries,
            vault_id=vault_id,
            cursor=cursor,
            limit=limit,
            source_type=source_type,
        )
        return EntryPageResponse.model_validate(raw_result)

    @router.get("/{entry_id}", response_model=EntryReadResponse)
    async def get_entry(
        entry_id: UUID,
        response: Response,
        vault_id: VaultDependency,
        service: ServiceDependency,
    ) -> EntryReadResponse:
        try:
            raw_result = await _call_service(
                service.get_entry,
                vault_id=vault_id,
                entry_id=entry_id,
            )
        except (SourceNotFound, SourceDeleted) as exc:
            raise _not_found() from exc
        result = EntryReadResponse.model_validate(raw_result)
        response.headers["ETag"] = f'"{result.revision}"'
        return result

    @router.patch("/{entry_id}", response_model=EntryWriteResponse)
    async def append_entry_revision(
        entry_id: UUID,
        payload: EntryAppendRevisionRequest,
        response: Response,
        vault_id: VaultDependency,
        service: ServiceDependency,
        if_match: IfMatchDependency = None,
        idempotency_key: IdempotencyDependency = None,
    ) -> EntryWriteResponse:
        expected_revision = _expected_revision(payload.expected_revision, if_match)
        command = AppendEntryRevisionCommand(
            content=payload.content,
            expected_revision=expected_revision,
        )
        try:
            raw_result = await _call_service(
                service.append_entry_revision,
                vault_id=vault_id,
                entry_id=entry_id,
                command=command,
                idempotency_key=idempotency_key,
            )
        except (SourceNotFound, SourceDeleted) as exc:
            raise _not_found() from exc
        except RevisionConflict as exc:
            raise _conflict(exc) from exc
        result = EntryWriteResponse.model_validate(raw_result)
        response.headers["ETag"] = f'"{result.revision}"'
        return result

    @router.delete(
        "/{entry_id}",
        response_model=EntryDeleteResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def delete_entry(
        entry_id: UUID,
        vault_id: VaultDependency,
        service: ServiceDependency,
        if_match: IfMatchDependency = None,
        idempotency_key: IdempotencyDependency = None,
    ) -> EntryDeleteResponse:
        try:
            raw_result = await _call_service(
                service.delete_entry,
                vault_id=vault_id,
                entry_id=entry_id,
                expected_revision=_parse_if_match(if_match),
                idempotency_key=idempotency_key,
            )
        except (SourceNotFound, SourceDeleted) as exc:
            raise _not_found() from exc
        except RevisionConflict as exc:
            raise _conflict(exc) from exc
        return EntryDeleteResponse.model_validate(raw_result)

    return router


# Conventional aliases make the factory discoverable without creating a global
# router that would need unavailable platform dependencies.
create_router = create_sources_router
build_router = create_sources_router


# Compatibility names for callers that use command-first naming.
CreateEntryRequest = EntryCreateRequest
AppendEntryRevisionRequest = EntryAppendRevisionRequest
CreateEntryResponse = EntryWriteResponse
