from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from life_coach.application.source_entries import (
    LocalAesGcmSourceContentProtector,
    ProtectedCorrectionSourceRecorder,
    ProtectedSourceFragmentPlaintextReader,
    SourceContentUnavailable,
    SourceCursorCodec,
)
from life_coach.modules.identity.service import create_vault
from life_coach.modules.knowledge.enums import DataClass as KnowledgeDataClass
from life_coach.modules.sources.exceptions import InvalidSourceData
from life_coach.modules.sources.models import SourceDocument, SourceFragment, SourceRevision
from life_coach.shared.database import Base

_CONTENT_KEY = b"source-content-test-key-material-32-bytes"
_CURSOR_KEY = b"source-cursor-test-key-material-32-bytes"


def _identity() -> dict[str, object]:
    return {
        "vault_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "object_id": uuid.uuid4(),
        "revision_no": 1,
        "kind": "revision",
    }


def _seal(
    protector: LocalAesGcmSourceContentProtector,
    identity: dict[str, object],
    plaintext: str,
) -> bytes:
    return protector.seal(  # type: ignore[arg-type]
        **identity,
        plaintext=plaintext,
    )


def _open(
    protector: LocalAesGcmSourceContentProtector,
    identity: dict[str, object],
    ciphertext: bytes,
) -> str:
    return protector.open(  # type: ignore[arg-type]
        **identity,
        ciphertext=ciphertext,
    )


def test_local_aes_gcm_uses_random_nonces_and_round_trips() -> None:
    protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
    identity = _identity()
    plaintext = "I feel calmer after drawing."

    first = _seal(protector, identity, plaintext)
    second = _seal(protector, identity, plaintext)

    assert first != second
    assert plaintext.encode("utf-8") not in first
    assert plaintext.encode("utf-8") not in second
    assert _open(protector, identity, first) == plaintext
    assert _open(protector, identity, second) == plaintext


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("vault_id", uuid.uuid4()),
        ("document_id", uuid.uuid4()),
        ("object_id", uuid.uuid4()),
        ("revision_no", 2),
        ("kind", "fragment"),
    ],
)
def test_local_aes_gcm_rejects_authoritative_identity_substitution(
    field: str,
    replacement: object,
) -> None:
    protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
    identity = _identity()
    ciphertext = _seal(protector, identity, "private source text")
    substituted = {**identity, field: replacement}

    with pytest.raises(SourceContentUnavailable, match="unavailable"):
        _open(protector, substituted, ciphertext)


def test_local_aes_gcm_rejects_tampering_and_wrong_key_without_echoing_plaintext() -> None:
    protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
    wrong_protector = LocalAesGcmSourceContentProtector(b"different-source-key-material-32-bytes")
    identity = _identity()
    plaintext = "a private sentence that must not enter errors"
    ciphertext = _seal(protector, identity, plaintext)
    tampered = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])

    for candidate, opener in (
        (tampered, protector),
        (ciphertext, wrong_protector),
    ):
        with pytest.raises(SourceContentUnavailable) as caught:
            _open(opener, identity, candidate)
        assert plaintext not in str(caught.value)
        assert plaintext not in repr(caught.value)


def test_local_aes_gcm_rejects_invalid_envelopes_and_keeps_key_out_of_repr() -> None:
    protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
    identity = _identity()

    with pytest.raises(SourceContentUnavailable):
        _open(protector, identity, b"not-an-encrypted-envelope")

    assert _CONTENT_KEY.decode("ascii") not in repr(protector)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        LocalAesGcmSourceContentProtector(b"too-short")


def _document(*, document_id: uuid.UUID, created_at: datetime) -> SourceDocument:
    return SourceDocument(id=document_id, created_at=created_at)


def test_source_cursor_round_trips_and_normalizes_time_to_utc() -> None:
    codec = SourceCursorCodec(_CURSOR_KEY)
    vault_id = uuid.uuid4()
    document_id = uuid.uuid4()
    created_at = datetime(2026, 8, 24, 18, 15, tzinfo=UTC) + timedelta(hours=8)

    token = codec.encode(
        vault_id,
        _document(document_id=document_id, created_at=created_at),
    )

    decoded_at, decoded_id = codec.decode(vault_id, token)
    assert decoded_at == created_at.astimezone(UTC)
    assert decoded_id == document_id
    assert token.count(".") == 1


def test_source_cursor_is_bound_to_its_vault() -> None:
    codec = SourceCursorCodec(_CURSOR_KEY)
    vault_id = uuid.uuid4()
    token = codec.encode(
        vault_id,
        _document(document_id=uuid.uuid4(), created_at=datetime.now(UTC)),
    )

    with pytest.raises(InvalidSourceData, match="cursor is invalid"):
        codec.decode(uuid.uuid4(), token)


def test_source_cursor_rejects_payload_and_signature_tampering() -> None:
    codec = SourceCursorCodec(_CURSOR_KEY)
    vault_id = uuid.uuid4()
    token = codec.encode(
        vault_id,
        _document(document_id=uuid.uuid4(), created_at=datetime.now(UTC)),
    )
    payload, signature = token.split(".")
    payload_tampered = ("A" if payload[0] != "A" else "B") + payload[1:]
    signature_tampered = ("A" if signature[0] != "A" else "B") + signature[1:]

    for candidate in (
        f"{payload_tampered}.{signature}",
        f"{payload}.{signature_tampered}",
    ):
        with pytest.raises(InvalidSourceData, match="cursor is invalid"):
            codec.decode(vault_id, candidate)


def test_source_cursor_rejects_wrong_key_and_malformed_tokens() -> None:
    codec = SourceCursorCodec(_CURSOR_KEY)
    other_codec = SourceCursorCodec(b"different-cursor-key-material-32-bytes")
    vault_id = uuid.uuid4()
    token = codec.encode(
        vault_id,
        _document(document_id=uuid.uuid4(), created_at=datetime.now(UTC)),
    )

    with pytest.raises(InvalidSourceData, match="cursor is invalid"):
        other_codec.decode(vault_id, token)
    for malformed in ("", "one-part", "too.many.parts"):
        with pytest.raises(InvalidSourceData, match="cursor is invalid"):
            codec.decode(vault_id, malformed)

    assert _CURSOR_KEY.decode("ascii") not in repr(codec)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        SourceCursorCodec(b"too-short")


def test_plaintext_reader_opens_fragment_with_full_authoritative_identity() -> None:
    protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
    reader = ProtectedSourceFragmentPlaintextReader(protector)
    vault_id, document_id, revision_id, fragment_id = (uuid.uuid4() for _ in range(4))
    ciphertext = protector.seal(
        vault_id=vault_id,
        document_id=document_id,
        object_id=fragment_id,
        revision_no=2,
        kind="fragment",
        plaintext="private fragment",
    )

    assert (
        reader.read_text(
            vault_id=vault_id,
            document_id=document_id,
            revision_id=revision_id,
            revision_no=2,
            fragment_id=fragment_id,
            ciphertext=ciphertext,
        )
        == "private fragment"
    )
    with pytest.raises(SourceContentUnavailable):
        reader.read_text(
            vault_id=vault_id,
            document_id=document_id,
            revision_id=revision_id,
            revision_no=3,
            fragment_id=fragment_id,
            ciphertext=ciphertext,
        )


def test_correction_recorder_encrypts_source_and_obeys_caller_rollback() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
    recorder = ProtectedCorrectionSourceRecorder(protector)
    correction = "I prefer a specialist path."

    with Session(engine, expire_on_commit=False) as session:
        vault = create_vault(session)
        session.commit()
        anchor = recorder.record_correction(
            session=session,
            vault_id=vault.id,
            memory_id=uuid.uuid4(),
            correction_text=correction,
            data_class=KnowledgeDataClass.SENSITIVE,
            recorded_at=datetime.now(UTC),
        )
        fragment = session.scalar(
            select(SourceFragment).where(SourceFragment.id == anchor.source_fragment_id)
        )
        assert fragment is not None
        revision = session.scalar(
            select(SourceRevision).where(SourceRevision.id == fragment.revision_id)
        )
        assert revision is not None
        document = session.scalar(
            select(SourceDocument).where(SourceDocument.id == revision.document_id)
        )
        assert document is not None
        assert correction.encode() not in fragment.text_ciphertext
        assert correction.encode() not in (revision.content_ciphertext or b"")
        assert (
            protector.open(
                vault_id=vault.id,
                document_id=document.id,
                object_id=fragment.id,
                revision_no=revision.revision_no,
                kind="fragment",
                ciphertext=fragment.text_ciphertext,
            )
            == correction
        )

        session.rollback()
        assert session.scalar(select(SourceFragment.id)) is None
        assert session.scalar(select(SourceRevision.id)) is None
        assert session.scalar(select(SourceDocument.id)) is None

    engine.dispose()
