"""Privacy-safe payload and idempotency helpers.

Only opaque routing identifiers belong in durable job/outbox payloads. Request bodies may be
hashed by :func:`canonical_request_hash`, but are never returned or persisted by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping, Sequence

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type SafePayload = dict[str, str]

SAFE_PAYLOAD_KEYS = frozenset({"vault_id", "resource_id", "pipeline_version"})
_REQUEST_FINGERPRINT_PREFIX = "hmac-sha256:v1:"
_HASH_PATTERN = re.compile(r"^hmac-sha256:v1:[0-9a-f]{64}$")
_PIPELINE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,99}$")
_ROUTING_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")
_PROVIDER_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/+=@-]{0,254}$")
_TECHNICAL_IDENTIFIER_PATTERN = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[a-z][a-z0-9_.-]{0,31}:"
    r"(?:[0-9]{1,20}|[0-9a-f]{16,64}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r")$"
)
_FINGERPRINT_PROOF = object()


class UnsafePayloadError(ValueError):
    """Raised without echoing a rejected, potentially private payload."""


class InvalidRequestHashError(ValueError):
    """Raised when a persisted request fingerprint is not a versioned vault HMAC."""


class VaultRequestFingerprint(str):
    """Opaque fingerprint receipt minted only by the vault-scoped HMAC factory."""

    vault_id: uuid.UUID

    def __new__(
        cls,
        value: str,
        *,
        vault_id: uuid.UUID,
        _proof: object | None = None,
    ) -> VaultRequestFingerprint:
        if _proof is not _FINGERPRINT_PROOF:
            raise TypeError("request fingerprints must be minted by the vault HMAC factory")
        instance = super().__new__(cls, value)
        instance.vault_id = vault_id
        return instance


def vault_scoped_request_hash(
    request: JsonValue,
    *,
    vault_id: uuid.UUID,
    hmac_key: bytes,
) -> VaultRequestFingerprint:
    """Return an unlinkable, vault-bound request fingerprint.

    The HMAC key belongs in a secret manager and must be shared only by trusted application
    roles. Including the vault UUID in the authenticated bytes prevents the same request from
    producing a cross-vault correlation identifier.
    """

    if len(hmac_key) < 32:
        raise ValueError("request fingerprint HMAC key must contain at least 32 bytes")

    encoded = json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    authenticated = b"life-coach/request-fingerprint/v1\x00" + vault_id.bytes + encoded
    digest = hmac.new(hmac_key, authenticated, hashlib.sha256).hexdigest()
    return VaultRequestFingerprint(
        f"{_REQUEST_FINGERPRINT_PREFIX}{digest}",
        vault_id=vault_id,
        _proof=_FINGERPRINT_PROOF,
    )


def canonical_request_hash(
    request: JsonValue,
    *,
    vault_id: uuid.UUID,
    hmac_key: bytes,
) -> VaultRequestFingerprint:
    """Compatibility name for the mandatory vault-scoped HMAC fingerprint."""

    return vault_scoped_request_hash(request, vault_id=vault_id, hmac_key=hmac_key)


def validate_request_hash(request_hash: str) -> str:
    """Validate and return a normalized request fingerprint."""

    normalized = request_hash.lower()
    if _HASH_PATTERN.fullmatch(normalized) is None:
        raise InvalidRequestHashError("request_hash must be a vault-scoped HMAC-SHA-256 digest")
    return normalized


def validate_vault_request_fingerprint(
    fingerprint: object,
    *,
    vault_id: uuid.UUID,
) -> VaultRequestFingerprint:
    """Reject client-constructed digest strings at enqueue/create trust boundaries."""

    if not isinstance(fingerprint, VaultRequestFingerprint) or fingerprint.vault_id != vault_id:
        raise InvalidRequestHashError("request fingerprint must be minted for the repository vault")
    validate_request_hash(fingerprint)
    return fingerprint


def validate_safe_payload(payload: Mapping[str, object]) -> SafePayload:
    """Accept only the documented opaque routing payload.

    The exception deliberately reports no key or value, because even a rejected key can reveal
    private content semantics in logs.
    """

    if not set(payload).issubset(SAFE_PAYLOAD_KEYS):
        raise UnsafePayloadError("payload contains non-routing data")
    result: SafePayload = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value:
            raise UnsafePayloadError("payload values must be non-empty opaque strings")
        if key in {"vault_id", "resource_id"}:
            try:
                parsed = uuid.UUID(value)
            except ValueError as error:
                raise UnsafePayloadError("routing identifiers must be UUIDs") from error
            if str(parsed) != value.lower():
                raise UnsafePayloadError("routing identifiers must use canonical UUID form")
        if key == "pipeline_version" and _PIPELINE_VERSION_PATTERN.fullmatch(value) is None:
            raise UnsafePayloadError("pipeline version must use the technical version format")
        result[key] = value
    return result


def validate_subscriber_job_types(job_types: Sequence[str]) -> tuple[str, ...]:
    """Validate a durable, body-free expected subscriber set."""

    normalized = tuple(job_types)
    if len(set(normalized)) != len(normalized):
        raise UnsafePayloadError("subscriber job types must be unique")
    for job_type in normalized:
        validate_routing_name(job_type)
    return normalized


def validate_routing_name(value: str) -> str:
    """Keep operational names technical, bounded, and safe for metadata/log surfaces."""

    if _ROUTING_NAME_PATTERN.fullmatch(value) is None:
        raise UnsafePayloadError("operational name must use the routing-name format")
    return value


def validate_technical_identifier(value: str) -> str:
    """Accept only generated opaque identifiers, never free-form labels or prose.

    Values are canonical UUIDs or a short technical namespace followed by a numeric, UUID, or
    sufficiently long hexadecimal token. Human-readable metadata therefore cannot accidentally
    become a second payload channel.
    """

    if _TECHNICAL_IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise UnsafePayloadError("persistent identifier must use the technical format")
    return value


def validate_resource_version_identifier(value: str) -> str:
    """Require the outbound scope's resource version to be a globally opaque UUID."""

    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise UnsafePayloadError("resource version must be a canonical UUID") from error
    if str(parsed) != value:
        raise UnsafePayloadError("resource version must be a canonical UUID")
    return value


def validate_provider_identifier(value: str) -> str:
    """Allow common opaque provider IDs while rejecting whitespace/control/log injection."""

    if _PROVIDER_IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise UnsafePayloadError("provider identifier must use the opaque identifier format")
    return value
