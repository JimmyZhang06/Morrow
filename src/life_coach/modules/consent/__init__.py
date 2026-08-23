"""Append-only, purpose-limited consent records and reducers."""

from life_coach.modules.consent.exceptions import (
    ConsentDenied,
    ConsentInteractionReplayed,
    ConsentRecordImmutable,
    ConsentScopeNotFound,
    InvalidConsentAction,
    InvalidConsentActor,
    InvalidConsentCommand,
    InvalidConsentPurpose,
    InvalidProviderPolicy,
)
from life_coach.modules.consent.models import (
    ConsentAction,
    ConsentPurpose,
    ConsentRecord,
    ConsentScope,
)
from life_coach.modules.consent.provider_policy import ProviderPolicy
from life_coach.modules.consent.service import (
    ConsentResolution,
    UserConsentCommand,
    capture_snapshot,
    check_consent,
    check_snapshot,
    grant_consent,
    record_consent,
    require_consent,
    resolve_consent,
    revoke_consent,
)

__all__ = [
    "ConsentAction",
    "ConsentDenied",
    "ConsentInteractionReplayed",
    "ConsentPurpose",
    "ConsentRecord",
    "ConsentRecordImmutable",
    "ConsentResolution",
    "ConsentScope",
    "ConsentScopeNotFound",
    "InvalidConsentAction",
    "InvalidConsentActor",
    "InvalidConsentCommand",
    "InvalidConsentPurpose",
    "InvalidProviderPolicy",
    "ProviderPolicy",
    "UserConsentCommand",
    "capture_snapshot",
    "check_consent",
    "check_snapshot",
    "grant_consent",
    "record_consent",
    "require_consent",
    "resolve_consent",
    "revoke_consent",
]
