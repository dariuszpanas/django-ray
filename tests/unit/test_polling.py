"""Deterministic tests for adaptive worker polling."""

from __future__ import annotations

import pytest

from django_ray.conf.defaults import DEFAULTS
from django_ray.runner.polling import AdaptivePollingPolicy


def test_compatibility_defaults_keep_adaptive_backoff_opt_in() -> None:
    assert DEFAULTS["WORKER_POLL_INTERVAL_SECONDS"] == 0.1
    assert DEFAULTS["WORKER_POLL_MAX_INTERVAL_SECONDS"] == 0.1


def test_compatibility_mode_preserves_exact_fixed_cadence_without_jitter() -> None:
    random_calls = 0

    def random_value() -> float:
        nonlocal random_calls
        random_calls += 1
        return 1.0

    policy = AdaptivePollingPolicy(
        base_interval_seconds=0.1,
        max_interval_seconds=0.1,
        random_value=random_value,
    )

    assert [policy.next_delay(activity=False) for _ in range(4)] == [0.1] * 4
    assert policy.next_delay(activity=True) == 0.1
    assert random_calls == 0


def test_idle_backoff_grows_to_bound_and_resets_after_activity() -> None:
    policy = AdaptivePollingPolicy(
        base_interval_seconds=0.1,
        max_interval_seconds=0.8,
        random_value=lambda: 0.0,
    )

    assert [policy.next_delay(activity=False) for _ in range(5)] == pytest.approx(
        [0.1, 0.2, 0.4, 0.8, 0.8]
    )
    assert policy.idle_interval_seconds == 0.8
    assert policy.next_delay(activity=True) == pytest.approx(0.1)
    assert policy.idle_interval_seconds == 0.1
    assert policy.next_delay(activity=False) == pytest.approx(0.1)


@pytest.mark.parametrize(
    "random_value, expected",
    [
        (0.0, 0.1),
        (0.5, 0.09),
        (1.0, 0.08),
    ],
)
def test_downward_jitter_is_bounded_and_deterministic(random_value: float, expected: float) -> None:
    policy = AdaptivePollingPolicy(
        base_interval_seconds=0.1,
        max_interval_seconds=1.0,
        random_value=lambda: random_value,
    )

    assert policy.next_delay(activity=False) == pytest.approx(expected)


def test_jitter_never_exceeds_maximum_delay() -> None:
    policy = AdaptivePollingPolicy(
        base_interval_seconds=0.75,
        max_interval_seconds=1.0,
        random_value=lambda: 0.0,
    )

    policy.next_delay(activity=False)
    assert policy.next_delay(activity=False) == 1.0


def test_distinct_random_sequences_decorrelate_worker_delays() -> None:
    values_a = iter([0.1, 0.2, 0.3])
    values_b = iter([0.9, 0.8, 0.7])
    worker_a = AdaptivePollingPolicy(
        base_interval_seconds=0.1,
        max_interval_seconds=1.0,
        random_value=lambda: next(values_a),
    )
    worker_b = AdaptivePollingPolicy(
        base_interval_seconds=0.1,
        max_interval_seconds=1.0,
        random_value=lambda: next(values_b),
    )

    delays_a = [worker_a.next_delay(activity=False) for _ in range(3)]
    delays_b = [worker_b.next_delay(activity=False) for _ in range(3)]

    assert delays_a != delays_b
    assert all(a > b for a, b in zip(delays_a, delays_b, strict=True))
