"""Unit tests for Ray address URI validation and trusted cluster allowlisting."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray import observability
from django_ray.runner import ray_job
from django_ray.runner.ray_job import (
    RayJobRunner,
    UntrustedRayAddressError,
    _resolve_submission_address,
    clear_registered_trusted_ray_addresses,
    get_trusted_ray_addresses,
    register_trusted_ray_address,
    trusted_ray_address_override,
    validate_trusted_ray_address,
)


@pytest.fixture(autouse=True)
def _clean_runtime_trusted_addresses():
    clear_registered_trusted_ray_addresses()
    yield
    clear_registered_trusted_ray_addresses()


class TestRayAddressUriValidation:
    """Strict URI syntax and scheme validation before contacting Ray endpoints."""

    @pytest.mark.parametrize(
        "bad_address",
        [
            "",
            "   ",
            "http://user:pass@ray-head:8265",
            "https://token@ray-head:8265",
            "file:///etc/passwd",
            "ftp://ray-head:8265",
            "custom_module://ray-head:8265",
            "http://ray-head:8265?query=1",
            "http://ray-head:8265#frag",
            "http://ray-head:8265/\r\nX-Injected: 1",
            "http://ray-head:8265/foo/../bar",
            "ray://ray-head:10001/extra/path",
            "http://ray-head:notaport",
            "ray-head-without-port",
            "http://ray-head:8265 trailing",
        ],
    )
    def test_rejects_malformed_or_disallowed_uris(self, bad_address: str) -> None:
        with pytest.raises(UntrustedRayAddressError):
            _resolve_submission_address(bad_address)

    @pytest.mark.parametrize(
        "valid_address",
        [
            "http://ray-head:8265",
            "https://ray-head.internal:8265",
            "http://127.0.0.1:8765/ray",
        ],
    )
    def test_accepts_clean_http_and_https_uris(self, valid_address: str) -> None:
        assert _resolve_submission_address(valid_address) == valid_address


class TestTrustedRayAddressAllowlist:
    """Enforce configured and runtime-registered Ray cluster allowlists."""

    def test_allows_addresses_from_settings_tasks_and_allowlist(self, settings) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://primary-head:8265",
            "RAY_STATE_API_ADDRESS": "https://state-head:8265",
            "ALLOWED_RAY_ADDRESSES": ["http://extra-head:8265/"],
        }
        settings.TASKS = {
            "default": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "OPTIONS": {"RAY_ADDRESS": "ray://alias-head:10001"},
            }
        }

        trusted = get_trusted_ray_addresses()
        assert "http://primary-head:8265" in trusted
        assert "https://state-head:8265" in trusted
        assert "http://extra-head:8265" in trusted
        assert "ray://alias-head:10001" in trusted

        assert validate_trusted_ray_address("http://extra-head:8265") == "http://extra-head:8265"
        assert validate_trusted_ray_address("ray://alias-head:10001") == "ray://alias-head:10001"

    def test_rejects_untrusted_address_and_names_allowlist_setting(self, settings) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-head:8265"}
        settings.TASKS = {}

        with pytest.raises(UntrustedRayAddressError, match="ALLOWED_RAY_ADDRESSES"):
            validate_trusted_ray_address("http://untrusted-host:8265")

    def test_supports_runtime_cluster_override_registration(self, settings) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-head:8265"}
        settings.TASKS = {}

        with pytest.raises(UntrustedRayAddressError):
            validate_trusted_ray_address("http://cli-cluster:8265")

        with trusted_ray_address_override("http://cli-cluster:8265"):
            assert validate_trusted_ray_address("http://cli-cluster:8265") == (
                "http://cli-cluster:8265"
            )

        with pytest.raises(UntrustedRayAddressError):
            validate_trusted_ray_address("http://cli-cluster:8265")

        register_trusted_ray_address("http://persistent-cli:8265")
        assert validate_trusted_ray_address("http://persistent-cli:8265") == (
            "http://persistent-cli:8265"
        )

    def test_runner_get_client_blocks_untrusted_persisted_address_before_http(
        self,
        monkeypatch,
        settings,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-head:8265"}
        settings.TASKS = {}

        called = False

        def fail_resolve(_address: str) -> str:
            nonlocal called
            called = True
            raise AssertionError("_resolve_submission_address must not be called")

        monkeypatch.setattr(ray_job, "_resolve_submission_address", fail_resolve)

        runner = RayJobRunner()
        with pytest.raises(UntrustedRayAddressError, match="ALLOWED_RAY_ADDRESSES"):
            runner._get_client("http://untrusted-target:8265")
        assert called is False

    def test_runner_accepts_explicit_cluster_address_override(self, monkeypatch, settings) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-head:8265"}
        settings.TASKS = {}

        captured: list[str] = []

        def fake_pinned(address: str, **_kwargs):
            captured.append(address)
            return SimpleNamespace(address=address)

        monkeypatch.setattr(ray_job, "_address_pinned_job_client", fake_pinned)

        runner = RayJobRunner(cluster_address="http://override-head:8265")
        assert runner.ray_address == "http://override-head:8265"
        runner._get_client()
        assert captured == ["http://override-head:8265"]

    def test_observability_blocks_untrusted_state_and_log_addresses(
        self,
        monkeypatch,
        settings,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://trusted-head:8265",
            "RAY_STATE_API_ADDRESS": "http://trusted-head:8265",
        }
        settings.TASKS = {}

        monkeypatch.setitem(
            sys.modules,
            "ray.util.state",
            SimpleNamespace(
                get_task=lambda **_kwargs: pytest.fail("get_task must not be called"),
                get_log=lambda **_kwargs: pytest.fail("get_log must not be called"),
            ),
        )

        with pytest.raises(UntrustedRayAddressError, match="ALLOWED_RAY_ADDRESSES"):
            observability._address_pinned_state_client("http://untrusted-state:8265")

        with pytest.raises(observability.WorkflowObservabilityError):
            observability.get_ray_task_state(
                "ray-task-1",
                address="http://untrusted-state:8265",
            )

        with pytest.raises(observability.WorkflowObservabilityError):
            observability.get_ray_task_logs(
                "ray-task-1",
                address="http://untrusted-state:8265",
            )

    def test_rejects_malformed_allowlist_configuration(self, settings) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://trusted-head:8265",
            "ALLOWED_RAY_ADDRESSES": "http://not-a-list:8265",
        }
        with pytest.raises(ImproperlyConfigured, match="ALLOWED_RAY_ADDRESSES"):
            get_trusted_ray_addresses()


class TestPlaintextHttpRuntimeEnvWarningAndObservabilityHardening:
    """Tests for non-loopback HTTP env_vars warning and observability fixes."""

    def test_warns_only_for_non_loopback_http_with_env_vars_without_leaking_secrets(
        self, caplog
    ) -> None:
        secret_val = "super-secret-env-token-999"
        with caplog.at_level("WARNING", logger="django_ray.runner.ray_job"):
            ray_job._warn_if_plaintext_http_runtime_env_vars(
                "http://ray-head.internal:8265",
                {"env_vars": {"API_KEY": secret_val}},
            )
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert caplog.records[0].args == ("http://ray-head.internal:8265",)
        assert "at rest" in msg
        assert "API_KEY" not in msg
        assert secret_val not in msg

        caplog.clear()
        with caplog.at_level("WARNING", logger="django_ray.runner.ray_job"):
            ray_job._warn_if_plaintext_http_runtime_env_vars(
                "http://127.0.0.1:8265",
                {"env_vars": {"API_KEY": secret_val}},
            )
            ray_job._warn_if_plaintext_http_runtime_env_vars(
                "https://ray-head.internal:8265",
                {"env_vars": {"API_KEY": secret_val}},
            )
            ray_job._warn_if_plaintext_http_runtime_env_vars(
                "http://ray-head.internal:8265",
                {"env_vars": {}},
            )
        assert caplog.records == []

    def test_bounded_log_text_closes_streaming_generator_on_early_stop(self) -> None:
        closed = False

        def _stream():
            nonlocal closed
            try:
                yield "first-chunk"
                yield "second-chunk-never-consumed"
            finally:
                closed = True

        text, truncated = observability._bounded_log_text(_stream(), max_bytes=4)
        assert closed is True
        assert truncated is True
        assert text == "[TRUNCATED]"[:4]

    def test_log_stream_pins_endpoint_against_ambient_ray_env(self, monkeypatch, settings) -> None:
        from ray.dashboard.modules import dashboard_sdk
        from ray.util.state import api as state_api

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://selected-head:8265",
            "RAY_STATE_API_ADDRESS": "http://selected-head:8265",
        }
        settings.TASKS = {}
        monkeypatch.setenv("RAY_ADDRESS", "http://ambient-hijack:8265")
        monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://ambient-hijack:8265")

        requested_urls: list[str] = []

        class FakeResp:
            status_code = 200
            text = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_content(self, chunk_size=None):
                del chunk_size
                yield b"pinned-log-line\n"

        def fake_get(url, **kwargs):
            requested_urls.append(url)
            assert kwargs["stream"] is True
            assert kwargs["allow_redirects"] is False
            return FakeResp()

        monkeypatch.setattr(state_api, "requests", SimpleNamespace(get=fake_get))
        monkeypatch.setattr(dashboard_sdk, "get_auth_headers_if_auth_enabled", lambda _h: {})

        logs = observability.get_ray_task_logs("ray-task-99", tail=10)
        assert logs == {"out": "pinned-log-line\n", "err": "pinned-log-line\n"}
        assert len(requested_urls) == 2
        assert all(
            url.startswith("http://selected-head:8265/api/v0/logs/file?") for url in requested_urls
        )

    @pytest.mark.django_db
    def test_queue_depths_include_cancelling_and_attempt_history_marks_terminal_current(
        self,
    ) -> None:
        from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

        RayTaskExecution.objects.create(
            task_id="cancelling-only-1",
            callable_path="testproject.tasks.echo_task",
            queue_name="cancelling-queue",
            state=TaskState.CANCELLING,
        )
        depths = observability.get_queue_depths()
        matching = [q for q in depths["queues"] if q["queue_name"] == "cancelling-queue"]
        assert len(matching) == 1
        assert matching[0]["running"] == 1

        terminal = RayTaskExecution.objects.create(
            task_id="terminal-history-1",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.SUCCEEDED,
            attempt_number=2,
        )
        TaskAttempt.objects.create(
            execution=terminal,
            attempt_number=1,
            state=TaskState.FAILED,
        )
        TaskAttempt.objects.create(
            execution=terminal,
            attempt_number=2,
            state=TaskState.SUCCEEDED,
        )
        history = observability.get_attempt_history(terminal)
        assert [(a["attempt_number"], a["current"]) for a in history["attempts"]] == [
            (1, False),
            (2, True),
        ]

    @pytest.mark.django_db
    def test_workflow_node_snapshot_reads_progress_once_and_supports_v3(self, monkeypatch) -> None:
        from django_ray.models import RayTaskExecution, TaskState
        from django_ray.workflow.progress import reads as progress_reads

        execution = RayTaskExecution.objects.create(
            task_id="v3-obs-node-1",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        calls = 0

        def fake_progress(_exec):
            nonlocal calls
            calls += 1
            return {
                "schema_version": observability.WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
                "revision": 5,
            }

        monkeypatch.setattr(observability, "get_workflow_progress", fake_progress)
        monkeypatch.setattr(
            progress_reads,
            "get_workflow_node_detail",
            lambda _exec, node_id, **_kwargs: {
                "node": {
                    "node_id": node_id,
                    "state": "SUCCEEDED",
                    "ray_task_id": None,
                }
            },
        )

        snapshot = observability.get_workflow_node_snapshot(execution, "node-v3-0")
        assert snapshot is not None
        assert snapshot["node"]["node_id"] == "node-v3-0"
        assert calls == 1
