"""Retry classification and bounded full-jitter calculation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from life_coach.jobs.enums import FailureClass, JobState


@dataclass(frozen=True, slots=True)
class RetryDecision:
    target_state: JobState
    delay: timedelta | None
    requires_reconciliation: bool = False


def full_jitter_delay(
    *,
    attempt: int,
    base_seconds: float = 1.0,
    cap_seconds: float = 300.0,
    random_sample: Callable[[], float],
) -> timedelta:
    """Return ``U(0, min(cap, base * 2**(attempt-1)))``.

    ``random_sample`` is injected so boundary behavior is deterministic in tests.
    """

    if attempt < 1:
        raise ValueError("attempt must be positive")
    if base_seconds <= 0 or cap_seconds <= 0:
        raise ValueError("retry delay bounds must be positive")
    sample = random_sample()
    if not 0.0 <= sample <= 1.0:
        raise ValueError("random sample must be between zero and one")
    ceiling = min(cap_seconds, base_seconds * (2 ** (attempt - 1)))
    return timedelta(seconds=ceiling * sample)


def retry_decision(
    failure: FailureClass,
    *,
    attempts: int,
    max_attempts: int,
    random_sample: Callable[[], float],
    base_seconds: float = 1.0,
    cap_seconds: float = 300.0,
) -> RetryDecision:
    if attempts < 0 or max_attempts < 1:
        raise ValueError("invalid attempt counters")
    if failure in {FailureClass.TRANSIENT, FailureClass.RATE_LIMITED}:
        if attempts >= max_attempts:
            return RetryDecision(JobState.DEAD, None)
        delay = full_jitter_delay(
            attempt=max(attempts, 1),
            base_seconds=base_seconds,
            cap_seconds=cap_seconds,
            random_sample=random_sample,
        )
        return RetryDecision(JobState.RETRYING, delay)
    if failure is FailureClass.EXTERNAL_OUTCOME_UNKNOWN:
        return RetryDecision(JobState.WAITING, None, requires_reconciliation=True)
    if failure in {
        FailureClass.POLICY_REVOKED,
        FailureClass.SOURCE_CHANGED,
        FailureClass.TOMBSTONED,
    }:
        return RetryDecision(JobState.CANCELED, None)
    return RetryDecision(JobState.DEAD, None)
