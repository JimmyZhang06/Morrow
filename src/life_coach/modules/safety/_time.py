"""Private time validation helpers for safety domain objects."""

from __future__ import annotations

from datetime import datetime


def require_aware(value: datetime, *, field_name: str) -> None:
    """Reject ambiguous timestamps before they reach policy comparisons."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
