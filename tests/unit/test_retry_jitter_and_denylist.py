"""Unit tests for retry exception denylist precision, backoff jitter, and polling bounds."""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from django_ray.conf.defaults import DEFAULTS
from django_ray.decorators import ray_task
from django_ray.runner import retry as retry_module
from django_ray.runner.polling import AdaptivePollingPolicy


@ray_task(max_attempts=2, retry_backoff_seconds=15, retry_jitter=False)
def _module_level_contract_sample() -> int:
    return 1


class BasePermanentDomainError(Exception):
    """Base domain exception used to verify subclass denylist matching."""


class ChildPermanentDomainError(BasePermanentDomainError):
    """Subclass of BasePermanentDomainError."""


class ValueError(Exception):  # noqa: A001 - intentional name collision with builtins.ValueError
    """Module-scoped exception intentionally sharing a builtin short name."""


def _patch_retry_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_attempts: int = 5,
    backoff_seconds: int = 60,
    jitter_ratio: float = 0.1,
    denylist: list[object] | None = None,
) -> None:
    monkeypatch.setattr(
        retry_module,
        "get_settings",
        lambda: {
            "MAX_TASK_ATTEMPTS": max_attempts,
            "RETRY_BACKOFF_SECONDS": backoff_seconds,
            "RETRY_BACKOFF_JITTER_RATIO": jitter_ratio,
            "RETRY_EXCEPTION_DENYLIST": denylist or [],
        },
    )


class TestRetryDenylistPrecisionAndSubclasses:
    """Qualified dotted denylist entries match exact module+class and subclasses only (F-062)."""

    def test_qualified_denylist_does_not_collide_on_short_class_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_retry_settings(
            monkeypatch,
            denylist=["payments.errors.PermanentError", "builtins.ValueError"],
        )
        task = SimpleNamespace(attempt_number=1)

        different_module = retry_module.should_retry(
            task, exception_type="shipping.errors.PermanentError"
        )
        local_shadow = retry_module.should_retry(
            task,
            exception_type=f"{ValueError.__module__}.{ValueError.__qualname__}",
        )
        exact_match = retry_module.should_retry(
            task, exception_type="payments.errors.PermanentError"
        )
        builtin_match = retry_module.should_retry(task, exception_type="builtins.ValueError")

        assert different_module.should_retry is True
        assert local_shadow.should_retry is True
        assert exact_match.should_retry is False
        assert builtin_match.should_retry is False

    def test_qualified_and_short_denylist_match_subclasses_via_mro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_path = f"{BasePermanentDomainError.__module__}.{BasePermanentDomainError.__qualname__}"
        child_path = (
            f"{ChildPermanentDomainError.__module__}.{ChildPermanentDomainError.__qualname__}"
        )
        _patch_retry_settings(
            monkeypatch,
            denylist=[base_path, "builtins.LookupError"],
        )
        task = SimpleNamespace(attempt_number=1)

        child_by_path = retry_module.should_retry(task, exception_type=child_path)
        child_by_instance = retry_module.should_retry(
            task, exception=ChildPermanentDomainError("bad state")
        )
        key_error = retry_module.should_retry(task, exception_type="builtins.KeyError")
        index_error = retry_module.should_retry(task, exception_type="IndexError")
        runtime_error = retry_module.should_retry(task, exception_type="builtins.RuntimeError")

        assert child_by_path.should_retry is False
        assert child_by_instance.should_retry is False
        assert key_error.should_retry is False
        assert index_error.should_retry is False
        assert runtime_error.should_retry is True

    def test_short_denylist_entry_matches_builtin_and_subclasses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_retry_settings(monkeypatch, denylist=["LookupError"])
        task = SimpleNamespace(attempt_number=1)

        assert (
            retry_module.should_retry(task, exception_type="builtins.KeyError").should_retry
            is False
        )
        assert retry_module.should_retry(task, exception_type="KeyError").should_retry is False
        assert retry_module.should_retry(task, exception_type="RuntimeError").should_retry is True

    def test_denylist_lookup_never_imports_unloaded_modules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_retry_settings(monkeypatch, denylist=["unloaded_pkg_one.ErrorA"])
        task = SimpleNamespace(attempt_number=1)

        decision = retry_module.should_retry(task, exception_type="unloaded_pkg_two.ErrorA")

        assert decision.should_retry is True
        assert "unloaded_pkg_one" not in sys.modules
        assert "unloaded_pkg_two" not in sys.modules


class TestRetryBackoffJitter:
    """Bounded deterministic retry backoff jitter prevents thundering herds (F-062)."""

    def test_default_jitter_ratio_is_configured(self) -> None:
        assert DEFAULTS["RETRY_BACKOFF_JITTER_RATIO"] == 0.1

    @pytest.mark.parametrize(
        ("sample", "expected_seconds"),
        [
            (0.0, 60.0),
            (0.5, 57.0),
            (1.0, 54.0),
        ],
    )
    def test_calculate_backoff_applies_bounded_downward_jitter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sample: float,
        expected_seconds: float,
    ) -> None:
        _patch_retry_settings(monkeypatch, backoff_seconds=60, jitter_ratio=0.1)

        delay = retry_module.calculate_backoff(1, random_value=lambda: sample)

        assert delay == timedelta(seconds=expected_seconds)

    def test_jitter_at_one_hour_cap_spreads_below_ceiling_without_exceeding_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_retry_settings(monkeypatch, backoff_seconds=60, jitter_ratio=0.1)

        rng_a = random.Random(101)
        rng_b = random.Random(202)
        delays_a = [retry_module.calculate_backoff(10, random_value=rng_a.random) for _ in range(5)]
        delays_b = [retry_module.calculate_backoff(10, random_value=rng_b.random) for _ in range(5)]

        assert delays_a != delays_b
        for delay in (*delays_a, *delays_b):
            assert timedelta(seconds=3240.0) <= delay <= timedelta(seconds=3600.0)

    def test_should_retry_accepts_deterministic_random_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed_at = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return observed_at

        monkeypatch.setattr(retry_module, "datetime", FixedClock)
        _patch_retry_settings(monkeypatch, backoff_seconds=100, jitter_ratio=0.2)

        decision = retry_module.should_retry(
            SimpleNamespace(attempt_number=2),
            exception_type="RuntimeError",
            random_value=lambda: 0.5,
        )

        # Attempt 2 base = 200s; 0.2 * 0.5 = 10% downward jitter => 180s.
        assert decision.should_retry is True
        assert decision.next_attempt_at == observed_at + timedelta(seconds=180.0)

    def test_polling_policy_clamps_invalid_bounds_safely(self) -> None:
        policy = AdaptivePollingPolicy(
            base_interval_seconds=0.5,
            max_interval_seconds=0.2,
            backoff_multiplier=0.5,
            jitter_ratio=2.0,
            random_value=lambda: 1.0,
        )
        assert policy.next_delay(activity=False) == 0.5
        assert policy.backoff_multiplier == 1.0
        assert policy.jitter_ratio == 1.0


class TestRetryContractAndDenylistEdgeBranches:
    """Cover task contract resolution and exception-matching edge branches in runner/retry.py."""

    def test_resolve_task_contract_direct_cached_loaded_and_fallback_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from django_ray.decorators import RayTaskContract

        # Direct decorated callable
        direct = retry_module.resolve_task_contract(_module_level_contract_sample)
        assert direct is not None and direct.max_attempts == 2

        # Pre-cached _ray_task_contract on execution object
        cached_contract = RayTaskContract(max_attempts=4, retry_backoff_seconds=10)
        assert (
            retry_module.resolve_task_contract(SimpleNamespace(_ray_task_contract=cached_contract))
            is cached_contract
        )

        # Invalid/blank/overlong callable paths return None cleanly
        assert retry_module.resolve_task_contract(SimpleNamespace(callable_path=None)) is None
        assert retry_module.resolve_task_contract(SimpleNamespace(callable_path="   .  ")) is None
        assert (
            retry_module.resolve_task_contract(SimpleNamespace(callable_path="a." + ("b" * 600)))
            is None
        )

        # Loaded module lookup via sys.modules: missing attr falls through to resolver;
        # resolver exception returns None; loaded module with decorated attr returns contract
        resolved = retry_module.resolve_task_contract(
            SimpleNamespace(callable_path=f"{__name__}._module_level_contract_sample")
        )
        assert resolved is direct

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: (_ for _ in ()).throw(ImportError("blocked")),
        )
        assert (
            retry_module.resolve_task_contract(
                SimpleNamespace(callable_path=f"{__name__}.does_not_exist")
            )
            is None
        )

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: _module_level_contract_sample,
        )
        assert (
            retry_module.resolve_task_contract(SimpleNamespace(callable_path="non_ident!a.fn"))
            is direct
        )

    def test_exception_resolution_and_denylist_edge_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from django_ray.decorators import RayTaskContract

        assert retry_module._normalize_exception_name("  ") == set()
        assert retry_module._normalize_exception_name("ValueError") == {
            "ValueError",
            "builtins.ValueError",
        }
        assert retry_module._normalize_exception_name("pkg.MyErr") == {"pkg.MyErr"}

        assert retry_module._resolve_loaded_exception_class("") is None
        assert retry_module._resolve_loaded_exception_class("a" * 300) is None
        assert retry_module._resolve_loaded_exception_class("not-ident") is None
        assert retry_module._resolve_loaded_exception_class("int") is None
        assert retry_module._resolve_loaded_exception_class("bad.ident!") is None
        assert retry_module._resolve_loaded_exception_class(f"{__name__}.NoSuchExc") is None
        assert retry_module._resolve_loaded_exception_class("KeyError") is KeyError

        # _match_denylist_entry edge paths: exception class, empty inputs, non-str/blank entries,
        # short name on unloaded module, and subclass lookup via loaded deny_cls
        assert retry_module._match_denylist_entry(None, ["ValueError"], exception=None) is None
        assert (
            retry_module._match_denylist_entry(
                None,
                [123, "   ", "builtins.KeyError"],
                exception=KeyError,
            )
            == "builtins.KeyError"
        )
        assert (
            retry_module._match_denylist_entry("ValueError", ["builtins.ValueError"])
            == "builtins.ValueError"
        )
        assert (
            retry_module._match_denylist_entry("unloaded_mod.CustomBoom", ["CustomBoom"])
            == "CustomBoom"
        )

        # Force subclass fallback branch (mro sets empty while exc_cls is loaded)
        monkeypatch.setattr(retry_module, "_exception_mro_names", lambda _cls: (set(), set()))
        base_path = f"{BasePermanentDomainError.__module__}.{BasePermanentDomainError.__qualname__}"
        assert (
            retry_module._match_denylist_entry(
                None,
                [base_path],
                exception=ChildPermanentDomainError,
            )
            == base_path
        )
        assert (
            retry_module._match_denylist_entry(
                None,
                ["LookupError"],
                exception=KeyError,
            )
            == "LookupError"
        )

        # should_retry: max attempts reached, exception class label, and contract overrides
        _patch_retry_settings(
            monkeypatch,
            max_attempts=3,
            backoff_seconds=60,
            jitter_ratio=0.5,
            denylist=["builtins.KeyError"],
        )
        maxed = retry_module.should_retry(SimpleNamespace(attempt_number=3))
        assert maxed.should_retry is False
        assert "Max attempts (3) reached" in (maxed.reason or "")

        denied_cls = retry_module.should_retry(
            SimpleNamespace(attempt_number=1),
            exception=KeyError,
        )
        assert denied_cls.should_retry is False
        assert "builtins.KeyError" in (denied_cls.reason or "")

        contract = RayTaskContract(max_attempts=5, retry_backoff_seconds=10, retry_jitter=False)
        decision = retry_module.should_retry(
            SimpleNamespace(attempt_number=1),
            contract=contract,
            random_value=lambda: 1.0,
        )
        assert decision.should_retry is True
        assert decision.next_attempt_at is not None
