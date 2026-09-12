"""Unit tests for worker command runtime/control-flow helpers."""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.conf import settings as django_settings
from django.core.management import CommandError
from django.core.management.base import CommandParser

import django_ray
from django_ray.backends import RayTaskBackend
from django_ray.execution_codec import ExecutionRequestEncodeError
from django_ray.execution_protocol import (
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    WORKER_CAPABILITY_SCHEMA_VERSION,
    ExecutionProtocolRange,
)
from django_ray.input_storage import EXTERNAL_INPUT_PLACEHOLDER
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runner.polling import AdaptivePollingPolicy
from django_ray.runner.ray_core import RayCoreHandle
from tests.integration import test_cohort_claim_storage as _cohort_claim_tests
from tests.integration import test_cohort_worker as _cohort_worker_tests
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema
from tests.unit import test_cohort_worker_controller as _cohort_controller_tests
from tests.unit.test_worker_mode_selection import _assert_current_startup_lease

cohort_controller_case = _cohort_controller_tests.case
current_claim_case = _cohort_claim_tests.case


@pytest.fixture
def current_claim_cleanup():
    yield from _cohort_claim_tests.isolated_sqlite_ledger_maintenance.__wrapped__()


@pytest.fixture
def current_worker_case(current_claim_cleanup, current_claim_case, monkeypatch):
    from django_ray.management.commands import django_ray_worker as worker
    from django_ray.runner import (
        cohort_completion,
        cohort_core_submission,
        cohort_dispatch,
        cohort_preparation,
    )

    case = current_claim_case

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.now

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(cohort_dispatch, "datetime", Clock)
    monkeypatch.setattr(cohort_completion, "_clock", lambda: case.now)
    monkeypatch.setattr(cohort_preparation, "_clock", lambda: (case.now, 100.0))
    monkeypatch.setattr(cohort_core_submission, "_clock", lambda: (case.now, 100.0))
    return case


@pytest.fixture
def historical_worker(preactivation_protocol_schema, monkeypatch):
    """Named protocol-1 factories on the actual stopped preactivation schema."""
    from django_ray.lifecycle import cancel_task

    def task(**fields):
        return RayTaskExecution.objects.create(execution_protocol_version=1, **fields)

    def lease(**fields):
        return TaskWorkerLease.objects.create(
            capability_schema_version=1,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
            **fields,
        )

    def lease_for(cmd):
        row = lease(worker_id=cmd.worker_id, hostname="historical-host", pid=12345)
        cmd.lease = row
        cmd.lease_identity = WorkerLeaseIdentity(
            row.worker_id, row.hostname, row.pid, row.started_at
        )
        return row

    def finalize(task, **fences):
        return cancel_task(task, supported_protocols=ExecutionProtocolRange(1, 1), **fences)

    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.finalize_cancellation", finalize
    )
    return SimpleNamespace(task=task, lease=lease, lease_for=lease_for)


@pytest.mark.parametrize("disconnected", [True, False])
def test_current_shutdown_waits_for_owned_ray_processes_before_accepting_disconnect(
    monkeypatch, disconnected
):
    import ray
    import ray.util.client as client

    from django_ray.runner.cohort_connection import CoreConnectionPhase

    command = Command()
    command.execution_mode = "local"
    state = SimpleNamespace(cleanup=None, calls=[])
    ticket = object()

    def cleanup(actual_ticket, *, cleanup, timeout_seconds):
        assert actual_ticket is ticket and timeout_seconds == 5.0
        state.cleanup = cleanup()

    def poll(actual_ticket):
        assert actual_ticket is ticket
        return SimpleNamespace(
            phase=(
                CoreConnectionPhase.CONNECTED
                if state.cleanup is None
                else CoreConnectionPhase.CLEANED
                if state.cleanup
                else CoreConnectionPhase.BLOCKED
            ),
            callback_running=False,
        )

    monkeypatch.setattr(ray, "shutdown", lambda **kwargs: state.calls.append(kwargs))
    monkeypatch.setattr(ray, "is_initialized", lambda: not disconnected)
    monkeypatch.setattr(client, "num_connected_contexts", lambda: 0 if disconnected else 1)
    command._cohort_controller = SimpleNamespace(
        connection=SimpleNamespace(poll=poll, begin_cleanup=cleanup),
        connection_ticket=ticket,
        lifecycle=SimpleNamespace(outstanding=None),
        adapter=None,
        invalidate=lambda: None,
        poll_stopped_cleanup=lambda: None,
    )
    command._shutdown_cohort()
    assert state.calls == [{"wait_for_processes": True}]
    assert state.cleanup is disconnected
    assert command.shutdown_exit_code is None if disconnected else command.shutdown_exit_code == 1


class CustomRayTaskBackend(RayTaskBackend):
    """Importable subclass used to prove alias discovery is type-aware."""


class CapturingStdout:
    """Minimal stdout sink compatible with BaseCommand output usage."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str = "", ending: str = "\n") -> None:
        self.messages.append(f"{message}{ending}")

    def flush(self) -> None:
        return


def _make_command(*, worker_id: str = "unit-worker") -> Command:
    cmd = Command()
    cmd.stdout = CapturingStdout()
    cmd.style = cmd.style
    cmd.worker_id = worker_id
    cmd.active_tasks = {}
    cmd.execution_mode = "sync"
    cmd.sync_mode = False
    cmd.local_mode = False
    cmd.cluster_address = None
    cmd.ray_core_runner = None
    return cmd


def _set_lease_identity(cmd: Command) -> WorkerLeaseIdentity:
    identity = WorkerLeaseIdentity(
        worker_id=cmd.worker_id,
        hostname="unit-host",
        pid=12345,
        started_at=datetime.now(UTC),
    )
    cmd.lease_identity = identity
    return identity


def _prepare_cohort_startup(cmd, monkeypatch, case, *, error=None):
    """Run the actual controller with an owned fake callback and no Ray process."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    monkeypatch.setattr(
        django_settings,
        "TASKS",
        {
            "default": {"BACKEND": "django_ray.backends.RayTaskBackend", "QUEUES": ["default"]},
        },
    )
    case.ray_initializations = []
    ray = sys.modules["ray"]

    def initialize(**arguments):
        # The parent wait hook verifies the real lease before releasing this
        # callback. The retained connection thread must not open an ORM writer.
        case.ray_initializations.append(arguments)
        if error is not None:
            raise RuntimeError(error)

    monkeypatch.setattr(ray, "is_initialized", lambda: False, raising=False)
    monkeypatch.setattr(ray, "init", initialize, raising=False)
    monkeypatch.setattr(cmd, "_initialize_ray_execution", lambda: pytest.fail("Legacy startup"))
    monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(cmd, "shutdown", lambda: None)


def _run_cohort_startup_cycle(cmd, monkeypatch, case):
    """Drive pending/finished owned connect in two bounded production-loop ticks."""
    waits = []
    for method in (
        "_poll_cohort_completions",
        "_expire_cohort_tasks",
        "_recover_cohort_jobs",
        "_poll_cohort_timeouts",
        "_poll_cohort_cancellations",
        "_claim_and_process_cohort_tasks",
    ):
        monkeypatch.setattr(cmd, method, lambda *args: 0)

    def wait(_delay):
        _assert_current_startup_lease(cmd)
        waits.append(True)
        if len(waits) == 1:
            assert cmd.ray_core_runner is None
            assert len(case.threads) == 1 and not case.ray_initializations
            case.threads[0].run()
        else:
            assert len(waits) == 2
            cmd.shutdown_requested = True

    monkeypatch.setattr(cmd, "_wait_for_poll_deadline", wait)
    monkeypatch.setattr(cmd, "run_loop", lambda **arguments: Command.run_loop(cmd, **arguments))
    return waits


class FakeClock:
    """Monotonic test clock advanced only by worker sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestWorkerCommandRuntime:
    """Runtime helper and control-flow behavior."""

    def test_add_arguments_registers_expected_flags(self) -> None:
        cmd = _make_command()
        parser = CommandParser(prog="manage.py django_ray_worker", missing_args_message="")

        cmd.add_arguments(parser)
        args = parser.parse_args(["--queue", "high", "--concurrency", "3", "--sync"])

        assert args.queue == "high"
        assert args.concurrency == 3
        assert args.sync is True
        assert args.local is False
        assert args.cluster is None

    @pytest.mark.parametrize(
        "arguments",
        [
            ["--queue", "a", "--queues", "b"],
            ["--queue", "a", "--all-queues"],
            ["--queues", "a", "--all-queues"],
            ["--sync", "--local"],
            ["--sync", "--cluster", "ray://cluster:10001"],
            ["--local", "--cluster", "ray://cluster:10001"],
        ],
    )
    def test_add_arguments_rejects_conflicting_selectors(self, arguments: list[str]) -> None:
        cmd = _make_command()
        parser = CommandParser(prog="manage.py django_ray_worker", missing_args_message="")
        cmd.add_arguments(parser)

        with pytest.raises(CommandError, match="not allowed with argument"):
            parser.parse_args(arguments)

    def test_parse_queues_supports_all_variants(self, monkeypatch) -> None:
        cmd = _make_command()

        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "default": {
                    "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
                    "QUEUES": ["celery-default"],
                },
                "ray": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["ray-batch", "shared"],
                },
                "ray-secondary": {
                    "BACKEND": ("tests.unit.test_worker_command_runtime.CustomRayTaskBackend"),
                    "QUEUES": ["shared", "ray-gpu"],
                    "OPTIONS": {"RAY_ADDRESS": "ray://analytics:10001"},
                },
                "ray-defaulted": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                },
            },
            raising=False,
        )
        assert cmd._parse_queues({"all_queues": True}) == [
            "ray-batch",
            "shared",
            "ray-gpu",
            "default",
        ]
        assert cmd._parse_queues({"queues": ["a", "b"]}) == ["a", "b"]
        assert cmd._parse_queues({"queue": "a, b, ,c"}) == ["a", "b", "c"]
        assert cmd._parse_queues({"queue": "single"}) == ["single"]
        assert cmd._parse_queues({}) == ["default"]

    @pytest.mark.parametrize(
        "selection",
        [
            {"queue": ", ,"},
            {"queues": []},
            {"queues": ["default", ""]},
        ],
    )
    def test_parse_queues_rejects_empty_explicit_selection(
        self, selection: dict[str, object]
    ) -> None:
        cmd = _make_command()

        with pytest.raises(CommandError, match="at least one non-empty queue"):
            cmd._parse_queues(selection)

    @pytest.mark.parametrize(
        ("tasks_config", "message"),
        [
            (
                {
                    "default": {
                        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
                        "QUEUES": ["default"],
                    }
                },
                "no TASKS backend using RayTaskBackend",
            ),
            (
                {
                    "ray": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "QUEUES": [],
                    }
                },
                "has no enumerable QUEUES",
            ),
            (
                {
                    "ray": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "QUEUES": "default",
                    }
                },
                "QUEUES must be a collection",
            ),
            (
                {
                    "ray": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "QUEUES": ["default", " "],
                    }
                },
                "QUEUES must contain non-empty strings",
            ),
            (
                {
                    "ray": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "QUEUES": {"default", 1},
                    }
                },
                "QUEUES must contain non-empty strings",
            ),
        ],
    )
    def test_parse_all_queues_fails_closed(
        self,
        monkeypatch,
        tasks_config: dict[str, object],
        message: str,
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(django_settings, "TASKS", tasks_config, raising=False)

        with pytest.raises(CommandError, match=message):
            cmd._parse_queues({"all_queues": True})

    @pytest.mark.parametrize(
        ("mode_options", "runner"),
        [
            ({"local": True}, "ray_job"),
            ({"cluster": "ray://worker-cluster:10001"}, "ray_job"),
            ({}, "ray_core"),
        ],
    )
    def test_parse_all_queues_rejects_mixed_targets_in_ray_core_mode(
        self,
        monkeypatch,
        mode_options: dict[str, object],
        runner: str,
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": runner, "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "default": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["default"],
                },
                "analytics": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["analytics"],
                    "OPTIONS": {"RAY_ADDRESS": "ray://analytics:10001"},
                },
            },
            raising=False,
        )

        with pytest.raises(CommandError, match="different RAY_ADDRESS values"):
            cmd._parse_queues({"all_queues": True, **mode_options})

    def test_parse_all_queues_skips_ray_job_only_queues_before_target_check(
        self, monkeypatch
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "default": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["default"],
                },
                "jobs": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["ray-data"],
                    "OPTIONS": {
                        "RAY_ADDRESS": "ray://jobs:10001",
                        "RAY_JOB_ONLY": True,
                    },
                },
            },
            raising=False,
        )

        assert cmd._parse_queues({"all_queues": True, "local": True}) == ["default"]
        assert "Skipping Ray Job-only queue(s) [ray-data]" in "".join(cmd.stdout.messages)

    def test_parse_all_queues_fails_when_every_queue_requires_ray_job(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "jobs": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["ray-data"],
                    "OPTIONS": {"RAY_JOB_ONLY": True},
                }
            },
            raising=False,
        )

        with pytest.raises(CommandError, match="no queues compatible with this Ray Core"):
            cmd._parse_queues({"all_queues": True, "local": True})

    @pytest.mark.parametrize(
        ("mode_options", "runner", "mode_label"),
        [
            ({"local": True}, "ray_job", "Ray Core"),
            ({"cluster": "ray://worker:10001"}, "ray_job", "Ray Core"),
            ({"sync": True}, "ray_job", "synchronous"),
            ({}, "ray_core", "Ray Core"),
        ],
    )
    @pytest.mark.parametrize(
        "selection",
        [
            {"queue": "ray-data"},
            {"queue": "default,ray-data"},
            {"queues": ["default", "ray-data"]},
        ],
    )
    def test_explicit_queue_selection_rejects_ray_job_only_queue(
        self,
        monkeypatch,
        mode_options: dict[str, object],
        runner: str,
        mode_label: str,
        selection: dict[str, object],
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": runner, "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "jobs": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["ray-data"],
                    "OPTIONS": {"RAY_JOB_ONLY": True},
                }
            },
            raising=False,
        )

        with pytest.raises(CommandError, match=rf"{mode_label} worker cannot claim"):
            cmd._parse_queues({**selection, **mode_options})

    def test_ray_job_worker_accepts_ray_job_only_queue(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )

        assert cmd._parse_queues({"queue": "ray-data"}) == ["ray-data"]

    def test_restricted_alias_wins_when_queue_is_shared(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "general": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["shared"],
                },
                "jobs": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["shared"],
                    "OPTIONS": {"RAY_JOB_ONLY": True},
                },
            },
            raising=False,
        )

        with pytest.raises(CommandError, match=r"Ray Job-only queue\(s\) \[shared\]"):
            cmd._parse_queues({"queue": "shared", "local": True})

    def test_unconfigured_explicit_queue_remains_compatible_with_ray_core(
        self, monkeypatch
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )

        assert cmd._parse_queues({"queue": "open-ended", "local": True}) == ["open-ended"]

    def test_explicit_queue_does_not_import_unrelated_backend(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "default": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["default"],
                },
                "unavailable": {
                    "BACKEND": "missing_package.backends.UnavailableBackend",
                    "QUEUES": ["other"],
                },
            },
            raising=False,
        )

        assert cmd._parse_queues({"queue": "default", "local": True}) == ["default"]

    def test_unavailable_ray_job_only_backend_fails_closed(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "reserved": {
                    "BACKEND": "missing_package.backends.UnavailableBackend",
                    "QUEUES": ["ray-data"],
                    "OPTIONS": {"RAY_JOB_ONLY": True},
                }
            },
            raising=False,
        )

        with pytest.raises(CommandError, match="while validating RAY_JOB_ONLY"):
            cmd._parse_queues({"queue": "ray-data", "local": True})

    def test_non_ray_backend_ray_job_only_option_does_not_reserve_queue(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "immediate": {
                    "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
                    "QUEUES": ["shared-option-name"],
                    "OPTIONS": {"RAY_JOB_ONLY": True},
                }
            },
            raising=False,
        )

        assert cmd._parse_queues({"queue": "shared-option-name", "local": True}) == [
            "shared-option-name"
        ]

    def test_raw_ray_job_only_reservation_rejects_empty_queue_set(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "reserved": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": [],
                    "OPTIONS": {"RAY_JOB_ONLY": True},
                }
            },
            raising=False,
        )

        with pytest.raises(CommandError, match="must declare at least one queue"):
            cmd._parse_queues({"queue": "ray-data", "local": True})

    @pytest.mark.parametrize("mode_options", [{}, {"local": True}])
    def test_parse_queues_rejects_invalid_ray_job_only_policy(
        self, monkeypatch, mode_options: dict[str, object]
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"RUNNER": "ray_job", "RAY_ADDRESS": "auto"},
        )
        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {
                "jobs": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": ["ray-data"],
                    "OPTIONS": {"RAY_JOB_ONLY": "true"},
                }
            },
            raising=False,
        )

        with pytest.raises(CommandError, match="RAY_JOB_ONLY.*boolean"):
            cmd._parse_queues({"queue": "ray-data", **mode_options})

    def test_init_local_ray_clears_env_and_initializes(self, monkeypatch) -> None:
        cmd = _make_command()
        init_calls: list[dict[str, object]] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: False,
            init=lambda **kwargs: init_calls.append(kwargs),
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setenv("RAY_ADDRESS", "ray://old:10001")
        monkeypatch.setenv("RAY_RUNTIME_ENV_HOOK", "uv-hook")

        cmd._init_local_ray()

        assert "RAY_ADDRESS" not in os.environ
        assert "RAY_RUNTIME_ENV_HOOK" not in os.environ
        assert len(init_calls) == 1
        assert init_calls[0]["dashboard_port"] == 8265

    def test_init_cluster_ray_reconnects_and_reports_resources(self, monkeypatch) -> None:
        cmd = _make_command()
        calls: list[str] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: True,
            shutdown=lambda: calls.append("shutdown"),
            init=lambda **kwargs: calls.append(f"init:{kwargs['address']}"),
            cluster_resources=lambda: {"CPU": 4},
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        cmd._init_cluster_ray("ray://cluster:10001")

        assert calls == ["shutdown", "init:ray://cluster:10001"]

    def test_setup_signal_handlers_registers_both_signals(self, monkeypatch) -> None:
        cmd = _make_command()
        seen: list[int] = []

        def fake_signal(sig, _handler):
            seen.append(sig)

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.signal.signal", fake_signal
        )

        cmd.setup_signal_handlers()

        assert signal.SIGTERM in seen
        assert signal.SIGINT in seen

    def test_handle_shutdown_signal_sets_flag(self) -> None:
        cmd = _make_command()
        assert cmd.shutdown_requested is False

        cmd.handle_shutdown_signal(signal.SIGTERM, None)

        assert cmd.shutdown_requested is True
        assert cmd._shutdown_event.is_set()
        assert cmd.shutdown_signal == signal.SIGTERM
        assert cmd.shutdown_exit_code == 143

    def test_idle_poll_wait_is_interrupted_by_shutdown_signal(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=60.0,
            max_interval_seconds=60.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 120.0
        cmd.timeout_check_interval = 120.0
        cmd.cancellation_interval = 120.0
        cmd.lease_cleanup_interval = 120.0
        waits: list[float] = []
        wakeups: list[bool] = []

        class ShutdownEvent:
            def wait(self, timeout: float) -> bool:
                waits.append(timeout)
                cmd.handle_shutdown_signal(signal.SIGTERM, None)
                return True

            def set(self) -> None:
                wakeups.append(True)

        monkeypatch.setattr(cmd, "_shutdown_event", ShutdownEvent())
        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)
        monkeypatch.setattr(cmd, "claim_and_process_tasks", lambda *_: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda *_: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda *_: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda *_: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=120.0)

        assert waits == [0.1]
        assert wakeups == [True]
        assert cmd.shutdown_requested is True

    def test_worker_startup_output_respects_django_verbosity(self) -> None:
        cmd = _make_command()
        cmd.verbosity = 0
        cmd._write_worker_output("hidden")
        assert cmd.stdout.messages == []

        cmd.verbosity = 1
        cmd._write_worker_output("visible")
        assert cmd.stdout.messages == ["visible\n"]

    def test_run_loop_does_not_claim_after_signal_during_poll(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace())
        calls: list[str] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: calls.append("heartbeat"))

        def stop_during_poll() -> None:
            calls.append("poll")
            cmd.handle_shutdown_signal(signal.SIGTERM, None)

        monkeypatch.setattr(cmd, "poll_ray_core_tasks", stop_during_poll)
        monkeypatch.setattr(cmd, "claim_and_process_tasks", lambda *_: calls.append("claim"))
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.time", lambda: 100.0
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda *_: None)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=1)

        assert calls == ["heartbeat", "poll"]

    @pytest.mark.django_db
    def test_cli_signal_shutdown_uses_documented_exit_code(
        self, monkeypatch, cohort_controller_case
    ) -> None:
        cmd = _make_command()
        _prepare_cohort_startup(cmd, monkeypatch, cohort_controller_case)
        cmd._called_from_command_line = True
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 1},
        )
        monkeypatch.setattr(
            cmd,
            "run_loop",
            lambda **_kwargs: cmd.handle_shutdown_signal(signal.SIGINT, None),
        )
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=1,
                sync=True,
                local=False,
                cluster=None,
                verbosity=1,
            )

        assert exc_info.value.code == 130

        _assert_current_startup_lease(cmd)
        assert cmd._cohort_controller is not None
        assert not cohort_controller_case.threads

    def test_run_loop_executes_reconciliation_cycle_once(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace())
        cmd.reconciliation_interval = 0
        cmd.last_reconciliation = 0

        calls: list[str] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: calls.append("heartbeat"))
        monkeypatch.setattr(cmd, "poll_ray_core_tasks", lambda: calls.append("poll"))

        def fake_claim(_queues, _concurrency):
            calls.append("claim")

        monkeypatch.setattr(cmd, "claim_and_process_tasks", fake_claim)
        monkeypatch.setattr(
            cmd,
            "reconcile_tasks",
            lambda queues: calls.append(f"reconcile:{','.join(queues)}"),
        )
        monkeypatch.setattr(
            cmd,
            "detect_stuck_tasks",
            lambda queues: calls.append(f"stuck:{','.join(queues)}"),
        )
        monkeypatch.setattr(
            cmd,
            "process_cancellations",
            lambda queues: calls.append(f"cancel:{','.join(queues)}"),
        )

        def cleanup_once():
            calls.append("cleanup")
            cmd.shutdown_requested = True

        monkeypatch.setattr(cmd, "cleanup_expired_leases", cleanup_once)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.time", lambda: 100.0
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda *_: None)

        cmd.run_loop(queues=["default"], concurrency=2, heartbeat_interval=1)

        assert calls == [
            "heartbeat",
            "poll",
            "claim",
            "cancel:default",
            "reconcile:default",
            "stuck:default",
            "cleanup",
        ]

    def test_run_loop_stops_immediately_after_heartbeat_loses_lease(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace())
        calls: list[str] = []

        def lose_lease() -> None:
            calls.append("heartbeat")
            cmd._request_shutdown_for_lease_loss("replacement owner")

        monkeypatch.setattr(cmd, "send_heartbeat", lose_lease)
        monkeypatch.setattr(cmd, "poll_ray_core_tasks", lambda: calls.append("poll"))
        monkeypatch.setattr(cmd, "claim_and_process_tasks", lambda *_: calls.append("claim"))
        monkeypatch.setattr(cmd, "process_cancellations", lambda _queues: calls.append("cancel"))
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _queues: calls.append("reconcile"))
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _queues: calls.append("stuck"))
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: calls.append("cleanup"))

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=1)

        assert calls == ["heartbeat"]

    def test_run_loop_stops_after_preclaim_lease_loss(self, monkeypatch) -> None:
        cmd = _make_command()
        calls: list[str] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: calls.append("heartbeat"))

        def lose_lease(_queues, _concurrency):
            calls.append("claim")
            cmd._request_shutdown_for_lease_loss("replacement owner")
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", lose_lease)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _queues: calls.append("cancel"))
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _queues: calls.append("reconcile"))
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _queues: calls.append("stuck"))
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: calls.append("cleanup"))

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=1)

        assert calls == ["heartbeat", "claim"]

    def test_idle_claim_backoff_does_not_delay_cancellation_schedule(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=0.8,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 10.0
        cmd.timeout_check_interval = 10.0
        cmd.cancellation_interval = 0.15
        cmd.lease_cleanup_interval = 10.0
        claims: list[float] = []
        cancellations: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def claim(_queues, _concurrency):
            claims.append(clock.now)
            if len(claims) == 4:
                cmd.shutdown_requested = True
            return 0

        def cancel(_queues) -> int:
            cancellations.append(clock.now)
            return int(len(cancellations) == 2)

        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "process_cancellations", cancel)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", clock.sleep)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert cancellations[:3] == pytest.approx([0.0, 0.15, 0.3])
        assert claims == pytest.approx([0.0, 0.1, 0.25, 0.35])

    def test_heartbeat_deadline_interrupts_maximum_idle_claim_delay(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=1.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 10.0
        cmd.timeout_check_interval = 10.0
        cmd.cancellation_interval = 10.0
        cmd.lease_cleanup_interval = 10.0
        heartbeats: list[float] = []
        claims: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: heartbeats.append(clock.now))

        def claim(_queues, _concurrency):
            claims.append(clock.now)
            if clock.now >= 0.7:
                cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _queues: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", clock.sleep)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=0.25)

        assert claims == pytest.approx([0.0, 0.1, 0.3, 0.7])
        assert heartbeats == pytest.approx([0.0, 0.25, 0.5])

    def test_pending_ray_core_work_keeps_independent_completion_cadence(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=1))
        cmd.completion_poll_interval = 0.1
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.5,
            max_interval_seconds=1.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 10.0
        cmd.timeout_check_interval = 10.0
        cmd.cancellation_interval = 10.0
        cmd.lease_cleanup_interval = 10.0
        polls: list[float] = []
        claims: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def poll() -> int:
            polls.append(clock.now)
            if len(polls) == 4:
                cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "poll_ray_core_tasks", poll)
        monkeypatch.setattr(
            cmd,
            "claim_and_process_tasks",
            lambda _queues, _concurrency: claims.append(clock.now) or 0,
        )
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _queues: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", clock.sleep)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert polls == pytest.approx([0.0, 0.1, 0.2, 0.3])
        assert claims == [0.0]

    def test_ray_job_completion_frees_capacity_before_the_recovery_clock(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "ray"
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.5,
            max_interval_seconds=10.0,
            random_value=lambda: 0.0,
        )
        admitted: list[float] = []
        polls: list[float] = []
        recoveries: list[float] = []

        def claim(_queues, _concurrency):
            if cmd.active_tasks:
                return 0
            admitted.append(clock.now)
            cmd.active_tasks[1] = "raysubmit_pending"
            if len(admitted) == 2:
                cmd.shutdown_requested = True
            return 1

        def poll():
            polls.append(clock.now)
            # Fake I/O: the remote completion was durable after 50 ms.
            if clock.now >= 0.05:
                cmd.active_tasks.clear()
                return 1
            return 0

        def recover(_queues):
            recoveries.append(clock.now)
            if clock.now >= 30:
                cmd.active_tasks.clear()
            return 0

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)
        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(cmd, "poll_ray_job_completions", poll, raising=False)
        monkeypatch.setattr(cmd, "reconcile_tasks", recover)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _queues: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _queues: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic", clock.monotonic
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", clock.sleep)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert admitted == pytest.approx([0.0, 0.25])
        assert polls == pytest.approx([0.0, 0.25])
        assert recoveries == [0.0]

    def test_shutdown_during_job_receipt_poll_prevents_the_advanced_claim(
        self, monkeypatch
    ) -> None:
        cmd = _make_command()
        cmd.execution_mode = "ray"
        cmd.active_tasks = {1: "raysubmit_pending"}
        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def poll():
            cmd.shutdown_requested = True
            return 1

        monkeypatch.setattr(cmd, "poll_ray_job_completions", poll)
        monkeypatch.setattr(
            cmd,
            "claim_and_process_tasks",
            lambda *_args: pytest.fail("shutdown must precede a newly freed claim"),
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

    def test_reconciliation_timeout_and_cleanup_have_independent_deadlines(
        self, monkeypatch
    ) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=1.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 0.2
        cmd.timeout_check_interval = 0.3
        cmd.cancellation_interval = 10.0
        cmd.lease_cleanup_interval = 0.5
        reconciliations: list[float] = []
        timeout_checks: list[float] = []
        cleanups: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def claim(_queues, _concurrency):
            if clock.now >= 0.7:
                cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(
            cmd,
            "reconcile_tasks",
            lambda _queues: reconciliations.append(clock.now) or 0,
        )
        monkeypatch.setattr(
            cmd,
            "detect_stuck_tasks",
            lambda _queues: timeout_checks.append(clock.now) or 0,
        )
        monkeypatch.setattr(cmd, "process_cancellations", lambda _queues: 0)
        monkeypatch.setattr(
            cmd,
            "cleanup_expired_leases",
            lambda: cleanups.append(clock.now) or 0,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", clock.sleep)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert reconciliations == pytest.approx([0.0, 0.2, 0.4, 0.6])
        assert timeout_checks == pytest.approx([0.0, 0.3, 0.6])
        assert cleanups == pytest.approx([0.0, 0.5])

    def test_send_heartbeat_without_acquired_lease_fails_closed(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.lease = None
        recreated: list[bool] = []
        monkeypatch.setattr(cmd, "_recreate_lease", lambda: recreated.append(True))

        cmd.send_heartbeat()

        assert recreated == []
        assert cmd.shutdown_requested is True
        assert cmd.shutdown_exit_code == 1

    @pytest.mark.django_db
    def test_send_heartbeat_updates_owned_lease_and_reports_status(self) -> None:
        cmd = _make_command()
        cmd._create_lease("default")
        assert cmd.lease_identity is not None
        lease = TaskWorkerLease.objects.get(**cmd.lease_identity.database_filters())
        original_heartbeat = lease.last_heartbeat_at
        cmd._heartbeat_count = 3
        cmd.tasks_processed_count = 5
        cmd.last_task_processed = 0
        cmd.active_tasks = {1: "active"}
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=2))

        cmd.send_heartbeat()

        lease.refresh_from_db()
        assert lease.last_heartbeat_at >= original_heartbeat
        assert any("tasks_processed=5, active=3" in message for message in cmd.stdout.messages)

    def test_send_heartbeat_fails_closed_for_inactive_or_deleted_lease(
        self,
        monkeypatch,
    ) -> None:
        cmd = _make_command()
        _set_lease_identity(cmd)
        recreated: list[str] = []
        monkeypatch.setattr(
            TaskWorkerLease.objects,
            "filter",
            lambda **_filters: SimpleNamespace(update=lambda **_updates: 0),
        )
        monkeypatch.setattr(
            cmd,
            "_recreate_lease",
            lambda: recreated.append("recreated") or True,
        )
        cmd.send_heartbeat()
        cmd.send_heartbeat()

        assert recreated == []
        assert cmd.shutdown_requested is True
        assert cmd.shutdown_exit_code == 1

    def test_send_heartbeat_database_error_fails_closed(self, monkeypatch) -> None:
        cmd = _make_command()
        _set_lease_identity(cmd)

        def _broken(**_filters: object) -> None:
            raise RuntimeError("database offline")

        monkeypatch.setattr(TaskWorkerLease.objects, "filter", _broken)

        cmd.send_heartbeat()

        assert cmd.shutdown_requested is True
        assert any("Worker lease ownership lost" in message for message in cmd.stdout.messages)

    def test_check_ray_connection_reconnects_when_not_initialized(self, monkeypatch) -> None:
        cmd = _make_command()
        reconnect_calls: list[bool] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: False, cluster_resources=lambda: {"CPU": 1}
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: reconnect_calls.append(True))

        cmd._check_ray_connection()

        assert reconnect_calls == [True]

    def test_reconnect_ray_retries_until_failure(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        attempts: list[int] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: False,
            shutdown=lambda: None,
            cluster_resources=lambda: {"CPU": 1},
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd,
            "_init_local_ray",
            lambda: (_ for _ in ()).throw(RuntimeError("connect failed")),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep", lambda *_: None
        )

        original_write = cmd.stdout.write

        def tracked_write(message: str = "", ending: str = "\n"):
            if "Reconnection attempt" in message:
                attempts.append(1)
            original_write(message, ending=ending)

        cmd.stdout.write = tracked_write  # type: ignore[method-assign]
        cmd._reconnect_ray()

        assert len(attempts) == 5

    @pytest.mark.django_db
    def test_handle_local_mode_init_failure_continues_startup(
        self, monkeypatch, cohort_controller_case
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        _prepare_cohort_startup(
            cmd, monkeypatch, cohort_controller_case, error="private init failure"
        )
        waits = _run_cohort_startup_cycle(cmd, monkeypatch, cohort_controller_case)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=False,
            local=True,
            cluster=None,
        )

        assert cmd.execution_mode == "local"

        assert waits == [True, True]
        assert len(cohort_controller_case.ray_initializations) == 1
        assert cmd.ray_core_runner is None
        assert cmd._cohort_controller.reason == "connection_unavailable"
        assert any("connection_unavailable" in message for message in cmd.stdout.messages)
        assert all("private init failure" not in message for message in cmd.stdout.messages)

    @pytest.mark.django_db
    def test_handle_configures_adaptive_polling_from_settings(
        self, monkeypatch, cohort_controller_case
    ) -> None:
        cmd = _make_command()
        _prepare_cohort_startup(cmd, monkeypatch, cohort_controller_case)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "DEFAULT_CONCURRENCY": 2,
                "RUNNER": "ray_job",
                "WORKER_POLL_INTERVAL_SECONDS": 0.25,
                "WORKER_POLL_MAX_INTERVAL_SECONDS": 2.0,
            },
        )
        monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)
        monkeypatch.setattr(
            cmd,
            "_validate_execution_mode_configuration",
            lambda _settings: None,
        )

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=False,
            local=False,
            cluster=None,
            verbosity=1,
        )

        assert cmd.completion_poll_interval == 0.1
        assert cmd.polling_policy.base_interval_seconds == 0.25
        assert cmd.polling_policy.max_interval_seconds == 2.0
        assert any("0.25s base, 2s maximum" in message for message in cmd.stdout.messages)

        _assert_current_startup_lease(cmd)
        assert cmd._cohort_controller is not None
        assert not cohort_controller_case.threads

    @pytest.mark.django_db
    def test_handle_cluster_mode_init_failure_continues_startup(
        self, monkeypatch, cohort_controller_case
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        _prepare_cohort_startup(
            cmd, monkeypatch, cohort_controller_case, error="private init failure"
        )
        waits = _run_cohort_startup_cycle(cmd, monkeypatch, cohort_controller_case)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=False,
            local=False,
            cluster="ray://cluster:10001",
        )

        assert cmd.execution_mode == "cluster"

        assert waits == [True, True]
        assert len(cohort_controller_case.ray_initializations) == 1
        assert cmd.ray_core_runner is None
        assert cmd._cohort_controller.reason == "connection_unavailable"
        assert any("connection_unavailable" in message for message in cmd.stdout.messages)
        assert all("private init failure" not in message for message in cmd.stdout.messages)

    @pytest.mark.django_db
    def test_handle_keyboard_interrupt_path(self, monkeypatch, cohort_controller_case) -> None:
        cmd = _make_command()
        _prepare_cohort_startup(cmd, monkeypatch, cohort_controller_case)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        monkeypatch.setattr(
            cmd,
            "run_loop",
            lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=True,
            local=False,
            cluster=None,
        )

        assert any("Shutdown requested via keyboard interrupt" in m for m in cmd.stdout.messages)

        _assert_current_startup_lease(cmd)
        assert cmd._cohort_controller is not None
        assert not cohort_controller_case.threads

    @pytest.mark.parametrize("concurrency", [0, -1, True, 1001])
    def test_handle_rejects_invalid_cli_concurrency_before_ray_init(
        self, monkeypatch, concurrency
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        init_calls: list[bool] = []
        monkeypatch.setattr(cmd, "_init_local_ray", lambda: init_calls.append(True))

        with pytest.raises(CommandError, match="--concurrency"):
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=concurrency,
                sync=False,
                local=True,
                cluster=None,
            )

        assert init_calls == []


@pytest.mark.django_db
class TestWorkerCommandRuntimeDb:
    """DB-backed helper path tests."""

    def test_ray_job_storage_validation_precedes_lease_and_claim(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="rq2-startup-storage-required-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="rq2-invalid-storage-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "RUNNER": "ray_job",
                "RAY_ADDRESS": "ray://cluster:10001",
                "DEFAULT_CONCURRENCY": 1,
                "INPUT_STORAGE_BACKEND": None,
            },
        )
        monkeypatch.setattr(
            cmd,
            "_create_lease",
            lambda _queue: pytest.fail("invalid rq2 storage must precede lease creation"),
        )
        monkeypatch.setattr(
            cmd,
            "run_loop",
            lambda **_kwargs: pytest.fail("invalid rq2 storage must precede task claims"),
        )
        monkeypatch.setattr(
            cmd,
            "setup_signal_handlers",
            lambda: pytest.fail("invalid rq2 storage must fail during preflight"),
        )

        with pytest.raises(CommandError, match="request storage configuration is invalid"):
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=1,
                sync=False,
                local=False,
                cluster=None,
                verbosity=1,
            )

        task.refresh_from_db()
        assert not TaskWorkerLease.objects.filter(worker_id="rq2-invalid-storage-worker").exists()
        assert task.state == TaskState.QUEUED
        assert task.claimed_by_worker is None

    @pytest.mark.parametrize(
        ("args_json", "kwargs_json", "input_reference"),
        [
            ("manager-must-not-decode-args", "manager-must-not-decode-kwargs", None),
            (
                EXTERNAL_INPUT_PLACEHOLDER,
                EXTERNAL_INPUT_PLACEHOLDER,
                "resultfs://sha256/" + "a" * 64 + "?rel=aa/aa/" + "a" * 64 + ".json&bytes=4",
            ),
        ],
    )
    @pytest.mark.django_db(transaction=True)
    def test_ray_core_claim_submits_durable_input_without_manager_hydration(
        self,
        monkeypatch,
        current_worker_case,
        args_json: str,
        kwargs_json: str,
        input_reference: str | None,
    ) -> None:
        from django_ray.models import RayTaskCohortClaim
        from tests.integration.test_cohort_dispatch import _claimed

        case = current_worker_case
        RayTaskExecution.objects.filter(pk=case.task.pk).update(
            callable_path="testproject.tasks.add_numbers",
            args_json=args_json,
            kwargs_json=kwargs_json,
            input_reference=input_reference,
        )
        case.task.refresh_from_db()
        claimed = _claimed(case, "ray_core")
        cmd = _make_command(worker_id=case.owner.worker_id)
        cmd.execution_mode = "local"
        cmd.lease, cmd.lease_identity = case.lease, case.owner
        sdk = _cohort_worker_tests.core_sdk.__wrapped__(monkeypatch, cmd)
        cmd.ray_core_runner = sdk.runner

        def reject_manager_hydration(*_args, **_kwargs):
            pytest.fail("the manager must not hydrate Ray Core task input")

        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args", reject_manager_hydration
        )
        monkeypatch.setattr("django_ray.input_storage.load_task_input", reject_manager_hydration)
        cmd._dispatch_cohort_task(claimed)
        assert not sdk.calls
        assert cmd._cohort_preparation_tickets
        _cohort_worker_tests._advance_core(cmd)

        from django_ray.execution_codec import decode_execution_request

        ((serialized, _bindings),) = sdk.calls
        request = decode_execution_request(serialized)
        assert request.execution_protocol_version == 3
        assert request.identity == claimed.claim.facts.identity
        assert (request.serialized_args, request.serialized_kwargs, request.input_reference) == (
            args_json,
            kwargs_json,
            input_reference,
        )
        case.task.refresh_from_db()
        assert case.task.state == TaskState.RUNNING
        handle = cmd._cohort_core_handles[case.task.pk]
        assert (
            sdk.runner.get_pending_handle(
                case.task.pk,
                attempt_number=claimed.claim.facts.identity.attempt_number,
                execution_generation=claimed.claim.facts.identity.execution_generation,
            )
            is handle
        )
        assert RayTaskCohortClaim.objects.get().dispatched_at is not None

    @pytest.mark.django_db(transaction=True)
    def test_ray_core_request_encode_failure_is_permanent_before_execution(
        self, monkeypatch, historical_worker
    ) -> None:
        from functools import partial

        cmd = _make_command(worker_id="request-encode-worker")
        historical_worker.lease_for(cmd)
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            partial(cmd._handle_task_failure, supported_protocols=ExecutionProtocolRange(1, 1)),
        )
        task = historical_worker.task(
            task_id="request-encode-failure",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
            execution_generation=1,
        )
        application_calls: list[bool] = []

        class RejectingRunner:
            def submit_durable(self, **_kwargs: Any) -> SubmissionHandle:
                raise ExecutionRequestEncodeError

        def invoke_application(*_args: Any, **_kwargs: Any) -> str:
            application_calls.append(True)
            return '{"success":true,"result":3}'

        cmd.ray_core_runner = cast(Any, RejectingRunner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", invoke_application)

        cmd.submit_task_to_ray_core(task)

        task.refresh_from_db()
        assert application_calls == []
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.ray_job_id is None
        assert task.error_message == "Failed to submit to Ray Core: execution request is invalid"

    @pytest.mark.django_db(transaction=True)
    def test_historical_sync_execution_still_accepts_legacy_completion(
        self, monkeypatch, historical_worker
    ) -> None:
        cmd = _make_command(worker_id="legacy-sync-worker")
        historical_worker.lease_for(cmd)
        task = historical_worker.task(
            task_id="legacy-sync-completion",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
        )
        executor_calls: list[dict[str, Any]] = []

        def execute_legacy_completion(**kwargs: Any) -> str:
            executor_calls.append(kwargs)
            return '{"success":true,"result":3}'

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            execute_legacy_completion,
        )

        cmd.execute_task_sync(task)

        task.refresh_from_db()
        assert len(executor_calls) == 1
        assert executor_calls[0]["serialized_args"] == "[1, 2]"
        assert executor_calls[0]["serialized_kwargs"] == "{}"
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"

    @pytest.mark.django_db(transaction=True)
    def test_mark_stale_ray_core_tasks_routes_rows_through_retry_handling(
        self, historical_worker
    ) -> None:
        cmd = _make_command(worker_id="stale-worker")
        historical_worker.lease_for(cmd)
        task = historical_worker.task(
            task_id="stale-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
        )
        pending = {
            task.pk: RayCoreHandle(
                task_pk=task.pk,
                object_ref=object(),
                submitted_at=datetime.now(UTC),
                task_name="test",
                attempt_number=task.attempt_number,
                execution_generation=task.execution_generation,
            )
        }

        def retire_pending_handle(handle: RayCoreHandle) -> bool:
            if pending.get(handle.task_pk) is not handle:
                return False
            pending.pop(handle.task_pk)
            return True

        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks=pending,
                pending_count=1,
                pending_task_ids=tuple(pending),
                pending_task_handles=tuple(pending.values()),
                retire_pending_handle=retire_pending_handle,
            ),
        )

        cmd._mark_stale_ray_core_tasks_as_lost()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.finished_at is None
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_mark_stale_ray_core_tasks_does_not_fail_replacement_attempt(self) -> None:
        cmd = _make_command(worker_id="stale-worker")
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="stale-replacement-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
            attempt_number=2,
            execution_generation=7,
        )
        stale_handle = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="stale",
            attempt_number=1,
            execution_generation=7,
        )
        pending = {task.pk: stale_handle}

        def retire_pending_handle(handle: RayCoreHandle) -> bool:
            if pending.get(handle.task_pk) is not handle:
                return False
            pending.pop(handle.task_pk)
            return True

        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks=pending,
                pending_count=1,
                pending_task_handles=tuple(pending.values()),
                retire_pending_handle=retire_pending_handle,
            ),
        )

        cmd._mark_stale_ray_core_tasks_as_lost()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.error_message is None
        assert not TaskAttempt.objects.filter(execution=task, attempt_number=2).exists()
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_create_lease_is_idempotent_only_for_exact_owner(self) -> None:
        cmd = _make_command(worker_id="lease-worker")

        cmd._create_lease("high-priority")
        assert cmd.lease is not None
        first_pk = cmd.lease.pk
        cmd._create_lease("low-priority")

        assert cmd.lease is not None
        assert cmd.lease.pk == first_pk
        assert cmd.lease.queue_name == "low-priority"
        assert cmd.lease.is_active is True
        assert cmd.lease.capability_schema_version == WORKER_CAPABILITY_SCHEMA_VERSION
        assert cmd.lease.django_ray_version == django_ray.__version__
        assert (
            cmd.lease.min_supported_execution_protocol_version
            == MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION
        )
        assert (
            cmd.lease.max_supported_execution_protocol_version
            == MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION
        )
        assert cmd.lease.legacy_admission_token_id is None
        assert cmd.lease_queue_name == "low-priority"
        assert any("Lease restored" in message for message in cmd.stdout.messages)

    def test_create_lease_database_error_aborts_startup(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="lease-error-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.TaskWorkerLease.objects.create",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("database offline")),
        )

        with pytest.raises(CommandError, match="Could not create worker lease"):
            cmd._create_lease("default")

        assert cmd.lease is None
        assert cmd.lease_identity is None

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_clears_tracking_and_finalizes(
        self, monkeypatch, historical_worker
    ) -> None:
        cmd = _make_command(worker_id="cancel-worker")
        historical_worker.lease_for(cmd)
        task = historical_worker.task(
            task_id="cancel-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
        )

        cancel_calls: list[str] = []
        pending_handle = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="test",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks={task.pk: pending_handle},
                pending_task_ids=(task.pk,),
                get_pending_handle=lambda *_args, **_kwargs: pending_handle,
                cancel_pending_with_status=lambda _handle: (
                    cancel_calls.append("ray-cancel")
                    or CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
                ),
            ),
        )
        cmd.active_tasks = {task.pk: "raysubmit_1"}

        finalized: list[int] = []

        def fake_finalize(t: RayTaskExecution, **_kwargs: Any) -> bool:
            finalized.append(t.pk)
            t.state = TaskState.CANCELLED
            t.finished_at = datetime.now(UTC)
            t.save(update_fields=["state", "finished_at"])
            return True

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.finalize_cancellation",
            fake_finalize,
        )

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert finalized == [task.pk]
        assert cancel_calls == ["ray-cancel"]
        assert task.pk not in cmd.active_tasks

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_adopts_inactive_owner(
        self, historical_worker
    ) -> None:
        historical_worker.lease(
            worker_id="dead-cancel-worker",
            hostname="host",
            pid=123,
            is_active=False,
        )
        task = historical_worker.task(
            task_id="cancel-orphan-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker="dead-cancel-worker",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")
        historical_worker.lease_for(cmd)

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == "recovery-worker"
        assert task.cancellation_status == "NOT_APPLICABLE"

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_adopts_missing_owner(self, historical_worker) -> None:
        task = historical_worker.task(
            task_id="cancel-orphan-002",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker=None,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")
        historical_worker.lease_for(cmd)

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == "recovery-worker"

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_adopts_expired_lease_owner(
        self, historical_worker
    ) -> None:
        historical_worker.lease(
            worker_id="expired-cancel-worker",
            hostname="host",
            pid=123,
            is_active=True,
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
        )
        task = historical_worker.task(
            task_id="cancel-orphan-003",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker="expired-cancel-worker",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")
        historical_worker.lease_for(cmd)

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == "recovery-worker"

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_skips_active_owner(self, historical_worker) -> None:
        historical_worker.lease(
            worker_id="active-cancel-worker",
            hostname="host",
            pid=123,
            is_active=True,
        )
        task = historical_worker.task(
            task_id="cancel-active-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker="active-cancel-worker",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")
        historical_worker.lease_for(cmd)

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert task.claimed_by_worker == "active-cancel-worker"

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_uses_ray_job_cancellation(
        self, monkeypatch, historical_worker
    ) -> None:
        task = historical_worker.task(
            task_id="cancel-ray-job-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            ray_job_id="raysubmit_cancel_orphan_001",
            ray_address="ray://cluster:10001",
            args_json="[]",
            kwargs_json="{}",
        )
        calls: list[str] = []

        class FakeRunner:
            def cancel_with_status(self, handle):
                calls.append(handle.ray_job_id)
                from django_ray.runner.cancellation import (
                    CancellationOutcome,
                    CancellationOutcomeStatus,
                )

                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd = _make_command(worker_id="recovery-worker")
        historical_worker.lease_for(cmd)

        cmd.process_cancellations()

        task.refresh_from_db()
        assert calls == ["raysubmit_cancel_orphan_001"]
        assert task.state == TaskState.CANCELLED
        assert task.cancellation_status == "REQUESTED"

    @pytest.mark.django_db(transaction=True)
    def test_historical_process_cancellations_records_indeterminate_without_ray_core_runner(
        self, historical_worker
    ) -> None:
        task = historical_worker.task(
            task_id="cancel-ray-core-unavailable-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            ray_job_id="ray_core:123",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")
        historical_worker.lease_for(cmd)

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.cancellation_status == "INDETERMINATE"
        assert (
            task.cancellation_error
            == "Exact Ray Core handle unavailable while recovering cancellation"
        )

    def test_shutdown_hands_off_active_ray_job(self) -> None:
        cmd = _make_command(worker_id="handoff-worker")
        cmd.execution_mode = "ray"
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="handoff-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_handoff",
            ray_address="ray://cluster:10001",
        )
        cmd.active_tasks = {task.pk: "raysubmit_handoff"}
        cmd.active_task_identities = {task.pk: (task.attempt_number, task.execution_generation)}

        cmd._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker is None
        assert cmd.active_tasks == {}

    def test_shutdown_handoff_does_not_release_replacement_with_same_ray_job_id(
        self,
    ) -> None:
        cmd = _make_command(worker_id="handoff-worker")
        cmd.execution_mode = "ray"
        task = RayTaskExecution.objects.create(
            task_id="handoff-replacement-aba-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            attempt_number=2,
            execution_generation=5,
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_handoff_reused",
            ray_address="ray://cluster:10001",
        )
        cmd.active_tasks = {task.pk: "raysubmit_handoff_reused"}
        cmd.active_task_identities = {task.pk: (1, 5)}

        cmd._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == cmd.worker_id
        assert task.last_heartbeat_at is None
        assert cmd.active_tasks == {}
        assert cmd.active_task_identities == {}

    def test_shutdown_during_submission_hands_off_claimed_task(self) -> None:
        cmd = _make_command(worker_id="submission-shutdown-worker")
        cmd.execution_mode = "ray"
        task = RayTaskExecution.objects.create(
            task_id="submission-shutdown-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        processed: list[int] = []
        cmd.shutdown_requested = True
        cmd.process_task = lambda claimed: processed.append(claimed.pk)  # type: ignore[method-assign]

        cmd.claim_and_process_tasks(["default"], concurrency=1)

        task.refresh_from_db()
        assert processed == []
        assert task.state == TaskState.QUEUED
        assert task.claimed_by_worker is None

    def test_claim_boundary_rejects_programmatic_ray_job_only_bypass(self) -> None:
        cmd = _make_command(worker_id="wrong-runner-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("ray-data")
        task = RayTaskExecution.objects.create(
            task_id="ray-job-only-claim-001",
            callable_path="testproject.apps.cluster_tasks.tasks.ray_data_batch_score",
            queue_name="ray-data",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )

        with pytest.raises(CommandError, match="cannot claim Ray Job-only"):
            cmd.claim_and_process_tasks(["ray-data"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.claimed_by_worker is None
        assert task.execution_generation == 0

    def test_sync_active_task_is_allowed_to_finish_after_signal(self) -> None:
        cmd = _make_command(worker_id="sync-shutdown-worker")
        cmd.execution_mode = "sync"
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="sync-shutdown-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
        )
        finished: list[int] = []
        cmd.handle_shutdown_signal(signal.SIGTERM, None)
        cmd.execute_task_sync = lambda active: finished.append(active.pk)  # type: ignore[method-assign]

        cmd.process_task(task)

        assert finished == [task.pk]

    @pytest.mark.django_db(transaction=True)
    def test_sync_batch_stops_starting_new_tasks_after_signal(
        self, monkeypatch, current_worker_case
    ) -> None:
        from django_ray.models import RayTaskCohortClaim
        from django_ray.target.cohort_claim import CohortRunnerFamily
        from tests.integration.test_cohort_selection import _clone
        from tests.integration.test_cohort_worker import _claim_task

        case = current_worker_case
        first = case.task
        second = _clone(case, name="sync-batch-second")
        cmd = _make_command(worker_id=case.owner.worker_id)
        cmd.execution_mode = "sync"
        cmd.lease, cmd.lease_identity = case.lease, case.owner
        limits = []

        def claim(*, limit):
            limits.append(limit)
            assert limit == 1 and not cmd.shutdown_requested
            return (_claim_task(case),)

        cmd._cohort_controller = SimpleNamespace(family=CohortRunnerFamily.SYNC, claim=claim)
        apply_result = cmd._apply_cohort_result

        def finish_first_then_signal(*args, **kwargs):
            outcome = apply_result(*args, **kwargs)
            assert outcome == 1
            cmd.handle_shutdown_signal(signal.SIGTERM, None)
            return outcome

        monkeypatch.setattr(cmd, "_apply_cohort_result", finish_first_then_signal)
        assert cmd._claim_and_process_cohort_tasks(concurrency=2) == 1

        first.refresh_from_db()
        second.refresh_from_db()
        assert limits == [1] and cmd.shutdown_requested
        assert first.state == TaskState.SUCCEEDED
        assert first.result_data == "3"
        assert RayTaskCohortClaim.objects.get(binding_id=first.pk).disposition == "RESOLVED"
        assert second.state == TaskState.QUEUED and second.execution_generation == 0
        assert second.claimed_by_worker is None and second.started_at is None
        assert second.last_heartbeat_at is None

    def test_shutdown_cancels_and_persists_active_ray_core(self) -> None:
        cmd = _make_command(worker_id="core-shutdown-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="core-shutdown-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
        )
        cancel_calls: list[str] = []
        pending_handle = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="test",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks={task.pk: pending_handle},
                pending_task_ids=(task.pk,),
                pending_task_handles=(pending_handle,),
                cancel_pending_with_status=lambda _handle: (
                    cancel_calls.append("cancel")
                    or CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
                ),
            ),
        )

        cmd._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert cancel_calls == ["cancel"]
        assert task.state == TaskState.CANCELLING
        assert task.cancellation_status == "REQUESTED"

    def test_cleanup_expired_leases_logs_and_handles_errors(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="cleanup-worker")
        warnings: list[tuple[str, bool]] = []
        cmd.logger = SimpleNamespace(
            warning=lambda message, *, exc_info=False: warnings.append((str(message), exc_info))
        )

        monkeypatch.setattr("django_ray.runner.leasing.cleanup_expired_leases", lambda: 2)
        cmd.cleanup_expired_leases()
        assert any("Cleaned up 2 expired worker lease(s)" in m for m in cmd.stdout.messages)

        monkeypatch.setattr(
            "django_ray.runner.leasing.cleanup_expired_leases",
            lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        )
        cmd.cleanup_expired_leases()
        assert warnings == [("Failed to cleanup expired leases", True)]

    def test_shutdown_releases_lease_and_handles_ray_disconnect(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="shutdown-worker")
        cmd.lease = cast(Any, SimpleNamespace())
        identity = _set_lease_identity(cmd)
        cmd.execution_mode = "local"

        released: list[str] = []
        monkeypatch.setattr(
            "django_ray.runner.leasing.release_lease",
            lambda owner: released.append(owner.worker_id) or True,
        )

        fake_ray = SimpleNamespace(
            is_initialized=lambda: True,
            shutdown=lambda: released.append("ray-shutdown"),
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        cmd.shutdown()

        assert identity.worker_id == "shutdown-worker"
        assert released == ["shutdown-worker", "ray-shutdown"]
