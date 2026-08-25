"""Authenticated Source entry application service with protected content and replay receipts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as AnySession

from life_coach.jobs.payloads import JsonValue, VaultRequestFingerprint, canonical_request_hash
from life_coach.modules.identity.models import CreatedBy, DataClass
from life_coach.modules.identity.service import get_vault
from life_coach.modules.knowledge.contracts import CorrectionSourceAnchor
from life_coach.modules.knowledge.enums import DataClass as KnowledgeDataClass
from life_coach.modules.sources.command_receipts import (
    SourceCommandReceiptRepository,
    SourceCommandReceiptSpec,
    SourceCommandReservation,
)
from life_coach.modules.sources.exceptions import InvalidSourceData, SourceNotFound
from life_coach.modules.sources.models import (
    FragmentKind,
    ProcessingState,
    SourceDocument,
    SourceRevision,
    SourceType,
)
from life_coach.modules.sources.service import (
    DELETION_SINKS,
    append_source_revision,
    create_source_document,
    create_source_fragment,
    get_source_document,
    get_source_revision,
    list_source_documents,
    list_source_revisions,
    read_source_document,
    tombstone_source_document,
)
from life_coach.platform.database import VaultAsyncSession

EntrySourceTypeValue = Literal["note", "conversation"]
SourceTypeValue = Literal[
    "note", "conversation", "audio", "image", "file", "import", "correction"
]
DataClassValue = Literal["normal", "sensitive", "highly_sensitive"]

_ENVELOPE_PREFIX = b"life-coach/source/aes-gcm/v1\x00"
_NONCE_BYTES = 12
_CURSOR_DOMAIN = b"life-coach/source-cursor/v1\x00"


class SourceContentUnavailable(RuntimeError):
    """Protected Source content could not be opened or verified."""


class SourceContentProtector(Protocol):
    """Protect one pre-identified immutable Source revision or fragment."""

    def seal(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        object_id: uuid.UUID,
        revision_no: int,
        kind: Literal["revision", "fragment"],
        plaintext: str,
    ) -> bytes: ...

    def open(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        object_id: uuid.UUID,
        revision_no: int,
        kind: Literal["revision", "fragment"],
        ciphertext: bytes,
    ) -> str: ...


class LocalAesGcmSourceContentProtector:
    """Versioned local AEAD adapter for development and synthetic canaries.

    Production must inject a KMS/object-store-backed adapter instead of building
    this class from application settings. The envelope uses a random nonce and
    AAD-bound Vault/document/object/revision identities to prevent ciphertext
    substitution across authoritative rows.
    """

    __slots__ = ("_master_key",)

    def __init__(self, master_key: bytes) -> None:
        if not isinstance(master_key, bytes) or len(master_key) < 32:
            raise ValueError("Source content key must contain at least 32 bytes")
        self._master_key = bytes(master_key)

    def seal(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        object_id: uuid.UUID,
        revision_no: int,
        kind: Literal["revision", "fragment"],
        plaintext: str,
    ) -> bytes:
        if not isinstance(plaintext, str) or not plaintext:
            raise InvalidSourceData("Source content must be non-empty text")
        nonce = os.urandom(_NONCE_BYTES)
        payload = AESGCM(self._vault_key(vault_id)).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            self._aad(vault_id, document_id, object_id, revision_no, kind),
        )
        return _ENVELOPE_PREFIX + nonce + payload

    def open(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        object_id: uuid.UUID,
        revision_no: int,
        kind: Literal["revision", "fragment"],
        ciphertext: bytes,
    ) -> str:
        if not isinstance(ciphertext, bytes) or not ciphertext.startswith(_ENVELOPE_PREFIX):
            raise SourceContentUnavailable("Source content is unavailable")
        body = ciphertext[len(_ENVELOPE_PREFIX) :]
        if len(body) <= _NONCE_BYTES:
            raise SourceContentUnavailable("Source content is unavailable")
        nonce, payload = body[:_NONCE_BYTES], body[_NONCE_BYTES:]
        try:
            plaintext = AESGCM(self._vault_key(vault_id)).decrypt(
                nonce,
                payload,
                self._aad(vault_id, document_id, object_id, revision_no, kind),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            raise SourceContentUnavailable("Source content is unavailable") from None

    def _vault_key(self, vault_id: uuid.UUID) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=vault_id.bytes,
            info=b"life-coach/source-vault-key/v1",
        ).derive(self._master_key)

    @staticmethod
    def _aad(
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        object_id: uuid.UUID,
        revision_no: int,
        kind: str,
    ) -> bytes:
        if revision_no < 1 or kind not in {"revision", "fragment"}:
            raise ValueError("invalid Source content identity")
        return b"\x00".join(
            (
                b"life-coach/source-content/v1",
                vault_id.bytes,
                document_id.bytes,
                object_id.bytes,
                str(revision_no).encode("ascii"),
                kind.encode("ascii"),
            )
        )


class ProtectedSourceFragmentPlaintextReader:
    """Open one authoritative fragment with the complete persisted AEAD identity."""

    __slots__ = ("_protector",)

    def __init__(self, protector: SourceContentProtector) -> None:
        self._protector = protector

    def read_text(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_no: int,
        fragment_id: uuid.UUID,
        ciphertext: bytes,
    ) -> str:
        if revision_id.int == 0:
            raise SourceContentUnavailable("Source content is unavailable")
        return self._protector.open(
            vault_id=vault_id,
            document_id=document_id,
            object_id=fragment_id,
            revision_no=revision_no,
            kind="fragment",
            ciphertext=ciphertext,
        )


class ProtectedCorrectionSourceRecorder:
    """Persist a user correction as encrypted Source evidence in the caller's UoW."""

    __slots__ = ("_protector",)

    def __init__(self, protector: SourceContentProtector) -> None:
        self._protector = protector

    def record_correction(
        self,
        *,
        session: AnySession,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        correction_text: str,
        data_class: KnowledgeDataClass,
        recorded_at: datetime,
    ) -> CorrectionSourceAnchor:
        del memory_id
        document_id, revision_id, fragment_id = (uuid.uuid4() for _ in range(3))
        identity_class = DataClass(data_class.value)
        content_hash = hashlib.sha256(correction_text.encode("utf-8")).hexdigest()
        revision_ciphertext = self._protector.seal(
            vault_id=vault_id,
            document_id=document_id,
            object_id=revision_id,
            revision_no=1,
            kind="revision",
            plaintext=correction_text,
        )
        fragment_ciphertext = self._protector.seal(
            vault_id=vault_id,
            document_id=document_id,
            object_id=fragment_id,
            revision_no=1,
            kind="fragment",
            plaintext=correction_text,
        )
        result = create_source_document(
            session,
            vault_id=vault_id,
            document_id=document_id,
            revision_id=revision_id,
            content_ciphertext=revision_ciphertext,
            content_hash=content_hash,
            content_mime="text/plain; charset=utf-8",
            title=None,
            event_time_hint=recorded_at,
            capture_timezone="UTC",
            processing_state=ProcessingState.READY,
            source_type=SourceType.CORRECTION,
            created_by=CreatedBy.USER,
            data_class=identity_class,
        )
        fragment = create_source_fragment(
            session,
            vault_id=vault_id,
            fragment_id=fragment_id,
            revision_id=result.revision.id,
            ordinal=0,
            char_start=0,
            char_end=len(correction_text),
            text_ciphertext=fragment_ciphertext,
            text_hash=content_hash,
            fragment_kind=FragmentKind.PARAGRAPH,
            created_by=CreatedBy.USER,
            data_class=identity_class,
        )
        return CorrectionSourceAnchor(source_fragment_id=fragment.id)


@dataclass(frozen=True, slots=True)
class CreateEntryCommand:
    content: str = field(repr=False)
    captured_at: datetime | None
    memory_policy: Literal["default"]
    client_id: str
    title: str | None = field(repr=False)
    source_type: EntrySourceTypeValue
    data_class: DataClassValue


@dataclass(frozen=True, slots=True)
class AppendEntryRevisionCommand:
    content: str = field(repr=False)
    expected_revision: int


class SourceEntryService(Protocol):
    async def create_entry(
        self,
        *,
        vault_id: uuid.UUID,
        command: CreateEntryCommand,
        idempotency_key: str,
    ) -> object: ...

    async def list_entries(
        self,
        *,
        vault_id: uuid.UUID,
        cursor: str | None,
        limit: int,
        source_type: SourceTypeValue | None,
    ) -> object: ...

    async def get_entry(self, *, vault_id: uuid.UUID, entry_id: uuid.UUID) -> object: ...

    async def append_entry_revision(
        self,
        *,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        command: AppendEntryRevisionCommand,
        idempotency_key: str,
    ) -> object: ...

    async def delete_entry(
        self,
        *,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> object: ...


type SourceReceiptFactory = Callable[
    [AsyncSession, uuid.UUID],
    SourceCommandReceiptRepository,
]


class SourceCursorCodec:
    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("Source cursor key must contain at least 32 bytes")
        self._key = bytes(key)

    def encode(self, vault_id: uuid.UUID, document: SourceDocument) -> str:
        payload = json.dumps(
            {
                "vault_id": str(vault_id),
                "created_at": document.created_at.astimezone(UTC).isoformat(),
                "document_id": str(document.id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._key, _CURSOR_DOMAIN + payload, hashlib.sha256).digest()
        return self._b64(payload) + "." + self._b64(signature)

    def decode(self, vault_id: uuid.UUID, token: str) -> tuple[datetime, uuid.UUID]:
        try:
            payload_part, signature_part = token.split(".", 1)
            payload = self._unb64(payload_part)
            signature = self._unb64(signature_part)
            expected = hmac.new(self._key, _CURSOR_DOMAIN + payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = json.loads(payload)
            if value["vault_id"] != str(vault_id):
                raise ValueError
            created_at = datetime.fromisoformat(value["created_at"])
            document_id = uuid.UUID(value["document_id"])
            if created_at.tzinfo is None:
                raise ValueError
            return created_at.astimezone(UTC), document_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise InvalidSourceData("Source cursor is invalid") from None

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PostgresSourceEntryService:
    """Map HTTP entry commands to the synchronous Source domain in one Vault transaction."""

    def __init__(
        self,
        *,
        session: VaultAsyncSession,
        vault_id: uuid.UUID,
        protector: SourceContentProtector,
        hmac_key: bytes,
        receipt_factory: SourceReceiptFactory = SourceCommandReceiptRepository,
    ) -> None:
        if len(hmac_key) < 32:
            raise ValueError("Source receipt key must contain at least 32 bytes")
        self._session = session
        self._vault_id = vault_id
        self._protector = protector
        self._hmac_key = bytes(hmac_key)
        self._receipts = receipt_factory(session, vault_id)
        self._cursors = SourceCursorCodec(hmac_key)

    def _require_vault(self, vault_id: uuid.UUID) -> None:
        if vault_id != self._vault_id:
            raise SourceNotFound("Source is unavailable")

    async def create_entry(
        self,
        *,
        vault_id: uuid.UUID,
        command: CreateEntryCommand,
        idempotency_key: str,
    ) -> object:
        self._require_vault(vault_id)
        if command.title is not None:
            raise InvalidSourceData("encrypted Source titles are not available yet")
        request_hash = self._request_hash(
            vault_id,
            {
                "domain": "source.create.v1",
                "content": command.content,
                "captured_at": (
                    command.captured_at.isoformat() if command.captured_at is not None else None
                ),
                "memory_policy": command.memory_policy,
                "client_id": command.client_id,
                "title": command.title,
                "source_type": command.source_type,
                "data_class": command.data_class,
            },
        )
        client_key_hash = self._client_key_hash(vault_id, idempotency_key)
        replay = await self._receipts.find(
            operation="source.create",
            client_key_hash=client_key_hash,
            request_hash=request_hash,
        )
        if replay is not None:
            return await self._write_replay(vault_id, replay)
        document_id, revision_id, fragment_id = (uuid.uuid4() for _ in range(3))
        policy_epoch, source_generation = await self._locked_fences(vault_id)
        reservation = await self._receipts.reserve(
            SourceCommandReceiptSpec(
                vault_id=vault_id,
                operation="source.create",
                client_key_hash=client_key_hash,
                request_hash=request_hash,
                resource_id=document_id,
                resource_revision_id=revision_id,
                result_revision_no=1,
                source_generation=source_generation + 1,
                policy_epoch=policy_epoch,
            )
        )
        if not reservation.created:
            return await self._write_replay(vault_id, reservation)

        revision_ciphertext = self._seal(
            vault_id, document_id, revision_id, 1, "revision", command.content
        )
        fragment_ciphertext = self._seal(
            vault_id, document_id, fragment_id, 1, "fragment", command.content
        )
        content_hash = hashlib.sha256(command.content.encode("utf-8")).hexdigest()
        capture_timezone = (
            str(command.captured_at.tzinfo) if command.captured_at is not None else "UTC"
        )

        def write(session: object) -> dict[str, object]:
            result = create_source_document(
                cast(AnySession, session),
                vault_id=vault_id,
                document_id=document_id,
                revision_id=revision_id,
                content_ciphertext=revision_ciphertext,
                content_hash=content_hash,
                content_mime="text/plain; charset=utf-8",
                source_type=command.source_type,
                title=None,
                event_time_hint=command.captured_at,
                capture_timezone=capture_timezone,
                processing_state=ProcessingState.PENDING,
                data_class=DataClass(command.data_class),
            )
            create_source_fragment(
                cast(AnySession, session),
                vault_id=vault_id,
                fragment_id=fragment_id,
                revision_id=revision_id,
                ordinal=0,
                char_start=0,
                char_end=len(command.content),
                text_ciphertext=fragment_ciphertext,
                text_hash=content_hash,
                fragment_kind=FragmentKind.PARAGRAPH,
                data_class=DataClass(command.data_class),
            )
            return self._write_dto(result.document, result.revision)

        return await self._session.run_sync(write)

    async def append_entry_revision(
        self,
        *,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        command: AppendEntryRevisionCommand,
        idempotency_key: str,
    ) -> object:
        self._require_vault(vault_id)
        request_hash = self._request_hash(
            vault_id,
            {
                "domain": "source.append.v1",
                "entry_id": str(entry_id),
                "expected_revision": command.expected_revision,
                "content": command.content,
            },
        )
        client_key_hash = self._client_key_hash(vault_id, idempotency_key)
        replay = await self._receipts.find(
            operation="source.append",
            client_key_hash=client_key_hash,
            request_hash=request_hash,
        )
        if replay is not None:
            return await self._write_replay(vault_id, replay)
        revision_id, fragment_id = uuid.uuid4(), uuid.uuid4()
        policy_epoch, source_generation = await self._locked_fences(vault_id)
        reservation = await self._receipts.reserve(
            SourceCommandReceiptSpec(
                vault_id=vault_id,
                operation="source.append",
                client_key_hash=client_key_hash,
                request_hash=request_hash,
                resource_id=entry_id,
                resource_revision_id=revision_id,
                result_revision_no=command.expected_revision + 1,
                source_generation=source_generation + 1,
                policy_epoch=policy_epoch,
            )
        )
        if not reservation.created:
            return await self._write_replay(vault_id, reservation)

        revision_no = command.expected_revision + 1
        revision_ciphertext = self._seal(
            vault_id, entry_id, revision_id, revision_no, "revision", command.content
        )
        fragment_ciphertext = self._seal(
            vault_id, entry_id, fragment_id, revision_no, "fragment", command.content
        )
        content_hash = hashlib.sha256(command.content.encode("utf-8")).hexdigest()

        def write(session: object) -> dict[str, object]:
            result = append_source_revision(
                cast(AnySession, session),
                vault_id=vault_id,
                document_id=entry_id,
                revision_id=revision_id,
                expected_revision=command.expected_revision,
                content_ciphertext=revision_ciphertext,
                content_hash=content_hash,
                content_mime="text/plain; charset=utf-8",
            )
            result.document.processing_state = ProcessingState.PENDING
            create_source_fragment(
                cast(AnySession, session),
                vault_id=vault_id,
                fragment_id=fragment_id,
                revision_id=revision_id,
                ordinal=0,
                char_start=0,
                char_end=len(command.content),
                text_ciphertext=fragment_ciphertext,
                text_hash=content_hash,
                fragment_kind=FragmentKind.PARAGRAPH,
                data_class=result.revision.data_class,
            )
            cast(AnySession, session).flush()
            return self._write_dto(result.document, result.revision)

        return await self._session.run_sync(write)

    async def get_entry(self, *, vault_id: uuid.UUID, entry_id: uuid.UUID) -> object:
        self._require_vault(vault_id)
        return await self._session.run_sync(
            lambda session: self._read_dto(session, vault_id=vault_id, entry_id=entry_id)
        )

    async def list_entries(
        self,
        *,
        vault_id: uuid.UUID,
        cursor: str | None,
        limit: int,
        source_type: SourceTypeValue | None,
    ) -> object:
        self._require_vault(vault_id)
        before_created_at = before_id = None
        if cursor is not None:
            before_created_at, before_id = self._cursors.decode(vault_id, cursor)

        def read(session: object) -> dict[str, object]:
            documents = list_source_documents(
                cast(AnySession, session),
                vault_id=vault_id,
                limit=limit + 1,
                before_created_at=before_created_at,
                before_id=before_id,
                source_type=source_type,
            )
            page, has_more = documents[:limit], len(documents) > limit
            return {
                "items": [
                    self._read_dto(
                        session,
                        vault_id=vault_id,
                        entry_id=document.id,
                    )
                    for document in page
                ],
                "next_cursor": (
                    self._cursors.encode(vault_id, page[-1]) if has_more and page else None
                ),
            }

        return await self._session.run_sync(read)

    async def delete_entry(
        self,
        *,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> object:
        self._require_vault(vault_id)
        request_hash = self._request_hash(
            vault_id,
            {
                "domain": "source.delete.v1",
                "entry_id": str(entry_id),
                "expected_revision": expected_revision,
            },
        )
        client_key_hash = self._client_key_hash(vault_id, idempotency_key)
        replay = await self._receipts.find(
            operation="source.delete",
            client_key_hash=client_key_hash,
            request_hash=request_hash,
        )
        if replay is not None:
            return self._deletion_dto(replay)
        policy_epoch, source_generation = await self._locked_fences(vault_id)

        def current_revision(session: object) -> uuid.UUID:
            result = read_source_document(
                cast(AnySession, session), vault_id=vault_id, document_id=entry_id
            )
            if result.revision.revision_no != expected_revision:
                from life_coach.modules.sources.exceptions import RevisionConflict

                raise RevisionConflict(
                    expected_revision=expected_revision,
                    current_revision=result.revision.revision_no,
                )
            return result.revision.id

        revision_id = await self._session.run_sync(current_revision)
        reservation = await self._receipts.reserve(
            SourceCommandReceiptSpec(
                vault_id=vault_id,
                operation="source.delete",
                client_key_hash=client_key_hash,
                request_hash=request_hash,
                resource_id=entry_id,
                resource_revision_id=revision_id,
                result_revision_no=expected_revision,
                source_generation=source_generation + 1,
                policy_epoch=policy_epoch + 1,
            )
        )
        if reservation.created:
            await self._session.run_sync(
                lambda session: tombstone_source_document(
                    session,
                    vault_id=vault_id,
                    document_id=entry_id,
                )
            )
        return self._deletion_dto(reservation)

    async def _locked_fences(self, vault_id: uuid.UUID) -> tuple[int, int]:
        return await self._session.run_sync(
            lambda session: (lambda vault: (vault.policy_epoch, vault.source_generation))(
                get_vault(session, vault_id, for_update=True)
            )
        )

    async def _write_replay(
        self,
        vault_id: uuid.UUID,
        reservation: SourceCommandReservation,
    ) -> dict[str, object]:
        if reservation.resource_revision_id is None:
            raise SourceNotFound("Source revision is unavailable")
        revision_id = reservation.resource_revision_id

        def read(session: object) -> dict[str, object]:
            document = get_source_document(
                cast(AnySession, session),
                vault_id=vault_id,
                document_id=reservation.resource_id,
            )
            revision = get_source_revision(
                cast(AnySession, session),
                vault_id=vault_id,
                revision_id=revision_id,
            )
            return self._write_dto(document, revision)

        return await self._session.run_sync(read)

    def _read_dto(
        self,
        session: object,
        *,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
    ) -> dict[str, object]:
        source = read_source_document(
            cast(AnySession, session), vault_id=vault_id, document_id=entry_id
        )
        revisions = list_source_revisions(
            cast(AnySession, session), vault_id=vault_id, document_id=entry_id
        )
        ciphertext = source.revision.content_ciphertext
        if ciphertext is None:
            raise SourceContentUnavailable("Source content is unavailable")
        content = self._protector.open(
            vault_id=vault_id,
            document_id=entry_id,
            object_id=source.revision.id,
            revision_no=source.revision.revision_no,
            kind="revision",
            ciphertext=ciphertext,
        )
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != source.revision.content_hash:
            raise SourceContentUnavailable("Source content is unavailable")
        document = source.document
        return {
            "id": document.id,
            "title": document.title,
            "source_type": document.source_type.value,
            "data_class": document.data_class.value,
            "captured_at": document.event_time_hint,
            "capture_timezone": document.capture_timezone,
            "content": content,
            "revision": source.revision.revision_no,
            "revision_id": source.revision.id,
            "created_at": document.created_at,
            "processing": {"state": document.processing_state.value},
            "revisions": [self._revision_dto(revision) for revision in revisions],
        }

    @staticmethod
    def _revision_dto(revision: SourceRevision) -> dict[str, object]:
        return {
            "id": revision.id,
            "revision": revision.revision_no,
            "created_at": revision.created_at,
            "content_mime": revision.content_mime,
            "language": revision.language,
        }

    @staticmethod
    def _write_dto(document: SourceDocument, revision: SourceRevision) -> dict[str, object]:
        return {
            "id": document.id,
            "revision": revision.revision_no,
            "saved": True,
            "processing": {"state": document.processing_state.value},
        }

    @staticmethod
    def _deletion_dto(reservation: SourceCommandReservation) -> dict[str, object]:
        return {
            "id": reservation.resource_id,
            "tombstoned": True,
            "source_generation": reservation.source_generation,
            "policy_epoch": reservation.policy_epoch,
            "cascade_state": "planned",
            "sinks": [{"sink": sink.value, "state": "planned"} for sink in DELETION_SINKS],
        }

    def _client_key_hash(self, vault_id: uuid.UUID, key: str) -> VaultRequestFingerprint:
        return canonical_request_hash(
            {"domain": "source.client_idempotency_key.v1", "key": key},
            vault_id=vault_id,
            hmac_key=self._hmac_key,
        )

    def _request_hash(self, vault_id: uuid.UUID, value: JsonValue) -> VaultRequestFingerprint:
        return canonical_request_hash(value, vault_id=vault_id, hmac_key=self._hmac_key)

    def _seal(
        self,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        object_id: uuid.UUID,
        revision_no: int,
        kind: Literal["revision", "fragment"],
        plaintext: str,
    ) -> bytes:
        return self._protector.seal(
            vault_id=vault_id,
            document_id=document_id,
            object_id=object_id,
            revision_no=revision_no,
            kind=kind,
            plaintext=plaintext,
        )


__all__ = [
    "AppendEntryRevisionCommand",
    "CreateEntryCommand",
    "LocalAesGcmSourceContentProtector",
    "PostgresSourceEntryService",
    "ProtectedCorrectionSourceRecorder",
    "ProtectedSourceFragmentPlaintextReader",
    "SourceContentProtector",
    "SourceContentUnavailable",
    "SourceCursorCodec",
    "SourceEntryService",
]
