"""Unit tests for retry policy behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from django_ray.runner import retry as retry_module


def _patch_settings(
    monkeypatch,
    *,
    max_attempts: int = 3,
    backoff_seconds: int = 60,
    denylist: list[object] | None = None,
) -> None:
    monkeypatch.setattr(
        retry_module,
        "get_settings",
        lambda: {
            "MAX_TASK_ATTEMPTS": max_attempts,
            "RETRY_BACKOFF_SECONDS": backoff_seconds,
            "RETRY_EXCEPTION_DENYLIST": denylist or [],
        },
    )


@pytest.mark.parametrize(
    ("failed_attempt", "base_seconds", "expected_seconds"),
    [
        (1, 60, 60),
        (2, 60, 120),
        (3, 60, 240),
        (8, 60, 3600),
        (1, 3600, 3600),
        (2, 3600, 3600),
        (1, 0, 0),
        (8, 0, 0),
    ],
)
def test_retry_schedule_uses_the_failed_attempt_and_preserves_caps(
    monkeypatch: pytest.MonkeyPatch,
    failed_attempt: int,
    base_seconds: int,
    expected_seconds: int,
) -> None:
    observed_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed_at

    monkeypatch.setattr(retry_module, "datetime", FixedClock)
    _patch_settings(monkeypatch, max_attempts=10, backoff_seconds=base_seconds)

    decision = retry_module.should_retry(SimpleNamespace(attempt_number=failed_attempt))

    assert decision.should_retry
    assert decision.next_attempt_at == observed_at + timedelta(seconds=expected_seconds)


class TestRetryDenylistNormalization:
    """Tests for denylist matching across short and fully-qualified names."""

    def test_short_denylist_matches_fully_qualified_exception(self, monkeypatch) -> None:
        _patch_settings(monkeypatch, denylist=["ValueError"])
        task = SimpleNamespace(attempt_number=1)

        decision = retry_module.should_retry(task, exception_type="builtins.ValueError")

        assert decision.should_retry is False
        assert decision.reason is not None
        assert "ValueError" in decision.reason

    def test_fully_qualified_denylist_matches_short_exception(self, monkeypatch) -> None:
        _patch_settings(monkeypatch, denylist=["builtins.ValueError"])
        task = SimpleNamespace(attempt_number=1)

        decision = retry_module.should_retry(task, exception_type="ValueError")

        assert decision.should_retry is False
        assert decision.reason is not None
        assert "builtins.ValueError" in decision.reason

    def test_custom_fully_qualified_denylist_requires_exact_module_match(self, monkeypatch) -> None:
        _patch_settings(monkeypatch, denylist=["myapp.errors.PermanentError"])
        task = SimpleNamespace(attempt_number=1)

        different_module = retry_module.should_retry(
            task, exception_type="other.module.PermanentError"
        )
        exact_module = retry_module.should_retry(task, exception_type="myapp.errors.PermanentError")

        assert different_module.should_retry is True
        assert exact_module.should_retry is False

    def test_non_matching_exception_still_retries(self, monkeypatch) -> None:
        _patch_settings(monkeypatch, denylist=["ValueError"])
        task = SimpleNamespace(attempt_number=1)

        decision = retry_module.should_retry(task, exception_type="RuntimeError")

        assert decision.should_retry is True
        assert decision.next_attempt_at is not None

    def test_max_attempts_still_takes_precedence(self, monkeypatch) -> None:
        _patch_settings(monkeypatch, max_attempts=3, denylist=["ValueError"])
        task = SimpleNamespace(attempt_number=3)

        decision = retry_module.should_retry(task, exception_type="RuntimeError")

        assert decision.should_retry is False
        assert decision.reason == "Max attempts (3) reached"

    def test_empty_exception_name_has_no_variants(self) -> None:
        assert retry_module._normalize_exception_name("   ") == set()

    def test_non_string_denylist_entries_are_ignored(self) -> None:
        assert retry_module._match_denylist_entry("ValueError", [None, 42]) is None
