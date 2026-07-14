from __future__ import annotations

from datetime import datetime, timezone

import pytest

from knowledgehub.core.retry import RetryPolicy, compute_retry_delay, parse_retry_after


def test_zero_backoff_is_honoured_and_takes_precedence() -> None:
    assert (
        compute_retry_delay(
            {"Backoff": "0", "Retry-After": "30"},
            1,
            random_value=0.5,
        )
        == 0.0
    )


def test_retry_after_accepts_seconds_and_http_date() -> None:
    now = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)

    assert parse_retry_after("0", now=now) == 0.0
    assert parse_retry_after("Tue, 14 Jul 2026 00:00:09 GMT", now=now) == 9.0
    assert parse_retry_after("not-a-date", now=now) is None


@pytest.mark.parametrize(
    ("sample", "expected"),
    [(0.0, 6.0), (0.5, 8.0), (1.0, 10.0)],
)
def test_exponential_delay_uses_bounded_symmetric_jitter(sample: float, expected: float) -> None:
    policy = RetryPolicy(
        base_delay_seconds=2.0,
        max_delay_seconds=60.0,
        jitter_ratio=0.25,
    )

    assert compute_retry_delay({}, 3, policy=policy, random_value=sample) == expected


def test_retry_number_and_random_sample_are_validated() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        compute_retry_delay({}, 0)
    with pytest.raises(ValueError, match="between 0 and 1"):
        compute_retry_delay({}, 1, random_value=1.1)
