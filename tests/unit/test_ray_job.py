"""Unit tests for Ray Job runner payload handling."""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from django_ray.runner import RayJobSubmissionUncertainError
from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_job import (
    _CONTROL_REQUEST_TIMEOUT_SECONDS,
    RayJobRunner,
    _address_pinned_job_client,
    _bounded_control_requests,
    _find_auto_ray_address,
    _resolve_submission_address,
)
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    normalize_runtime_env,
    runtime_env_for_storage,
)
from django_ray.workflow_plans import WorkflowPlanMismatchError


class FakeJobClient:
    """Test double for Ray's JobSubmissionClient."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_job(self, **kwargs: object) -> str:
        """Record submit call and return deterministic job id."""
        self.submissions.append(kwargs)
        return str(kwargs["submission_id"])

    @staticmethod
    def _package_uri(value: object) -> object:
        if not isinstance(value, str):
            return value
        path = Path(value)
        if path.is_dir():
            digest = hashlib.sha256()
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file():
                    digest.update(candidate.relative_to(path).as_posix().encode())
                    digest.update(candidate.read_bytes())
            return f"gcs://_ray_pkg_{digest.hexdigest()[:16]}.zip"
        if not path.is_file():
            return value
        digest = hashlib.sha1(path.read_bytes()).hexdigest()  # noqa: S324 - Ray contract
        return f"gcs://_ray_pkg_{digest}.zip"

    def _upload_working_dir_if_needed(self, runtime_env: dict[str, object]) -> None:
        if "working_dir" in runtime_env:
            runtime_env["working_dir"] = self._package_uri(runtime_env["working_dir"])

    def _upload_py_modules_if_needed(self, runtime_env: dict[str, object]) -> None:
        modules = runtime_env.get("py_modules")
        if isinstance(modules, list):
            runtime_env["py_modules"] = [self._package_uri(module) for module in modules]


class TestRayJobAddressResolution:
    """Selected Ray targets must be resolved without ambient routing overrides."""

    @pytest.mark.parametrize(
        ("gcs_addresses", "bootstrap_address", "expected"),
        [
            ({"cluster-a:6379", "cluster-b:6379"}, "latest:6379", "latest:6379"),
            ({"cluster-a:6379"}, "latest:6379", "cluster-a:6379"),
            (set(), "latest:6379", "latest:6379"),
        ],
    )
    def test_find_auto_ray_address_matches_env_free_discovery(
        self,
        monkeypatch,
        gcs_addresses,
        bootstrap_address,
        expected,
    ) -> None:
        from ray._private import services

        monkeypatch.setattr(services, "find_gcs_addresses", lambda: gcs_addresses)
        monkeypatch.setattr(
            services,
            "find_bootstrap_address",
            lambda _temp_dir: bootstrap_address,
        )

        assert _find_auto_ray_address() == expected

    def test_find_auto_ray_address_requires_a_running_cluster(self, monkeypatch) -> None:
        from ray._private import services

        monkeypatch.setattr(services, "find_gcs_addresses", set)
        monkeypatch.setattr(services, "find_bootstrap_address", lambda _temp_dir: None)

        with pytest.raises(ConnectionError, match="explicit 'auto' target"):
            _find_auto_ray_address()

    def test_resolve_submission_address_keeps_http_target(self, monkeypatch) -> None:
        monkeypatch.setenv("RAY_ADDRESS", "http://global-dashboard:8265")
        monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://api-override:8265")

        assert (
            _resolve_submission_address("https://alias-dashboard:8265")
            == "https://alias-dashboard:8265"
        )

    @pytest.mark.parametrize(
        ("selected", "resolved_input"),
        [
            ("cluster-head:6379", "cluster-head:6379"),
            ("auto", "autodetected-head:6379"),
        ],
    )
    def test_resolve_submission_address_uses_selected_bootstrap(
        self,
        monkeypatch,
        selected,
        resolved_input,
    ) -> None:
        from ray.dashboard import utils as dashboard_utils

        inputs: list[str] = []
        monkeypatch.setattr(
            "django_ray.runner.ray_job._find_auto_ray_address",
            lambda: "autodetected-head:6379",
        )
        monkeypatch.setattr(
            dashboard_utils,
            "ray_address_to_api_server_url",
            lambda address: inputs.append(address) or "http://alias-dashboard:8265",
        )
        monkeypatch.setenv("RAY_ADDRESS", "global-head:6379")

        assert _resolve_submission_address(selected) == "http://alias-dashboard:8265"
        assert inputs == [resolved_input]


class TestRayJobRunnerSubmit:
    """Tests for RayJobRunner.submit."""

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("task_id", "django-task-replacement"),
            ("pk", 124),
            ("attempt_number", 3),
            ("execution_generation", 12),
        ],
    )
    def test_submission_id_fences_each_execution_identity_field(
        self,
        field,
        replacement,
    ) -> None:
        values = {
            "task_id": "django-task-123",
            "pk": 123,
            "attempt_number": 2,
            "execution_generation": 11,
        }
        baseline = RayJobRunner.submission_id(SimpleNamespace(**values))
        changed_values = values | {field: replacement}
        changed = RayJobRunner.submission_id(SimpleNamespace(**changed_values))

        assert RayJobRunner.submission_id(SimpleNamespace(**values)) == baseline
        assert changed != baseline
        digest = baseline.removeprefix("raysubmit_django_ray_v1_")
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_submission_handle_exposes_the_identity_before_submit(self) -> None:
        runner = RayJobRunner()
        task_execution = SimpleNamespace(
            pk=123,
            task_id="django-task-123",
            attempt_number=2,
            execution_generation=11,
            ray_target_address="ray://selected-cluster:10001",
            ray_address="ray://stale-cluster:10001",
        )

        handle = runner.submission_handle(task_execution)

        assert handle.ray_job_id == RayJobRunner.submission_id(task_execution)
        assert handle.ray_address == "ray://selected-cluster:10001"
        assert handle.submitted_at.tzinfo is UTC

    def test_submit_uses_base64_payload_entrypoint(self, monkeypatch) -> None:
        """Task payload should be transported as base64, not interpolated source."""
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)

        task_execution = SimpleNamespace(
            pk=123,
            task_id="django-task-123",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=2,
            execution_generation=11,
        )

        handle = runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=("it's broken",),
            kwargs={"publisher": "O'Reilly"},
        )

        assert handle.ray_job_id == RayJobRunner.submission_id(task_execution)
        assert handle.ray_job_id.startswith("raysubmit_django_ray_v1_")
        assert len(handle.ray_job_id) == len("raysubmit_django_ray_v1_") + 64
        assert len(fake_client.submissions) == 1

        submission = fake_client.submissions[0]
        assert submission["submission_id"] == handle.ray_job_id
        entrypoint = str(submission["entrypoint"])
        prefix = "python -m django_ray.runtime.entrypoint --payload-b64 "

        assert entrypoint.startswith(prefix)
        assert "it's broken" not in entrypoint
        assert "O'Reilly" not in entrypoint

        payload_b64 = entrypoint.removeprefix(prefix)
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        payload = json.loads(payload_json)

        assert payload["callable_path"] == "testproject.tasks.echo_task"
        assert json.loads(payload["serialized_args"]) == ["it's broken"]
        assert json.loads(payload["serialized_kwargs"]) == {"publisher": "O'Reilly"}
        assert payload["task_execution_pk"] == 123
        assert payload["task_id"] == "django-task-123"
        assert payload["attempt_number"] == 2
        assert payload["execution_generation"] == 11
        assert payload["runtime_env_profile"] is None
        assert len(payload["runtime_env_hash"]) == 64
        assert payload["runtime_env_plan_identity"]["plan_format"] == (
            "django-ray.runtime-env-plan"
        )
        assert payload["runtime_env_plan_identity"]["plan_format_version"] == 1
        assert payload["runtime_env_plan_identity"]["reusable"] is True
        assert payload["runtime_env_plan_identity"]["unresolved_paths"] == []
        assert submission["metadata"] == {
            "django_ray_task_id": "123",
            "django_ray_attempt_number": "2",
            "django_ray_execution_generation": "11",
            "callable_path": "testproject.tasks.echo_task",
            "runtime_env_profile": "",
            "runtime_env_hash": (
                "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            ),
        }

    def test_submit_rejects_a_returned_identity_mismatch(self, monkeypatch) -> None:
        class MismatchedJobClient(FakeJobClient):
            def submit_job(self, **kwargs: object) -> str:
                super().submit_job(**kwargs)
                return "raysubmit_unexpected"

        fake_client = MismatchedJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=129,
            task_id="django-task-129",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=4,
            execution_generation=15,
        )

        with pytest.raises(
            RayJobSubmissionUncertainError,
            match="unexpected ID",
        ) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        expected_id = RayJobRunner.submission_id(task_execution)
        assert exc_info.value.submission_id == expected_id
        assert exc_info.value.observed_submission_id == "raysubmit_unexpected"
        assert exc_info.value.__cause__ is None
        assert fake_client.submissions[0]["submission_id"] == expected_id

    def test_submit_wraps_only_the_submission_rpc_as_uncertain(self, monkeypatch) -> None:
        submission_error = TimeoutError("response timed out after acceptance")

        class TimingOutJobClient(FakeJobClient):
            def submit_job(self, **kwargs: object) -> str:
                self.submissions.append(kwargs)
                raise submission_error

        fake_client = TimingOutJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=130,
            task_id="django-task-130",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=16,
        )

        with pytest.raises(RayJobSubmissionUncertainError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        expected_id = RayJobRunner.submission_id(task_execution)
        assert exc_info.value.submission_id == expected_id
        assert exc_info.value.__cause__ is submission_error
        assert fake_client.submissions[0]["submission_id"] == expected_id

    def test_submit_treats_post_request_snapshot_cleanup_failure_as_uncertain(
        self,
        monkeypatch,
    ) -> None:
        import django_ray.runner.ray_job as ray_job_module

        cleanup_error = PermissionError("temporary snapshot cleanup failed")
        original_snapshot = ray_job_module.snapshot_local_runtime_env

        @contextmanager
        def cleanup_fails(runtime_env):
            with original_snapshot(runtime_env) as immutable_snapshot:
                yield immutable_snapshot
            raise cleanup_error

        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        monkeypatch.setattr(ray_job_module, "snapshot_local_runtime_env", cleanup_fails)
        task_execution = SimpleNamespace(
            pk=132,
            task_id="django-task-132",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=18,
        )

        with pytest.raises(RayJobSubmissionUncertainError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        expected_id = RayJobRunner.submission_id(task_execution)
        assert exc_info.value.submission_id == expected_id
        assert exc_info.value.__cause__ is cleanup_error
        assert fake_client.submissions[0]["submission_id"] == expected_id

    def test_submit_keeps_pre_request_errors_definite(self, monkeypatch) -> None:
        address_error = ConnectionError("selected Ray dashboard is unavailable")
        runner = RayJobRunner()

        def fail_before_request(_ray_address=None):
            raise address_error

        monkeypatch.setattr(runner, "_get_client", fail_before_request)
        task_execution = SimpleNamespace(
            pk=131,
            task_id="django-task-131",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=17,
        )

        with pytest.raises(ConnectionError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value is address_error

    def test_submit_keeps_runtime_env_secrets_out_of_plan_identity_payload(
        self,
        monkeypatch,
    ) -> None:
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        runtime_env = normalize_runtime_env(
            {"env_vars": {"API_TOKEN": "do-not-persist"}},
            profile="secret-profile",
        )
        task_execution = SimpleNamespace(
            pk=125,
            task_id="django-task-125",
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        entrypoint = str(fake_client.submissions[0]["entrypoint"])
        encoded = entrypoint.rsplit(" ", 1)[-1]
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        assert "do-not-persist" not in json.dumps(payload)
        assert payload["runtime_env_plan_identity"]["reusable"] is False
        assert payload["runtime_env_plan_identity"]["unresolved_paths"] == [
            "spec.env_vars.API_TOKEN.value"
        ]

    def test_submit_rejects_corrupt_runtime_env_before_upload_or_request(
        self,
        monkeypatch,
    ) -> None:
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _ray_address=None: pytest.fail("Ray Job client was opened"),
        )
        runtime_env = normalize_runtime_env(
            {"env_vars": {"VALUE": "arbitrary-customer-marker-7cf3"}},
            profile="thin",
        )
        task_execution = SimpleNamespace(
            pk=126,
            task_id="django-task-126-corrupt",
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash="0" * 64,
            attempt_number=1,
            execution_generation=2,
        )

        with pytest.raises(RuntimeEnvSnapshotError, match="hash does not match") as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert "arbitrary-customer-marker-7cf3" not in str(exc_info.value)

    def test_submit_keeps_large_runtime_env_identity_out_of_entrypoint(self, monkeypatch) -> None:
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        runtime_env = normalize_runtime_env(
            {"excludes": [f"{'x' * 2040}{index:04d}" for index in range(1024)]}
        )
        task_execution = SimpleNamespace(
            pk=126,
            task_id="django-task-126",
            runtime_env_profile=None,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        entrypoint = str(fake_client.submissions[0]["entrypoint"])
        assert len(entrypoint.encode("utf-8")) < 32 * 1024

    def test_submit_uploads_immutable_local_runtime_env_snapshots(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        working_dir = tmp_path / "working-dir"
        working_dir.mkdir()
        (working_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        py_module = tmp_path / "shared_module"
        py_module.mkdir()
        (py_module / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
        runtime_env = normalize_runtime_env(
            {
                "working_dir": str(working_dir),
                "py_modules": [str(py_module)],
            }
        )
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=127,
            task_id="django-task-127",
            runtime_env_profile=None,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        submitted_runtime_env = fake_client.submissions[0]["runtime_env"]
        assert isinstance(submitted_runtime_env, dict)
        assert str(submitted_runtime_env["working_dir"]).startswith("gcs://_ray_pkg_")
        assert len(str(submitted_runtime_env["working_dir"]).split("_")[-1]) == 20
        submitted_py_modules = submitted_runtime_env["py_modules"]
        assert isinstance(submitted_py_modules, list)
        assert str(submitted_py_modules[0]).startswith("gcs://_ray_pkg_")
        assert len(str(submitted_py_modules[0]).split("_")[-1]) == 20

    def test_submit_rejects_local_source_mutation_before_job_creation(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        working_dir = tmp_path / "working-dir"
        working_dir.mkdir()
        source = working_dir / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        runtime_env = normalize_runtime_env({"working_dir": str(working_dir)})

        class MutatingJobClient(FakeJobClient):
            def _upload_working_dir_if_needed(self, runtime_env: dict[str, object]) -> None:
                source.write_text("VALUE = 2\n", encoding="utf-8")
                super()._upload_working_dir_if_needed(runtime_env)

        fake_client = MutatingJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=128,
            task_id="django-task-128",
            runtime_env_profile=None,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
        )

        with pytest.raises(WorkflowPlanMismatchError, match="changed"):
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert fake_client.submissions == []

    def test_submit_transports_external_input_by_reference_only(self, monkeypatch) -> None:
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        reference = "s3://inputs/django-ray/inputs/aa/bb/" + "a" * 64 + ".json?bytes=4"
        task_execution = SimpleNamespace(
            pk=124,
            task_id="django-task-124",
            input_reference=reference,
            args_json="null",
            kwargs_json="null",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=2,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        entrypoint_value = str(fake_client.submissions[0]["entrypoint"])
        encoded = entrypoint_value.rsplit(" ", 1)[-1]
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode())
        assert payload["transport_version"] == 2
        assert payload["input_reference"] == reference
        assert "serialized_args" not in payload
        assert "serialized_kwargs" not in payload

    def test_submit_uses_runtime_env_and_configured_ray_address(self, monkeypatch) -> None:
        """Submit should pass configured runtime_env and keep configured ray_address."""
        fake_client = FakeJobClient()
        addresses: list[str | None] = []
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: {"RAY_ADDRESS": "ray://unit-test:10001"},
        )
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or fake_client,
        )

        runtime_env = normalize_runtime_env(
            {"env_vars": {"MY_ENV": "1"}},
            profile="custom",
        )
        handle = runner.submit(
            task_execution=SimpleNamespace(
                pk=55,
                task_id="django-task-55",
                runtime_env_profile=runtime_env.profile,
                runtime_env_json=runtime_env.serialized,
                runtime_env_hash=runtime_env.digest,
                attempt_number=1,
                execution_generation=4,
            ),
            callable_path="testproject.tasks.add_numbers",
            args=(1, 2),
            kwargs={},
        )

        assert handle.ray_address == "ray://unit-test:10001"
        assert addresses == ["ray://unit-test:10001"]
        submission = fake_client.submissions[0]
        assert submission["runtime_env"] == {"env_vars": {"MY_ENV": "1"}}

    def test_submit_decrypts_stored_runtime_env_before_job_submission(
        self,
        monkeypatch,
        settings,
    ) -> None:
        key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode("ascii")
        encryption_config = {
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"runner-key": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "runner-key",
        }
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "ray://encrypted-job:10001",
            **encryption_config,
        }
        runtime_env = normalize_runtime_env(
            {"env_vars": {"EXECUTION_MODE": "encrypted-ray-job"}},
            profile="encrypted-job",
        )
        task_id = "encrypted-ray-job-task"
        stored = runtime_env_for_storage(
            runtime_env,
            task_id=task_id,
            config=encryption_config,
        )
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)

        runner.submit(
            task_execution=SimpleNamespace(
                pk=56,
                task_id=task_id,
                runtime_env_profile=stored.profile,
                runtime_env_json=stored.serialized,
                runtime_env_hash=stored.digest,
                attempt_number=1,
                execution_generation=4,
            ),
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert stored.serialized != runtime_env.serialized
        submission = fake_client.submissions[0]
        assert submission["runtime_env"] == runtime_env.spec
        entrypoint = str(submission["entrypoint"])
        payload = json.loads(base64.urlsafe_b64decode(entrypoint.rsplit(" ", 1)[-1]).decode())
        assert payload["runtime_env_profile"] == runtime_env.profile
        assert payload["runtime_env_hash"] == runtime_env.digest
        assert payload["runtime_env_plan_identity"]["profile"] == runtime_env.profile
        metadata = submission["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["runtime_env_profile"] == runtime_env.profile
        assert metadata["runtime_env_hash"] == runtime_env.digest

    def test_get_client_pins_configured_address_against_ray_environment(
        self,
        monkeypatch,
    ) -> None:
        """Ambient Ray variables must not replace durable django-ray routing."""
        from ray import __version__ as ray_version
        from ray.dashboard.modules.job import sdk as job_sdk

        resolved: list[str] = []

        def resolve_submission_address(address: str) -> str:
            resolved.append(address)
            return "http://alias-dashboard:8265"

        monkeypatch.setattr(
            "django_ray.runner.ray_job._resolve_submission_address",
            resolve_submission_address,
        )
        monkeypatch.setattr(
            job_sdk.JobSubmissionClient,
            "_check_connection_and_version",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setenv("RAY_ADDRESS", "http://global-dashboard:8265")
        monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://api-override:8265")
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: {"RAY_ADDRESS": "ray://alias-head:10001"},
        )

        client = RayJobRunner()._get_client()

        assert isinstance(client, job_sdk.JobSubmissionClient)
        assert resolved == ["ray://alias-head:10001"]
        assert client._address == "http://alias-dashboard:8265"
        assert client._cookies is None
        assert client._default_metadata == {}
        assert client._headers == {}
        assert client._verify is True
        assert client._ssl_context is None
        assert client._client_ray_version == ray_version

    def test_address_pinned_client_bounds_version_and_control_requests(
        self,
        monkeypatch,
    ) -> None:
        from ray import __version__ as ray_version
        from ray.dashboard.modules.dashboard_sdk import SubmissionClient

        requests: list[tuple[str, float | None]] = []

        class VersionResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {"ray_version": ray_version}

        def record_request(
            _client,
            _method,
            endpoint,
            *,
            data=None,
            json_data=None,
            **kwargs,
        ):
            del data, json_data
            requests.append((endpoint, kwargs.get("timeout")))
            return VersionResponse()

        monkeypatch.setattr(
            "django_ray.runner.ray_job._resolve_submission_address",
            lambda _address: "http://alias-dashboard:8265",
        )
        monkeypatch.setattr(SubmissionClient, "_do_request", record_request)

        client = _address_pinned_job_client("ray://alias-head:10001")
        client._do_request("GET", "/unbounded")
        with _bounded_control_requests(client):
            client._do_request("GET", "/bounded")

        assert requests == [
            ("/api/version", _CONTROL_REQUEST_TIMEOUT_SECONDS),
            ("/unbounded", None),
            ("/bounded", _CONTROL_REQUEST_TIMEOUT_SECONDS),
        ]

    def test_submit_uses_persisted_backend_target(self, monkeypatch) -> None:
        """Each backend alias must submit against its persisted Ray cluster."""
        addresses: list[str | None] = []
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or fake_client,
        )

        for index, address in enumerate(
            ("ray://alias-a:10001", "ray://alias-b:10001"),
            start=1,
        ):
            handle = runner.submit(
                task_execution=SimpleNamespace(
                    pk=index,
                    task_id=f"django-task-{index}",
                    ray_target_address=address,
                    ray_address="ray://stale-handle:10001",
                    runtime_env_profile=None,
                    runtime_env_json="{}",
                    runtime_env_hash="",
                    attempt_number=1,
                    execution_generation=0,
                ),
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )
            assert handle.ray_address == address

        assert addresses == ["ray://alias-a:10001", "ray://alias-b:10001"]

    def test_submit_accepts_legacy_address_without_dedicated_target(self, monkeypatch) -> None:
        addresses: list[str | None] = []
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or fake_client,
        )

        handle = runner.submit(
            task_execution=SimpleNamespace(
                pk=3,
                task_id="django-task-3",
                ray_address="ray://legacy:10001",
                runtime_env_profile=None,
                runtime_env_json="{}",
                runtime_env_hash="",
                attempt_number=1,
                execution_generation=0,
            ),
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert handle.ray_address == "ray://legacy:10001"
        assert addresses == ["ray://legacy:10001"]


class TestRayJobRunnerStatusAndControl:
    """Tests for get_status/cancel/get_logs behavior."""

    @staticmethod
    def _make_handle(job_id: str = "raysubmit_test_001") -> SubmissionHandle:
        return SubmissionHandle(
            ray_job_id=job_id,
            ray_address="ray://test:10001",
            submitted_at=datetime.now(UTC),
        )

    def test_get_status_maps_known_states(self, monkeypatch) -> None:
        """Known Ray status values should map to JobStatus correctly."""
        runner = RayJobRunner()
        expected = {
            "PENDING": JobStatus.PENDING,
            "RUNNING": JobStatus.RUNNING,
            "SUCCEEDED": JobStatus.SUCCEEDED,
            "FAILED": JobStatus.FAILED,
            "STOPPED": JobStatus.STOPPED,
        }

        for raw_status, mapped in expected.items():
            message = f"msg-{raw_status}"
            client = SimpleNamespace(
                get_job_status=lambda _job_id, value=raw_status: value,
                get_job_info=lambda _job_id, msg=message: SimpleNamespace(
                    message=msg,
                    start_time=123,
                    end_time=456,
                ),
            )
            monkeypatch.setattr(
                runner, "_get_client", lambda _ray_address=None, client=client: client
            )

            info = runner.get_status(self._make_handle())

            assert info.status == mapped
            assert info.message == f"msg-{raw_status}"
            assert info.start_time == 123
            assert info.end_time == 456

    def test_get_status_uses_unknown_for_unmapped_state(self, monkeypatch) -> None:
        runner = RayJobRunner()
        client = SimpleNamespace(
            get_job_status=lambda _job_id: "MYSTERY",
            get_job_info=lambda _job_id: SimpleNamespace(message="mystery"),
        )
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: client)

        info = runner.get_status(self._make_handle())

        assert info.status == JobStatus.UNKNOWN
        assert info.message == "mystery"

    def test_get_status_returns_unknown_on_client_exception(self, monkeypatch) -> None:
        runner = RayJobRunner()

        class FailingClient:
            def get_job_status(self, _job_id):
                raise RuntimeError("ray api unavailable")

        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: FailingClient())

        info = runner.get_status(self._make_handle("raysubmit_fail_001"))

        assert info.status == JobStatus.UNKNOWN
        assert info.job_id == "raysubmit_fail_001"
        assert "ray api unavailable" in (info.message or "")

    def test_status_and_cancellation_survive_broken_exception_messages(self, monkeypatch) -> None:
        calls = 0

        class BrokenControlError(RuntimeError):
            def __str__(self) -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError("secondary password=do-not-expose")

        class FailingClient:
            def get_job_status(self, _job_id):
                raise BrokenControlError()

            def stop_job(self, _job_id):
                raise BrokenControlError()

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: FailingClient())
        handle = self._make_handle("raysubmit_broken_control_001")

        info = runner.get_status(handle)
        cancellation = runner.cancel_with_status(handle)

        assert info.status == JobStatus.UNKNOWN
        assert info.message == "exception message unavailable"
        assert cancellation.status == CancellationOutcomeStatus.INDETERMINATE
        assert cancellation.message == (
            "Ray Job stop request raised BrokenControlError: exception message unavailable"
        )
        assert "secondary password" not in (cancellation.message or "")
        assert calls == 2

    def test_control_methods_normalize_client_construction_failure(self, monkeypatch) -> None:
        runner = RayJobRunner()

        def unavailable(_ray_address=None):
            raise TimeoutError("ray dashboard request timed out")

        monkeypatch.setattr(runner, "_get_client", unavailable)
        handle = self._make_handle("raysubmit_client_unavailable_001")

        info = runner.get_status(handle)
        cancellation = runner.cancel_with_status(handle)

        assert info.status == JobStatus.UNKNOWN
        assert info.job_id == handle.ray_job_id
        assert "ray dashboard request timed out" in (info.message or "")
        assert cancellation.status == CancellationOutcomeStatus.INDETERMINATE
        assert "ray dashboard request timed out" in (cancellation.message or "")
        assert runner.get_logs(handle) is None

    def test_status_logs_and_cancellation_use_handle_address(self, monkeypatch) -> None:
        """Control-plane calls must stay on the cluster recorded in the handle."""
        addresses: list[str | None] = []

        class Client:
            def get_job_status(self, _job_id: str) -> str:
                return "RUNNING"

            def get_job_info(self, _job_id: str) -> SimpleNamespace:
                return SimpleNamespace(message=None)

            def get_job_logs(self, _job_id: str) -> str:
                return "logs"

            def stop_job(self, _job_id: str) -> bool:
                return True

        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or Client(),
        )
        handle = self._make_handle()

        assert runner.get_status(handle).status == JobStatus.RUNNING
        assert runner.get_logs(handle) == "logs"
        assert runner.cancel(handle) is True
        assert addresses == ["ray://test:10001"] * 3

    def test_cancel_returns_true_on_success(self, monkeypatch) -> None:
        stopped: list[str] = []

        class Client:
            def stop_job(self, job_id: str) -> bool:
                stopped.append(job_id)
                return True

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        ok = runner.cancel(self._make_handle("raysubmit_cancel_001"))

        assert ok is True
        assert stopped == ["raysubmit_cancel_001"]

    def test_cancel_returns_false_when_job_was_not_running(self, monkeypatch) -> None:
        class Client:
            def stop_job(self, _job_id: str) -> bool:
                return False

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())
        handle = self._make_handle("raysubmit_cancel_not_running_001")

        assert runner.cancel(handle) is False
        outcome = runner.cancel_with_status(handle)
        assert outcome.status == CancellationOutcomeStatus.NOT_APPLICABLE
        assert "not running" in (outcome.message or "")

    def test_cancel_returns_false_on_exception(self, monkeypatch) -> None:
        class Client:
            def stop_job(self, _job_id: str) -> None:
                raise RuntimeError("cannot stop")

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        ok = runner.cancel(self._make_handle("raysubmit_cancel_002"))

        assert ok is False

    def test_cancel_with_status_preserves_indeterminate_api_failure(self, monkeypatch) -> None:
        class Client:
            def stop_job(self, _job_id: str) -> None:
                raise RuntimeError("cannot stop")

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        outcome = runner.cancel_with_status(self._make_handle("raysubmit_cancel_003"))

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "cannot stop" in (outcome.message or "")

    def test_get_logs_returns_none_on_exception(self, monkeypatch) -> None:
        class Client:
            def get_job_logs(self, _job_id: str) -> str:
                raise RuntimeError("logs unavailable")

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        logs = runner.get_logs(self._make_handle("raysubmit_logs_001"))

        assert logs is None

    def test_get_logs_returns_log_content(self, monkeypatch) -> None:
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _ray_address=None: SimpleNamespace(
                get_job_logs=lambda _job_id: "line-1\nline-2"
            ),
        )

        logs = runner.get_logs(self._make_handle("raysubmit_logs_002"))

        assert logs == "line-1\nline-2"
