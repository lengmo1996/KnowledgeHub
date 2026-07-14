"""Bounded retry policy shared by read-only source clients."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, FrozenSet, Mapping, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Configuration for finite exponential retry behaviour."""

    max_retries: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.2
    retryable_statuses: FrozenSet[int] = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds cannot be negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds cannot be negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")


class RetryExhaustedError(RuntimeError):
    """Raised when :func:`retry_call` exhausts all configured retries."""

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(f"operation failed after {attempts} attempts")


DEFAULT_RETRY_POLICY = RetryPolicy()


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value.strip()
    return None


def _nonnegative_number(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return max(0.0, parsed)


def parse_retry_after(value: Optional[str], *, now: Optional[datetime] = None) -> Optional[float]:
    """Parse either Retry-After seconds or an RFC 7231 HTTP date."""

    numeric = _nonnegative_number(value)
    if numeric is not None:
        return numeric
    if not value:
        return None
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (target - current).total_seconds())


def compute_retry_delay(
    headers: Mapping[str, str],
    retry_number: int,
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    random_value: Optional[float] = None,
    now: Optional[datetime] = None,
) -> float:
    """Compute delay for a one-based retry number.

    Zotero's ``Backoff`` response header has highest precedence, followed by
    ``Retry-After``.  Otherwise bounded exponential backoff with symmetric
    jitter is used.
    """

    if retry_number < 1:
        raise ValueError("retry_number must be at least 1")
    backoff = _nonnegative_number(_header(headers, "Backoff"))
    if backoff is not None:
        return min(backoff, policy.max_delay_seconds)
    retry_after = parse_retry_after(_header(headers, "Retry-After"), now=now)
    if retry_after is not None:
        return min(retry_after, policy.max_delay_seconds)

    base = min(
        policy.max_delay_seconds,
        policy.base_delay_seconds * (2 ** (retry_number - 1)),
    )
    sample = random.random() if random_value is None else random_value
    if not 0 <= sample <= 1:
        raise ValueError("random_value must be between 0 and 1")
    jitter_multiplier = 1.0 + ((float(sample) * 2.0) - 1.0) * policy.jitter_ratio
    return float(max(0.0, min(policy.max_delay_seconds, base * jitter_multiplier)))


def is_retryable_status(status_code: int, policy: RetryPolicy = DEFAULT_RETRY_POLICY) -> bool:
    """Return whether an HTTP status is transient under *policy*."""

    return status_code in policy.retryable_statuses


def retry_call(
    operation: Callable[[], T],
    *,
    is_retryable_exception: Callable[[Exception], bool],
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Callable[[float], None] = time.sleep,
    random_source: Callable[[], float] = random.random,
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
) -> T:
    """Retry an exception-raising operation according to a finite policy.

    HTTP clients normally implement their own response loop so server headers
    can be passed to :func:`compute_retry_delay`; this helper covers transient
    transport exceptions where no response headers exist.
    """

    for attempt in range(policy.max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= policy.max_retries or not is_retryable_exception(exc):
                raise
            retry_number = attempt + 1
            delay = compute_retry_delay(
                {}, retry_number, policy=policy, random_value=random_source()
            )
            if on_retry is not None:
                on_retry(exc, retry_number, delay)
            sleeper(delay)
    raise RetryExhaustedError(policy.max_retries + 1)  # pragma: no cover
