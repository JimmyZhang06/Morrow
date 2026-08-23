"""Domain failures that API adapters may safely translate."""

from __future__ import annotations


class KnowledgeError(Exception):
    """Base class for expected Knowledge-domain failures."""


class MemoryNotFoundError(KnowledgeError):
    """The requested memory is absent from the caller's vault."""


class EvidenceNotFoundError(KnowledgeError):
    """The requested evidence is absent from the caller's vault."""


class InvalidEvidenceError(KnowledgeError):
    """An evidence anchor is incomplete or internally inconsistent."""


class EvidenceSourceUnavailableError(KnowledgeError):
    """Evidence cannot be verified against authoritative Source state."""


class InvalidLifecycleTransitionError(KnowledgeError):
    """A lifecycle transition is not present in the domain state machine."""


class InvalidVerdictError(KnowledgeError):
    """A verdict is incomplete or cannot apply to the current version."""


class RevisionConflictError(KnowledgeError):
    """The supplied ETag no longer identifies the current aggregate state."""


class InvalidTemporalIntervalError(KnowledgeError):
    """A real/system interval is naive, empty, or inverted."""


class PolicyViolationError(KnowledgeError):
    """A proposal violates the memory or psychology safety policy."""


class AuthorizationUnavailableError(KnowledgeError):
    """A required vault/purpose authorization snapshot is unavailable."""


class CorrectionSourceUnavailableError(KnowledgeError):
    """A correction cannot be anchored because Source integration is absent."""


class AppendOnlyViolationError(KnowledgeError):
    """An immutable verdict event was updated or deleted."""
