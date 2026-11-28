"""Focused Wave 4 regression tests for callable allowlists and remote worker boundaries."""

from __future__ import annotations

import ast
import contextvars
import json
import pickle
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from django.tasks import task
from django.tasks.base import Task

from django_ray import observability
from django_ray.decorators import ray_task
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    NestedExecutionRequestRejected,
    NestedExecutionRequestRejection,
    encode_execution_request,
)
from django_ray.ray_job_protocol import build_ray_job_request_metadata
from django_ray.runner import ray_job
from django_ray.runner.ray_job import (
    RayJobRunner,
    UntrustedRayAddressError,
    _address_pinned_job_client,
)
from django_ray.runtime import distributed, import_utils
from django_ray.runtime.import_utils import (
    get_driver_allowed_callable_modules,
    resolve_task_callable,
    resolve_workflow_callable,
)
from django_ray.runtime.remote import execute_workflow_step_remote
from django_ray.runtime.runtime_env import normalize_runtime_env
from tests.unit._remote_helpers import (
    _execute_strict_request,
    _strict_execution_request,
)

ROOT = Path(__file__).resolve().parents[2]


@task
def _w4_sample_task(value: int) -> int:
    return value + 10


@ray_task(
    num_cpus=2.5,
    num_gpus=0.5,
    memory=268_435_456,
    runtime_env_profile="gpu-profile",
)
def _w4_contract_ray_task(value: int) -> int:
    return value * 3


@ray_task(num_cpus=4)
def _w4_cpu_only_ray_task(value: int) -> int:
    return value + 1


def _w4_workflow_step(value: int, *, offset: int = 1) -> int:
    return value + offset


def _w4_workflow_preview(value: int) -> dict[str, int]:
    return {"preview": value}


def _restore_production_default_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo conftest's tests.* convenience fixture to exercise real production defaults."""
    closure = getattr(import_utils._default_workflow_allowed_modules, "__closure__", None)
    if closure:
        original = closure[0].cell_contents
        if callable(original):
            monkeypatch.setattr(import_utils, "_default_workflow_allowed_modules", original)


class TestPreImportTaskAllowlistAndDriverPropagation:
    """Verify pre-import task allowlist defaults and non-Django Ray worker propagation."""

    def test_resolve_task_callable_blocks_unlisted_modules_before_import_when_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        _restore_production_default_allowlist(monkeypatch)
        settings.DJANGO_RAY = {}
        imported: list[str] = []

        def fail_import(name: str) -> Any:
            imported.append(name)
            raise AssertionError(f"import_module must not run for {name}")

        monkeypatch.setattr(import_utils, "import_module", fail_import)

        for blocked in (
            "json.dumps",
            "os.system",
            "django.utils.html.escape",
            "django_ray.runtime.import_utils.import_callable",
        ):
            with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
                resolve_task_callable(blocked)
        assert imported == []

    def test_resolve_task_callable_fails_closed_without_django_settings_unless_explicit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _restore_production_default_allowlist(monkeypatch)
        unconfigured = SimpleNamespace(configured=False)
        monkeypatch.setattr("django.conf.settings", unconfigured)

        imported: list[str] = []

        def spy_import(name: str) -> Any:
            imported.append(name)
            return __import__(name, fromlist=["*"])

        monkeypatch.setattr(import_utils, "import_module", spy_import)

        assert get_driver_allowed_callable_modules() == ()

        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_task_callable(f"{__name__}._w4_sample_task")
        assert imported == []

        resolved = resolve_task_callable(
            f"{__name__}._w4_sample_task",
            allowed_modules=(__name__,),
        )
        assert isinstance(resolved, Task)
        assert resolved is _w4_sample_task

    def test_get_driver_allowed_callable_modules_prefers_explicit_setting(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        _restore_production_default_allowlist(monkeypatch)
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": ["myapp.tasks", "other.pkg"]}
        assert get_driver_allowed_callable_modules() == ("myapp.tasks", "other.pkg")

        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": []}
        assert get_driver_allowed_callable_modules() == ()

        settings.DJANGO_RAY = {}
        defaults = get_driver_allowed_callable_modules()
        assert "testproject" in defaults
        assert "django" not in defaults
        assert "django_ray" not in defaults

    def test_non_django_workflow_step_succeeds_with_driver_propagated_allowlist(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        _restore_production_default_allowlist(monkeypatch)
        # 1. Driver computes its allowlist while Django settings are configured.
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        driver_allowlist = get_driver_allowed_callable_modules()
        assert driver_allowlist == (__name__,)

        # 2. Simulate a separate Ray worker process with bootstrap_django=False
        # where django.conf.settings.configured is False.
        unconfigured = SimpleNamespace(configured=False)
        monkeypatch.setattr("django.conf.settings", unconfigured)

        # Without the propagated allowlist, non-Django execution fails closed.
        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            execute_workflow_step_remote(
                f"{__name__}._w4_workflow_step",
                False,
                (),
                {"offset": 5},
                {},
                None,
                None,
                "0.0",
                7,
            )

        # With the driver's allowlist, non-Django execution succeeds cleanly.
        result = execute_workflow_step_remote(
            f"{__name__}._w4_workflow_step",
            False,
            (),
            {"offset": 5},
            {},
            None,
            None,
            "0.0",
            7,
            output_preview_path=f"{__name__}._w4_workflow_preview",
            allowed_callable_modules=driver_allowlist,
        )
        assert result == 12

        # Disallowed modules are still rejected pre-import even with an explicit allowlist.
        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_workflow_callable("json.dumps", allowed_modules=driver_allowlist)


class TestRemoteTaskStructuredLogging:
    """Ensure remote.py uses structured logging without bare print() calls."""

    def test_remote_module_contains_no_bare_print_calls(self) -> None:
        remote_path = ROOT / "src" / "django_ray" / "runtime" / "remote.py"
        tree = ast.parse(remote_path.read_text(encoding="utf-8"), filename=str(remote_path))
        print_lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        assert print_lines == [], f"Found bare print() calls in remote.py at lines {print_lines}"

    def test_execute_django_task_remote_emits_structured_log_records(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        payload = json.dumps({"success": True, "result": "ok"})
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda *_args, **_kwargs: payload,
        )
        with caplog.at_level("INFO", logger="django_ray.runtime.remote"):
            _execute_strict_request(encode_execution_request(_strict_execution_request()))

        messages = [record.getMessage() for record in caplog.records]
        assert any("[Task 217] Starting: tests.strict_task" in msg for msg in messages)
        assert any("[Task 217] SUCCESS" in msg for msg in messages)


class TestObservabilityRedirectAndJobAllowlistDefaults:
    """Close R2-020: disallow HTTP redirects on log streaming and default allowlist on."""

    @pytest.mark.parametrize("redirect_code", [301, 302, 307, 308])
    def test_log_stream_disables_redirects_and_rejects_3xx_without_leaking_location(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
        redirect_code: int,
    ) -> None:
        from ray.dashboard.modules import dashboard_sdk
        from ray.util.state import api as state_api

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://selected-head:8265",
            "RAY_STATE_API_ADDRESS": "http://selected-head:8265",
        }
        settings.TASKS = {}

        seen_kwargs: list[dict[str, Any]] = []

        class RedirectResponse:
            status_code = redirect_code
            text = "redirected to internal metadata"
            headers = {"Location": "http://169.254.169.254/latest/meta-data/secret-token"}

            def __enter__(self) -> RedirectResponse:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def iter_content(self, chunk_size: int | None = None) -> Any:
                del chunk_size
                raise AssertionError("redirect response body must never be streamed")

        def fake_get(_url: str, **kwargs: Any) -> RedirectResponse:
            seen_kwargs.append(kwargs)
            return RedirectResponse()

        monkeypatch.setattr(state_api, "requests", SimpleNamespace(get=fake_get))
        monkeypatch.setattr(
            dashboard_sdk,
            "get_auth_headers_if_auth_enabled",
            lambda _h: {"Authorization": "Bearer ray-secret"},
        )

        with pytest.raises(observability.WorkflowObservabilityError) as caught:
            observability.get_ray_task_logs("ray-task-redirect", tail=5)

        assert seen_kwargs
        assert seen_kwargs[0]["allow_redirects"] is False
        assert "169.254.169.254" not in str(caught.value)
        assert "secret-token" not in str(caught.value)

    def test_address_pinned_job_client_enforces_allowlist_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-head:8265"}
        settings.TASKS = {}

        called = False

        def fail_resolve(_address: str) -> str:
            nonlocal called
            called = True
            raise AssertionError("_resolve_submission_address must not run for untrusted host")

        monkeypatch.setattr(ray_job, "_resolve_submission_address", fail_resolve)

        with pytest.raises(UntrustedRayAddressError, match="ALLOWED_RAY_ADDRESSES"):
            _address_pinned_job_client("http://untrusted-head:8265")
        assert called is False


class TestRemoteWorkerStrictDistributedEnforcement:
    """Close R2-021: enforce strict nested requests on remote Ray workers before unpickling."""

    @pytest.mark.parametrize(
        "mode",
        [
            "require_env",
            "disallow_legacy_env",
            "django_setting",
            "strict_ray_job_env",
            "malformed_ray_job_env",
        ],
    )
    def test_remote_leaf_rejects_none_nested_request_in_clean_worker_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
        mode: str,
    ) -> None:
        pickled = pickle.dumps(_w4_workflow_step)
        events: list[str] = []
        monkeypatch.setattr(
            distributed,
            "_bootstrap_django_if_needed",
            lambda: events.append("bootstrap"),
        )
        monkeypatch.setattr(
            pickle,
            "loads",
            lambda _raw: events.append("pickle.loads"),
        )

        if mode == "require_env":
            monkeypatch.setenv("DJANGO_RAY_REQUIRE_STRICT_NESTED_REQUEST", "1")
        elif mode == "disallow_legacy_env":
            monkeypatch.setenv("DJANGO_RAY_ALLOW_LEGACY_NESTED_CALLABLES", "0")
        elif mode == "django_setting":
            settings.DJANGO_RAY = {"REQUIRE_STRICT_NESTED_REQUEST": True}
        elif mode == "strict_ray_job_env":
            req = ExecutionRequest(
                identity=ExecutionIdentity(9, "job-9", 1, 1),
                execution_protocol_version=1,
                callable_path="testproject.tasks.echo_task",
                transport_version=1,
                serialized_args="[]",
                serialized_kwargs="{}",
                input_reference=None,
                runtime_env_profile=None,
                runtime_env_hash="0" * 64,
                runtime_env_plan_identity={},
                compiled_graph_submission_transport="ray-job",
            )
            serialized = encode_execution_request(req)
            metadata = build_ray_job_request_metadata(req, serialized)
            monkeypatch.setenv(
                "RAY_JOB_CONFIG_JSON_ENV_VAR",
                json.dumps({"metadata": metadata}),
            )
        else:
            monkeypatch.setenv("RAY_JOB_CONFIG_JSON_ENV_VAR", "{malformed")

        # Run inside an isolated Context() with no inherited TaskContext,
        # matching a fresh remote Ray worker process.
        with pytest.raises(NestedExecutionRequestRejected) as caught_map:
            contextvars.Context().run(distributed._parallel_map_remote, pickled, 3, {})
        assert caught_map.value.classification is NestedExecutionRequestRejection.INVALID_VERSIONED

        with pytest.raises(NestedExecutionRequestRejected) as caught_starmap:
            contextvars.Context().run(distributed._parallel_starmap_remote, pickled, (3,))
        assert (
            caught_starmap.value.classification is NestedExecutionRequestRejection.INVALID_VERSIONED
        )

        with pytest.raises(NestedExecutionRequestRejected) as caught_scatter:
            contextvars.Context().run(distributed._scatter_gather_remote, pickled, (3,), {})
        assert (
            caught_scatter.value.classification is NestedExecutionRequestRejection.INVALID_VERSIONED
        )

        assert events == []


class TestRayJobRunnerFamilyAndRayTaskContract:
    """Close R2-005 & R2-030: runner_family and @ray_task resource/profile wiring."""

    def test_ray_job_runner_exposes_ray_job_runner_family(self, settings: Any) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "http://trusted-head:8265"}
        runner = RayJobRunner()
        assert RayJobRunner.runner_family == "ray_job"
        assert runner.runner_family == "ray_job"

    @pytest.mark.django_db
    def test_ray_job_runner_honors_ray_task_contract_resources_and_profile(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        from django_ray.models import RayTaskExecution, TaskState

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "http://trusted-head:8265",
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "rq2-store"),
            "RUNTIME_ENV_PROFILES": {
                "gpu-profile": {"env_vars": {"CUDA_MODE": "enabled"}},
                "explicit-profile": {"env_vars": {"CUSTOM": "override"}},
            },
        }
        empty_env = normalize_runtime_env({})
        submissions: list[dict[str, Any]] = []

        class RecordingJobClient:
            _address = "http://127.0.0.1:8265"

            def _upload_working_dir_if_needed(self, _spec: dict[str, Any]) -> None:
                return None

            def _upload_py_modules_if_needed(self, _spec: dict[str, Any]) -> None:
                return None

            def submit_job(self, **kwargs: Any) -> str:
                submissions.append(kwargs)
                return str(kwargs["submission_id"])

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _addr=None: RecordingJobClient())

        execution = RayTaskExecution.objects.create(
            task_id="ray-job-contract-1",
            callable_path=f"{__name__}._w4_contract_ray_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-contract",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            args_json="[4]",
            kwargs_json="{}",
            runtime_env_profile=None,
            runtime_env_json=empty_env.serialized,
            runtime_env_hash=empty_env.digest,
        )
        runner.submit(
            execution,
            f"{__name__}._w4_contract_ray_task",
            (4,),
            {},
        )
        assert len(submissions) == 1
        submitted = submissions[0]
        assert submitted["entrypoint_num_cpus"] == 2.5
        assert submitted["entrypoint_num_gpus"] == 0.5
        assert submitted["entrypoint_memory"] == 268_435_456
        assert submitted["runtime_env"] == {"env_vars": {"CUDA_MODE": "enabled"}}

        # When an execution already carries an explicit persisted RuntimeEnv profile,
        # keep the persisted snapshot while still forwarding CPU/GPU/memory resources.
        explicit_env = normalize_runtime_env(
            {"env_vars": {"CUSTOM": "override"}},
            profile="explicit-profile",
        )
        explicit_execution = RayTaskExecution.objects.create(
            task_id="ray-job-contract-2",
            callable_path=f"{__name__}._w4_contract_ray_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-contract",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            args_json="[5]",
            kwargs_json="{}",
            runtime_env_profile=explicit_env.profile,
            runtime_env_json=explicit_env.serialized,
            runtime_env_hash=explicit_env.digest,
        )
        runner.submit(
            explicit_execution,
            f"{__name__}._w4_contract_ray_task",
            (5,),
            {},
        )
        assert len(submissions) == 2
        assert submissions[1]["runtime_env"] == {"env_vars": {"CUSTOM": "override"}}
        assert submissions[1]["entrypoint_num_cpus"] == 2.5

        # Partial contracts omit unset entrypoint resource kwargs cleanly.
        cpu_only_execution = RayTaskExecution.objects.create(
            task_id="ray-job-contract-3",
            callable_path=f"{__name__}._w4_cpu_only_ray_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-contract",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            args_json="[6]",
            kwargs_json="{}",
            runtime_env_profile=None,
            runtime_env_json=empty_env.serialized,
            runtime_env_hash=empty_env.digest,
        )
        runner.submit(
            cpu_only_execution,
            f"{__name__}._w4_cpu_only_ray_task",
            (6,),
            {},
        )
        assert len(submissions) == 3
        assert submissions[2]["entrypoint_num_cpus"] == 4.0
        assert "entrypoint_num_gpus" not in submissions[2]
        assert "entrypoint_memory" not in submissions[2]


class TestWorkerLivenessProgressProbe:
    """Close R2-029: base worker livenessProbe detects deadlocked S/D main loops."""

    def test_base_worker_liveness_probe_checks_deadlock_and_progress(self) -> None:
        worker_yaml = ROOT / "k8s" / "base" / "django-ray-worker.yaml"
        doc = next(yaml.safe_load_all(worker_yaml.read_text(encoding="utf-8")))
        container = doc["spec"]["template"]["spec"]["containers"][0]
        probe_cmd = " ".join(container["livenessProbe"]["exec"]["command"])

        assert "/proc/1/stat" in probe_cmd
        assert "/proc/1/status" in probe_cmd
        assert '"D"' in probe_cmd
        assert '"T"' in probe_cmd
        assert "voluntary|nonvoluntary" in probe_cmd
        assert "django_ray_worker" in probe_cmd
        assert "django_ray_worker_ready" not in probe_cmd
