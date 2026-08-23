"""Closed vocabularies for the PostgreSQL job subsystem."""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    RETRYING = "retrying"
    DONE = "done"
    DEAD = "dead"
    CANCELED = "canceled"


class JobQueue(StrEnum):
    INTERACTIVE = "interactive"
    INGEST_TEXT = "ingest_text"
    MEDIA = "media"
    REFLECTION = "reflection"
    NARRATIVE = "narrative"
    PRIVACY_CRITICAL = "privacy_critical"
    INTEGRATION = "integration"


class FenceCheckpoint(StrEnum):
    BEFORE_SOURCE_READ = "before_source_read"
    BEFORE_EXTERNAL_CALL = "before_external_call"
    BEFORE_RESULT_COMMIT = "before_result_commit"


class CompletionStatus(StrEnum):
    DONE = "done"
    DISCARDED = "discarded"
    LEASE_LOST = "lease_lost"


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    DETERMINISTIC = "deterministic"
    POLICY_REVOKED = "policy_revoked"
    SOURCE_CHANGED = "source_changed"
    TOMBSTONED = "tombstoned"
    EXTERNAL_OUTCOME_UNKNOWN = "external_outcome_unknown"


class OutboundOperationState(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    MANUAL_REVIEW = "manual_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class OutboundNextAction(StrEnum):
    EXECUTE = "execute"
    AWAIT_RESULT = "await_result"
    RECONCILE = "reconcile"
    QUERY_PROVIDER = "query_provider"
    MANUAL_REVIEW = "manual_review"
    NONE = "none"


class OutboundAuthorizationDecision(StrEnum):
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"
    MISMATCH = "mismatch"
    CONSUMED_ELSEWHERE = "consumed_elsewhere"


class ReconciliationOutcome(StrEnum):
    FOUND = "found"
    CONFIRMED_NOT_FOUND = "confirmed_not_found"
    STILL_UNKNOWN = "still_unknown"
    PERMANENT_FAILURE = "permanent_failure"
