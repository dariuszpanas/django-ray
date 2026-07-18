"""Unit tests for Ray Job runner payload handling."""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_job import RayJobRunner


class FakeJobClient:
    """Test double for Ray's JobSubmissionClient."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_job(self, **kwargs: object) -> str:
        """Record submit call and return deterministic job id."""
        self.submissions.append(kwargs)
        return "raysubmit_test_001"


class TestRayJobRunnerSubmit:
    """Tests for RayJobRunner.submit."""

    def test_submit_uses_base64_payload_entrypoint(self, monkeypatch) -> None:
        """Task payload should be transported as base64, not interpolated source."""
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)

        task_execution = SimpleNamespace(
            pk=123,
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

        assert handle.ray_job_id == "raysubmit_test_001"
        assert len(fake_client.submissions) == 1

        submission = fake_client.submissions[0]
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
        assert payload["attempt_number"] == 2
        assert payload["execution_generation"] == 11
        assert payload["runtime_env_profile"] is None
        assert len(payload["runtime_env_hash"]) == 64
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

    def test_submit_uses_runtime_env_and_configured_ray_address(self, monkeypatch) -> None:
        """Submit should pass configured runtime_env and keep configured ray_address."""
        fake_client = FakeJobClient()
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: {"RAY_ADDRESS": "ray://unit-test:10001"},
        )
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)

        handle = runner.submit(
            task_execution=SimpleNamespace(
                pk=55,
                runtime_env_profile="custom",
                runtime_env_json='{"env_vars":{"MY_ENV":"1"}}',
                runtime_env_hash="",
                attempt_number=1,
                execution_generation=4,
            ),
            callable_path="testproject.tasks.add_numbers",
            args=(1, 2),
            kwargs={},
        )

        assert handle.ray_address == "ray://unit-test:10001"
        submission = fake_client.submissions[0]
        assert submission["runtime_env"] == {"env_vars": {"MY_ENV": "1"}}

    def test_get_client_uses_configured_address(self, monkeypatch) -> None:
        created: list[str] = []

        class FakeClient:
            def __init__(self, address: str) -> None:
                created.append(address)

        monkeypatch.setitem(
            sys.modules, "ray.job_submission", SimpleNamespace(JobSubmissionClient=FakeClient)
        )
        runner = RayJobRunner()

        runner._get_client()

        assert created == [runner.ray_address]

    def test_submit_uses_persisted_backend_address(self, monkeypatch) -> None:
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
                    ray_address=address,
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

            def stop_job(self, _job_id: str) -> None:
                return None

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
            def stop_job(self, job_id: str) -> None:
                stopped.append(job_id)

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        ok = runner.cancel(self._make_handle("raysubmit_cancel_001"))

        assert ok is True
        assert stopped == ["raysubmit_cancel_001"]

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
