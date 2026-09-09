"""Exercise real rq2 Jobs, durable receipts and fresh manager processes."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from qualification.latency.processes import JobsProxy, Manager


def read_json(path):
    return json.loads(path.read_text())


def wait(predicate, managers, *, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for manager in managers:
            if manager.process.poll() is not None:
                print(bytes(manager.output).decode(errors="replace"), flush=True)
                raise RuntimeError("manager exited before the assertion")
        try:
            value = predicate()
        except (FileNotFoundError, json.JSONDecodeError):
            value = None
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError("latency assertion deadline expired")


def run(root: Path, expected_module: str) -> dict:
    import django_ray
    from tests.local_ray import init_local_ray

    if str(Path(django_ray.__file__).resolve()) != expected_module:
        raise RuntimeError("latency driver imported another candidate")
    if not root.is_dir() or root.is_symlink() or any(root.iterdir()):
        raise RuntimeError("latency fixture must be an empty owned directory")
    import ray

    managers = []
    cases: list[dict[str, Any]] = []
    proxy = None
    failure = None
    try:
        init_local_ray(include_dashboard=True)
        proxy = JobsProxy("http://127.0.0.1:8265")
        (root / "config.json").write_text(
            json.dumps(
                {
                    "address": proxy.address,
                    "pythonpath": os.environ["PYTHONPATH"],
                }
            )
        )
        (root / "inputs").mkdir()
        os.environ["DJANGO_SETTINGS_MODULE"] = "qualification.latency.settings"
        os.environ["DJANGO_RAY_LATENCY_ROOT"] = str(root)
        import django

        django.setup()
        from django.core.management import call_command
        from django.db import connection

        from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
        from qualification.latency.tasks import held_result

        call_command("migrate", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            assert cursor.fetchone() == ("wal",)

        def start_manager(name, *, recovery_only=False):
            manager = Manager(name, recovery_only=recovery_only)
            managers.append(manager)
            ready = wait(lambda: read_json(root / f"{name}-ready.json"), managers)
            return manager, ready

        def stop_manager(manager, name):
            manager.stop()
            managers.remove(manager)
            metrics = read_json(root / f"{name}-metrics.json")
            assert metrics["module"] == expected_module
            assert metrics["shutdown_exit_code"] == 143
            assert metrics["counters"]["peak_active"] <= 1
            return metrics

        def enqueue(*, fail=False):
            result = held_result.enqueue(fail=fail)
            return RayTaskExecution.objects.get(task_id=result.id)

        def started(task):
            receipt = wait(lambda: read_json(root / f"started-{task.pk}.json"), managers)
            assert receipt["module"] == expected_module
            task.refresh_from_db()
            assert task.state == TaskState.RUNNING
            assert task.ray_job_id.startswith("raysubmit_django_ray_rq2_")
            assert task.ray_job_request_reference
            return receipt

        def complete(task, *, fail=False):
            before = started(task)
            identity = (task.ray_job_id, task.attempt_number, task.execution_generation)
            released_ns = time.monotonic_ns()
            (root / f"release-{task.pk}").touch(exist_ok=False)

            def is_terminal():
                task.refresh_from_db()
                return task.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.LOST)

            wait(is_terminal, managers)
            terminal_ns = time.monotonic_ns()
            committed = wait(lambda: read_json(root / f"completion-{task.pk}.json"), managers)
            assert task.state == (TaskState.FAILED if fail else TaskState.SUCCEEDED)
            assert (task.ray_job_id, task.attempt_number, task.execution_generation) == identity
            assert task.attempt_number == 1
            if not fail:
                assert json.loads(task.result_data) == {"value": 42, "task_pk": task.pk}
            else:
                assert "qualification expected failure" in task.error_message
            assert terminal_ns >= committed["committed_ns"] >= released_ns
            return {
                "task_pk": task.pk,
                "job_id": identity[0],
                "attempt": identity[1],
                "generation": identity[2],
                "module": before["module"],
                "state": task.state,
                "job_started_ns": before["started_ns"],
                "database_times": {
                    "created_ns": int(task.created_at.timestamp() * 1e9),
                    "claimed_ns": int(task.started_at.timestamp() * 1e9),
                    "finished_ns": int(task.finished_at.timestamp() * 1e9),
                },
                "released_ns": released_ns,
                "committed_ns": committed["committed_ns"],
                "terminal_ns": terminal_ns,
                "receipt_to_terminal_seconds": (terminal_ns - committed["committed_ns"]) / 1e9,
                "release_to_terminal_seconds": (terminal_ns - released_ns) / 1e9,
            }

        # Align release after the initial slow scan; do not count cold startup as
        # receipt delay. The control uses the same binary and the real 30s clock.
        for name, count, fail in (
            ("recovery-only", 1, False),
            ("capacity-one", 3, False),
            ("failure", 1, True),
            ("api-outage", 1, False),
        ):
            print(f"latency case={name} phase=started", flush=True)
            request_offset = len(proxy.requests)
            tasks = [enqueue(fail=fail) for _ in range(count)]
            manager, ready = start_manager(name, recovery_only=name == "recovery-only")
            first_started = started(tasks[0])
            if name == "recovery-only":
                # Release just after a scan that saw the real held Job. This
                # removes startup phase jitter from the recovery-clock control.
                def observed_scan(name=name, started_ns=first_started["started_ns"]):
                    scans = sorted(root.glob(f"{name}-scan-*.json"))
                    observed = read_json(scans[-1]) if scans else None
                    if observed and observed["reconciled_ns"] >= started_ns:
                        return observed
                    return None

                ready = wait(observed_scan, managers, seconds=35)
            for pending in tasks[1:]:
                pending.refresh_from_db()
                assert pending.state == TaskState.QUEUED and pending.ray_job_id is None
            outage_ns = None
            if name == "api-outage":
                proxy.unavailable.set()
                try:
                    urllib.request.urlopen(proxy.address + "/api/version", timeout=5)
                except urllib.error.HTTPError as error:
                    assert error.code == 503
                else:
                    raise AssertionError("Jobs API outage was not effective")
                outage_ns = time.monotonic_ns()
            observations = [complete(task, fail=fail) for task in tasks]
            claim_delays = [
                (later["database_times"]["claimed_ns"] - earlier["database_times"]["finished_ns"])
                / 1e9
                for earlier, later in zip(observations, observations[1:], strict=False)
            ]
            assert all(0 <= delay < 5 for delay in claim_delays)
            if outage_ns is not None:
                assert not [request for request in proxy.requests if request["at_ns"] >= outage_ns]
                proxy.unavailable.clear()
            metrics = stop_manager(manager, name)
            if name == "recovery-only":
                assert metrics["counters"]["fast_completed"] == 0
                assert observations[0]["receipt_to_terminal_seconds"] >= 10
            else:
                assert metrics["counters"]["fast_completed"] == count
                assert all(item["receipt_to_terminal_seconds"] < 5 for item in observations)
            cases.append(
                {
                    "name": name,
                    "tasks": observations,
                    "managers": [metrics],
                    "initial_reconciliation_ns": ready["reconciled_ns"],
                    "capacity_claim_delays_seconds": claim_delays,
                    "api_requests": proxy.requests[request_offset:],
                    "passed": True,
                }
            )

        print("latency case=manager-replacement phase=started", flush=True)
        request_offset = len(proxy.requests)
        task = enqueue()
        original, _ = start_manager("original")
        started(task)
        identity = (task.ray_job_id, task.attempt_number, task.execution_generation)
        original_owner = task.claimed_by_worker
        original_metrics = stop_manager(original, "original")
        task.refresh_from_db()
        assert task.claimed_by_worker is None and task.state == TaskState.RUNNING
        replacement, ready = start_manager("replacement")
        task.refresh_from_db()
        assert task.claimed_by_worker == ready["worker_id"] != original_owner
        assert (task.ray_job_id, task.attempt_number, task.execution_generation) == identity
        observation = complete(task)
        replacement_metrics = stop_manager(replacement, "replacement")
        assert replacement_metrics["counters"]["fast_completed"] == 1
        requests = proxy.requests[request_offset:]
        assert (
            sum(r["method"] == "POST" and r["path"].rstrip("/") == "/api/jobs" for r in requests)
            == 1
        )
        cases.append(
            {
                "name": "manager-replacement",
                "tasks": [observation],
                "managers": [original_metrics, replacement_metrics],
                "initial_reconciliation_ns": ready["reconciled_ns"],
                "capacity_claim_delays_seconds": [],
                "api_requests": requests,
                "passed": True,
            }
        )
        assert not TaskWorkerLease.objects.filter(is_active=True).exists()
        assert RayTaskExecution.objects.count() == 7
        assert not RayTaskExecution.objects.exclude(
            state__in=[TaskState.SUCCEEDED, TaskState.FAILED]
        ).exists()
        from ray.job_submission import JobSubmissionClient

        client = JobSubmissionClient("http://127.0.0.1:8265")
        for case in cases:
            for item in case["tasks"]:
                wait(
                    lambda item=item: client.get_job_status(item["job_id"]).is_terminal(),
                    managers,
                    seconds=20,
                )
        connection.close()
    except Exception as error:
        import traceback

        traceback.print_exc()
        failure = type(error).__name__
    finally:
        for manager in list(managers):
            try:
                manager.stop()
                managers.remove(manager)
            except Exception:
                failure = failure or "manager-cleanup-failed"
        if proxy is not None:
            proxy.close()
        ray.shutdown()
    return {
        "schema_version": 1,
        "module": expected_module,
        "cases": cases,
        "failure": failure,
        "ray_shutdown": not ray.is_initialized(),
        "managers_stopped": not managers,
    }


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("expected fixture directory, installed module and receipt path")
    if not __debug__:
        raise SystemExit("latency qualification requires assertions enabled")
    receipt = run(Path(sys.argv[1]), str(Path(sys.argv[2]).resolve()))
    with Path(sys.argv[3]).open("x") as stream:
        json.dump(receipt, stream, allow_nan=False)
    raise SystemExit(0 if receipt["failure"] is None else 1)
