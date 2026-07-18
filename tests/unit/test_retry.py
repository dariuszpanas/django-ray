"""Unit tests for retry policy behavior."""

from __future__ import annotations

from types import SimpleNamespace

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

    def test_custom_fully_qualified_denylist_matches_by_class_name(self, monkeypatch) -> None:
        _patch_settings(monkeypatch, denylist=["myapp.errors.PermanentError"])
        task = SimpleNamespace(attempt_number=1)

        decision = retry_module.should_retry(task, exception_type="other.module.PermanentError")

        assert decision.should_retry is False

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
