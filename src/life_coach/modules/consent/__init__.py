"""Append-only, purpose-limited consent records and reducers."""

from life_coach.modules.consent.exceptions import (
    ConsentDenied,
    ConsentRecordImmutable,
    ConsentScopeNotFound,
    InvalidConsentAction,
    InvalidConsentPurpose,
)
from life_coach.modules.consent.models import (
    ConsentAction,
    ConsentPurpose,
    ConsentRecord,
    ConsentScope,
)
from life_coach.modules.consent.service import (
    ConsentResolution,
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
    "ConsentPurpose",
    "ConsentRecord",
    "ConsentRecordImmutable",
    "ConsentResolution",
    "ConsentScope",
    "ConsentScopeNotFound",
    "InvalidConsentAction",
    "InvalidConsentPurpose",
    "capture_snapshot",
    "check_consent",
    "check_snapshot",
    "grant_consent",
    "record_consent",
    "require_consent",
    "resolve_consent",
    "revoke_consent",
]
