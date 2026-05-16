"""Unit tests for worker mode selection behavior."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from django_ray.management.commands.django_ray_worker import Command


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
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            lambda: SimpleNamespace(pending_count=0),
        )
        monkeypatch.setattr(cmd, "_init_local_ray", lambda: None)
        monkeypatch.setattr(cmd, "_create_lease", lambda queue: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **kwargs: None)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

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
        monkeypatch.setattr(cmd, "_create_lease", lambda queue: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **kwargs: None)
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

        assert cmd.execution_mode == "sync"
