"""Adaptive database polling policy for django-ray workers."""

from __future__ import annotations

import random
from collections.abc import Callable


class AdaptivePollingPolicy:
    """Compute de-correlated polling delays with bounded idle backoff.

    The policy is deliberately independent from wall-clock time. Callers report
    whether a loop observed activity and schedule the returned delay using their
    own monotonic clock. Injecting ``random_value`` makes policy tests exact.
    """

    def __init__(
        self,
        *,
        base_interval_seconds: float,
        max_interval_seconds: float,
        backoff_multiplier: float = 2.0,
        jitter_ratio: float = 0.2,
        random_value: Callable[[], float] | None = None,
    ) -> None:
        self.base_interval_seconds = base_interval_seconds
        self.max_interval_seconds = max_interval_seconds
        self.backoff_multiplier = backoff_multiplier
        self.jitter_ratio = jitter_ratio
        self._random_value = random_value or random.random
        self._idle_interval_seconds = base_interval_seconds

    @property
    def idle_interval_seconds(self) -> float:
        """Return the unjittered interval that will be used for the next poll."""
        return self._idle_interval_seconds

    def reset(self) -> None:
        """Reset idle backoff after any worker activity."""
        self._idle_interval_seconds = self.base_interval_seconds

    def next_delay(self, *, activity: bool) -> float:
        """Return the next jittered delay and advance idle backoff."""
        if activity:
            self.reset()

        interval = self._idle_interval_seconds
        if self.max_interval_seconds == self.base_interval_seconds:
            # Preserve the legacy fixed cadence exactly when idle backoff has
            # not been opted into. Jitter in this mode would increase the idle
            # query rate above the configured compatibility interval.
            delay = interval
        else:
            random_value = min(1.0, max(0.0, float(self._random_value())))
            # Jitter only downward from the bounded interval. Symmetric jitter
            # would clamp the upper half of samples at the maximum and
            # re-synchronize many idle workers into the same repeated spike.
            jitter_multiplier = 1.0 - self.jitter_ratio * random_value
            delay = interval * jitter_multiplier

        if activity:
            self.reset()
        else:
            self._idle_interval_seconds = min(
                self.max_interval_seconds,
                interval * self.backoff_multiplier,
            )

        return delay
