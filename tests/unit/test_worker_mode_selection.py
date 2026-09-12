"""Unit tests for worker mode selection behavior."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import TaskExecutionProtocolPolicy, TaskWorkerLease


def _assert_current_startup_lease(command: Command) -> None:
    assert command.lease_identity is not None
    lease = TaskWorkerLease.objects.get(**command.lease_identity.database_filters())
    assert (
        lease.capability_schema_version,
        lease.min_supported_execution_protocol_version,
        lease.max_supported_execution_protocol_version,
    ) == (1, 3, 3)
    assert lease.is_active and lease.legacy_admission_token_id is None
    policy = TaskExecutionProtocolPolicy.objects.get()
    assert policy.active_write_protocol_version == 3 and not policy.legacy_worker_admission_enabled


def _stub_owned_controller(command: Command, monkeypatch):
    calls = []

    def prepare(identity, **arguments):
        _assert_current_startup_lease(command)
        assert identity is command.lease_identity
        calls.append(arguments)
        return SimpleNamespace()

    monkeypatch.setattr("django_ray.runner.cohort_worker.CohortWorkerController", prepare)
    monkeypatch.setattr(command, "_init_local_ray", lambda: pytest.fail("Legacy Ray startup"))
    monkeypatch.setattr(command, "_initialize_ray_execution", lambda: pytest.fail("Legacy startup"))
    monkeypatch.setattr(
        command, "run_loop", lambda **kwargs: _assert_current_startup_lease(command)
    )
    monkeypatch.setattr(command, "shutdown", lambda: None)
    monkeypatch.setattr(command, "setup_signal_handlers", lambda: None)
    return calls


class TestWorkerModeSelection:
    """Tests for RUNNER-based default mode and CLI precedence."""

    def test_get_default_execution_mode_ray_job(self) -> None:
        cmd = Command()
        mode, cluster_address = cmd._get_default_execution_mode(
            {
                "RUNNER": "ray_job",
                "RAY_ADDRESS": "ray://cluster:10001",
            }
        )
        assert mode == "ray"
        assert cluster_address is None

    def test_get_default_execution_mode_ray_core_auto(self) -> None:
        cmd = Command()
        mode, cluster_address = cmd._get_default_execution_mode(
            {
                "RUNNER": "ray_core",
                "RAY_ADDRESS": "auto",
            }
        )
        assert mode == "local"
        assert cluster_address is None

    def test_get_default_execution_mode_ray_core_cluster(self) -> None:
        cmd = Command()
        mode, cluster_address = cmd._get_default_execution_mode(
            {
                "RUNNER": "ray_core",
                "RAY_ADDRESS": "ray://cluster:10001",
            }
        )
        assert mode == "cluster"
        assert cluster_address == "ray://cluster:10001"

    @pytest.mark.django_db
    def test_handle_uses_runner_setting_when_no_cli_mode_flags(self, monkeypatch) -> None:
        cmd = Command()
        cmd.stdout = StringIO()

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "RUNNER": "ray_core",
                "RAY_ADDRESS": "auto",
                "DEFAULT_CONCURRENCY": 1,
            },
        )
        calls = _stub_owned_controller(cmd, monkeypatch)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=False,
            local=False,
            cluster=None,
        )

        assert cmd.execution_mode == "local"
        assert len(calls) == 1 and calls[0]["execution_mode"] == "local"
        assert callable(calls[0]["connect"])

    @pytest.mark.django_db
    def test_handle_cli_sync_overrides_runner_setting(self, monkeypatch) -> None:
        cmd = Command()
        cmd.stdout = StringIO()

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "RUNNER": "ray_core",
                "RAY_ADDRESS": "auto",
                "DEFAULT_CONCURRENCY": 1,
            },
        )
        calls = _stub_owned_controller(cmd, monkeypatch)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=True,
            local=False,
            cluster=None,
        )

        assert cmd.execution_mode == "sync"
        assert len(calls) == 1 and calls[0]["execution_mode"] == "sync"
        assert calls[0]["connect"] is None
