from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from life_coach.application.source_entries import (
    LocalAesGcmSourceContentProtector,
    SourceContentUnavailable,
    SourceCursorCodec,
)
from life_coach.modules.sources.exceptions import InvalidSourceData
from life_coach.modules.sources.models import SourceDocument

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
