"""Deterministic resolution for a deliberately small set of relative times."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import TemporalRange, TimePrecision

_SPACE = re.compile(r"\s+")


class TimeResolutionError(ValueError):
    """Capture metadata is invalid for deterministic relative-time resolution."""


def resolve_relative_time(
    expression: str,
    *,
    captured_at: datetime,
    capture_timezone: str,
) -> TemporalRange:
    """Resolve common relative expressions without inventing exact dates.

    Bounds are stored in UTC while the original phrase, reference timestamp, and
    historical capture timezone remain in the contract. Unrecognized text becomes
    UNKNOWN rather than a guessed date. "Spring" uses the conventional March-June
    northern-hemisphere range; callers with another locale should use a locale
    resolver that returns the same contract.
    """

    phrase = expression.strip()
    if not phrase:
        raise TimeResolutionError("time expression must not be blank")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise TimeResolutionError("captured_at must be timezone-aware")
    capture_zone = _load_zone(capture_timezone, reference_year=captured_at.year)

    local_reference = captured_at.astimezone(capture_zone)
    normalized = _SPACE.sub(" ", phrase.casefold())
    start: datetime | None = None
    end: datetime | None = None
    precision = TimePrecision.UNKNOWN

    if normalized in {"昨天", "yesterday"}:
        day = local_reference.date() - timedelta(days=1)
        start = datetime(day.year, day.month, day.day, tzinfo=capture_zone)
        end = start + timedelta(days=1)
        precision = TimePrecision.DAY
    elif normalized in {"上个月", "last month"}:
        year, month = _previous_month(local_reference.year, local_reference.month)
        start = datetime(year, month, 1, tzinfo=capture_zone)
        end_year, end_month = _next_month(year, month)
        end = datetime(end_year, end_month, 1, tzinfo=capture_zone)
        precision = TimePrecision.MONTH
    elif normalized in {"去年", "last year"}:
        year = local_reference.year - 1
        start = datetime(year, 1, 1, tzinfo=capture_zone)
        end = datetime(year + 1, 1, 1, tzinfo=capture_zone)
        precision = TimePrecision.YEAR
    elif normalized in {"去年春天", "去年春季", "last spring"}:
        year = local_reference.year - 1
        start = datetime(year, 3, 1, tzinfo=capture_zone)
        end = datetime(year, 6, 1, tzinfo=capture_zone)
        precision = TimePrecision.RANGE

    return TemporalRange(
        precision=precision,
        original_expression=phrase,
        earliest=start.astimezone(UTC) if start is not None else None,
        latest=end.astimezone(UTC) if end is not None else None,
        is_relative=True,
        reference_timestamp=captured_at.astimezone(UTC),
        timezone=capture_timezone,
    )


def month_range(
    *,
    year: int,
    month: int,
    original_expression: str,
    timezone: str,
) -> TemporalRange:
    """Create an honest month-precision half-open range."""

    if not 1 <= month <= 12:
        raise TimeResolutionError("month must be between 1 and 12")
    zone = _load_zone(timezone, reference_year=year)
    start = datetime(year, month, 1, tzinfo=zone)
    end_year, end_month = _next_month(year, month)
    end = datetime(end_year, end_month, 1, tzinfo=zone)
    return TemporalRange(
        precision=TimePrecision.MONTH,
        original_expression=original_expression,
        earliest=start.astimezone(UTC),
        latest=end.astimezone(UTC),
        timezone=timezone,
    )


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _next_month(year: int, month: int) -> tuple[int, int]:
    # monthrange validates the year/month on every supported Python platform.
    calendar.monthrange(year, month)
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _load_zone(name: str, *, reference_year: int) -> tzinfo:
    if name in {"UTC", "Etc/UTC", "Z"}:
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        # Windows has no system IANA database. Shanghai has observed UTC+08:00
        # without DST since 1992, which safely covers this resolver's modern
        # relative-time window. Historical or other zones still fail closed.
        if name == "Asia/Shanghai" and reference_year >= 1992:
            return timezone(timedelta(hours=8), name=name)
        raise TimeResolutionError(f"unknown timezone: {name}") from exc


__all__ = [
    "TimeResolutionError",
    "month_range",
    "resolve_relative_time",
]
