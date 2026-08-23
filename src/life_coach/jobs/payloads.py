"""Privacy-safe payload and idempotency helpers.

Only opaque routing identifiers belong in durable job/outbox payloads. Request bodies may be
hashed by :func:`canonical_request_hash`, but are never returned or persisted by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type SafePayload = dict[str, str]

SAFE_PAYLOAD_KEYS = frozenset({"vault_id", "resource_id", "pipeline_version"})
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PIPELINE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,99}$")
_ROUTING_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")


class UnsafePayloadError(ValueError):
    """Raised without echoing a rejected, potentially private payload."""


class InvalidRequestHashError(ValueError):
    """Raised when a persisted request fingerprint is not a SHA-256 hex digest."""


def canonical_request_hash(request: JsonValue) -> str:
    """Return a stable SHA-256 fingerprint without retaining the request body."""

    encoded = json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_request_hash(request_hash: str) -> str:
    """Validate and return a normalized request fingerprint."""

    normalized = request_hash.lower()
    if _HASH_PATTERN.fullmatch(normalized) is None:
        raise InvalidRequestHashError("request_hash must be a SHA-256 hex digest")
    return normalized


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
