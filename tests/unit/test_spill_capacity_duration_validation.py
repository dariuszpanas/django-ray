"""Resource-free spill duration validation and mocked operational boundaries."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ray.workflow.progress import spill_capacity as spill


@pytest.mark.parametrize("field", ["stale_lease_grace_seconds", "lock_timeout_seconds"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**400, id="integer-float-overflow"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(None, id="none"),
        pytest.param("1", id="string"),
        pytest.param(-1, id="negative"),
    ],
)
def test_invalid_duration_rejected_before_root_or_lock_access(
    field: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid duration touched a root or lock")

    monkeypatch.setattr(spill, "Lock", forbidden)
    monkeypatch.setattr(spill.SpillCapacityController, "_validate_root_directory", forbidden)
    monkeypatch.setattr(spill.SpillCapacityController, "_acquire_cross_process_lock", forbidden)
    config = spill.SpillCapacityConfig(Path("synthetic-spill-root"), **{field: value})  # type: ignore[arg-type]
    with pytest.raises(spill.WorkflowProgressSpillCapacityConfigurationError, match=rf"^{field} "):
        spill.SpillCapacityController(config)


def test_zero_timeout_remains_invalid() -> None:
    with pytest.raises(spill.WorkflowProgressSpillCapacityConfigurationError, match="lock_timeout"):
        spill.SpillCapacityConfig(Path("synthetic-spill-root"), lock_timeout_seconds=0).validated()


@pytest.mark.parametrize("grace,timeout", [(1, 1), (0.5, 0.5), (0, 2), (0.0, 2.5)])
def test_finite_values_preserve_frozen_config_and_zero_grace(
    grace: int | float, timeout: int | float
) -> None:
    config = spill.SpillCapacityConfig(
        Path("synthetic-spill-root"),
        stale_lease_grace_seconds=grace,
        lock_timeout_seconds=timeout,
    )
    assert config.validated() is config
    assert spill.SpillCapacityController(config).config is config
    assert type(config.stale_lease_grace_seconds) is type(grace)
    assert type(config.lock_timeout_seconds) is type(timeout)
    with pytest.raises(FrozenInstanceError):
        config.lock_timeout_seconds = 3  # type: ignore[misc]


@pytest.mark.parametrize("timeout", [1, 0.5])
def test_mocked_contention_stops_at_finite_deadline(
    timeout: int | float, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = spill.SpillCapacityController(
        spill.SpillCapacityConfig(Path("synthetic-spill-root"), lock_timeout_seconds=timeout)
    )
    clock = iter([0, timeout / 2, timeout])
    mkdir = Mock(side_effect=FileExistsError)
    sleep = Mock()
    monkeypatch.setattr(spill, "os", SimpleNamespace(mkdir=mkdir))
    monkeypatch.setattr(spill, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=sleep))
    with pytest.raises(spill.WorkflowProgressSpillCapacityUnavailableError) as caught:
        controller._acquire_cross_process_lock(Path("synthetic-spill-root"))
    assert caught.value.reason == "lock_contention"
    assert mkdir.call_count == 2
    sleep.assert_called_once_with(0.005)
    assert controller._last_refusal_reason == "lock_contention"
    assert controller._refused_workspaces == 1


def test_mocked_lock_failure_preserves_refusal_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = spill.SpillCapacityController(
        spill.SpillCapacityConfig(Path("synthetic-spill-root"))
    )
    sleep = Mock()
    monkeypatch.setattr(spill, "os", SimpleNamespace(mkdir=Mock(side_effect=PermissionError)))
    monkeypatch.setattr(spill, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleep))
    with pytest.raises(spill.WorkflowProgressSpillCapacityUnavailableError) as caught:
        controller._acquire_cross_process_lock(Path("synthetic-spill-root"))
    assert caught.value.reason == "lock_acquisition_failed"
    assert controller._last_refusal_reason == "lock_acquisition_failed"
    assert controller._refused_workspaces == 1
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "alive,observed_token,age_seconds,grace,expected",
    [
        (False, "original", 1, 30, spill.SpillOwnerDisposition.INDETERMINATE),
        (False, "original", 31, 30, spill.SpillOwnerDisposition.STALE),
        (False, "original", 30, 30, spill.SpillOwnerDisposition.STALE),
        (False, "original", -1, 30, spill.SpillOwnerDisposition.INDETERMINATE),
        (False, "original", 0, 0.0, spill.SpillOwnerDisposition.STALE),
        (True, "original", 1, 30, spill.SpillOwnerDisposition.LIVE),
        (True, "replacement", 1, 30, spill.SpillOwnerDisposition.STALE),
    ],
)
def test_mocked_owner_classification_preserves_grace_and_pid_reuse(
    alive: bool,
    observed_token: str,
    age_seconds: int,
    grace: int | float,
    expected: spill.SpillOwnerDisposition,
) -> None:
    controller = spill.SpillCapacityController(
        spill.SpillCapacityConfig(Path("synthetic-spill-root"), stale_lease_grace_seconds=grace),
        pid_alive_fn=lambda _pid: alive,
        process_start_token_fn=lambda _pid: observed_token,
    )
    assert (
        controller._classify_reservation_payload(
            {
                "creator_pid": 12345,
                "process_start_token": "original",
                "created_at_ns": 1_000_000_000,
            },
            now_ns=1_000_000_000 + age_seconds * 1_000_000_000,
        )
        == expected
    )
    assert (
        controller._classify_reservation_payload(
            {"creator_pid": "unknown", "process_start_token": "original", "created_at_ns": 0},
            now_ns=1_000_000_000,
        )
        == spill.SpillOwnerDisposition.INDETERMINATE
    )
