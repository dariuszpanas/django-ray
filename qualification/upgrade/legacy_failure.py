"""Real terminal Jobs reconciled by a candidate manager without log authority."""

from __future__ import annotations

import json
import shlex
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path


def manager() -> None:
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import execute_from_command_line

    from django_ray.runner.ray_job import RayJobRunner

    def reject_logs(self, handle):
        # Observe the otherwise real runner without replacing status or stop RPCs.
        (settings.ROOT / "legacy-log-requested").write_text("requested")
        raise AssertionError("legacy failure requested job logs")

    RayJobRunner.get_logs = reject_logs
    execute_from_command_line(["django-ray-qualification", "django_ray_worker", "--concurrency=1"])


def run(root: Path) -> dict[str, object]:
    import psutil
    import ray
    from django.conf import settings
    from django.db import connections
    from django.utils import timezone
    from ray.job_submission import JobStatus, JobSubmissionClient

    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
    from qualification.upgrade.native_probe import snapshot

    assert settings.RUNNER == "ray_job"
    assert settings.DJANGO_RAY["MAX_TASK_ATTEMPTS"] == 3
    before = snapshot()
    process = None
    log_requested = root / "legacy-log-requested"
    log_requested.unlink(missing_ok=True)
    effects = [root / f"legacy-failure-effect-{index}" for index in range(4)]
    for effect in effects:
        effect.unlink(missing_ok=True)
    log = (root / "manager.log").open("wb")
    try:
        ray.init(
            address="local",
            num_cpus=1,
            num_gpus=0,
            object_store_memory=128 * 1024 * 1024,
            include_dashboard=True,
        )
        client = JobSubmissionClient("http://127.0.0.1:8265")
        rows = []
        for index, effect in enumerate(effects):
            job_id = f"raysubmit_legacy_failure_probe_{index}"
            program = (
                "from pathlib import Path; import sys; "
                f"Path({str(effect)!r}).open('a').write('x'); sys.exit({1 if index < 2 else 0})"
            )
            client.submit_job(
                submission_id=job_id,
                entrypoint=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
                entrypoint_num_cpus=0.25,
                metadata={"django_ray_attempt_number": "1", "django_ray_execution_generation": "0"},
            )
            deadline = time.monotonic() + 60
            expected_status = JobStatus.FAILED if index < 2 else JobStatus.SUCCEEDED
            while client.get_job_status(job_id) != expected_status:
                if time.monotonic() >= deadline:
                    raise TimeoutError("fixture Ray Job did not reach its expected terminal status")
                time.sleep(0.1)
            assert effect.read_text() == "x"
            # Seed only a historical RUNNING carrier. The real candidate manager,
            # not fixture code, must apply either terminal transition.
            rows.append(
                RayTaskExecution.objects.create(
                    task_id=f"legacy-failure-probe-{index}",
                    callable_path="qualification.upgrade.native_tasks.controlled",
                    queue_name="default",
                    state="RUNNING",
                    started_at=timezone.now() - timedelta(minutes=10),
                    ray_job_id=job_id,
                    ray_address="http://127.0.0.1:8265",
                    attempt_number=1,
                    execution_generation=0,
                    args_json="[]",
                    kwargs_json="{}",
                    completion_data=(
                        json.dumps({"success": True, "result": 7})
                        if index == 1
                        else "{not-json"
                        if index == 2
                        else None
                    ),
                )
            )
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "qualification.upgrade.legacy_failure"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                raise RuntimeError("candidate manager exited during legacy failure probe")
            for row in rows:
                row.refresh_from_db()
            if [row.state for row in rows] == ["LOST", "SUCCEEDED", "LOST", "LOST"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("candidate did not reconcile terminal legacy Jobs")
            time.sleep(0.1)
        # Observe beyond one manager heartbeat with retries configured available.
        time.sleep(3)
        for row in rows:
            row.refresh_from_db()
            assert (row.attempt_number, row.execution_generation) == (1, 0)
        assert [row.state for row in rows] == ["LOST", "SUCCEEDED", "LOST", "LOST"]
        assert "application effects are unknown" in rows[0].error_message
        assert rows[0].error_traceback is None
        assert json.loads(rows[1].result_data) == 7
        assert not TaskAttempt.objects.filter(execution__in=rows, attempt_number__gt=1).exists()
        assert not log_requested.exists()
        assert all(effect.read_text() == "x" for effect in effects)
        fields = {name: value["fields"] for name, value in before.items()}
        assert snapshot(fields, before) == before, "existing history changed"
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise
                assert process.returncode == 128 + signal.SIGTERM
        finally:
            children = psutil.Process().children(recursive=True)
            ray.shutdown()
            _, alive = psutil.wait_procs(children, timeout=15)
            log.close()
            assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]
    assert not TaskWorkerLease.objects.filter(is_active=True).exists()
    assert not RayTaskExecution.objects.exclude(
        state__in={"SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}
    ).exists()
    connections.close_all()
    return {
        "real_failed_jobs": 2,
        "real_succeeded_jobs": 2,
        "expired_malformed_outcome": "LOST",
        "expired_missing_outcome": "LOST",
        "fixture_effects": 4,
        "unknown_outcome": "LOST",
        "valid_completion_outcome": "SUCCEEDED",
        "automatic_retries": 0,
        "job_log_requests": 0,
        "history_preserved": True,
        "active_leases": 0,
        "ray_children_reaped": True,
    }


if __name__ == "__main__":
    manager()
