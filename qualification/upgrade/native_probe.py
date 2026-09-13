"""A fresh installed-version process; no synthetic terminal task transitions."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}
PAYLOAD = "native-upgrade:" + "x" * 2048


def snapshot(fields=None, identities=None):
    from django.core.serializers.json import DjangoJSONEncoder

    from django_ray.models import RayTaskExecution, TaskAttempt

    result = {}
    for model in (RayTaskExecution, TaskAttempt):
        names = fields[model.__name__] if fields else [f.attname for f in model._meta.fields]
        query = model.objects.order_by("pk")
        if identities is not None:
            key = "task_id" if model is RayTaskExecution else "execution__task_id"
            query = query.filter(**{key + "__in": identities})
        result[model.__name__] = {"fields": names, "rows": list(query.values(*names))}
    return json.loads(json.dumps(result, cls=DjangoJSONEncoder))


def read_history(root, history_file="history.json"):
    from django.tasks import task_backends

    from django_ray.input_storage import load_task_input
    from django_ray.models import RayTaskExecution
    from django_ray.result_storage import load_result_reference

    expected = json.loads((root / history_file).read_text())
    ids = [r["task_id"] for r in expected["RayTaskExecution"]["rows"]]
    fields = {name: value["fields"] for name, value in expected.items()}
    assert snapshot(fields, ids) == expected, "historical rows changed"
    for identity in ids:
        row = RayTaskExecution.objects.get(task_id=identity)
        assert row.input_reference
        args, kwargs = load_task_input(
            args_json=row.args_json,
            kwargs_json=row.kwargs_json,
            input_reference=row.input_reference,
        )
        assert args[1] == PAYLOAD and kwargs == {}
        result = task_backends["default"].get_result(identity)
        assert result.id == identity
        if row.state == "SUCCEEDED":
            assert row.result_reference
            assert json.loads(load_result_reference(row.result_reference)) == {
                "value": 42,
                "payload": PAYLOAD,
            }
    return len(ids)


def run_work(root):
    import ray
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connections

    from django_ray.lifecycle import request_task_cancellation, retry_task
    from django_ray.models import RayTaskExecution, TaskWorkerLease
    from qualification.upgrade.native_tasks import controlled

    for name in (
        "retry-again",
        *(
            f"{prefix}-{case}"
            for prefix in ("started", "release")
            for case in ("success", "failure", "retry", "cancelled")
        ),
    ):
        (root / name).unlink(missing_ok=True)
    call_command("migrate", verbosity=0)
    tasks = {
        case: controlled.enqueue(case, PAYLOAD)
        for case in ("success", "failure", "retry", "cancelled")
    }
    ids = [task.id for task in tasks.values()]
    # The inventory is deliberately read-only. Actual old managers, not a fixture
    # update, must settle these rows before backup can proceed.
    before = snapshot()
    assert RayTaskExecution.objects.filter(task_id__in=ids, state="QUEUED").count() == 4
    assert snapshot() == before
    cancelled = RayTaskExecution.objects.get(task_id=tasks["cancelled"].id)
    outcome = request_task_cancellation(
        cancelled.pk,
        expected_attempt_number=cancelled.attempt_number,
        expected_execution_generation=cancelled.execution_generation,
    )
    assert outcome.state == "CANCELLED"
    manager = None
    log = (root / "manager.log").open("wb")
    try:
        ray.init(
            address="local",
            num_cpus=1,
            num_gpus=0,
            object_store_memory=128 * 1024 * 1024,
            include_dashboard=settings.RUNNER == "ray_job",
        )
        manager = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "django",
                "django_ray_worker",
                *(["--cluster", "auto"] if settings.RUNNER == "ray_core" else []),
                "--concurrency",
                "1",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )

        def wait(predicate):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if manager.poll() is not None:
                    raise RuntimeError("manager exited during work")
                if value := predicate():
                    return value
                time.sleep(0.1)
            raise TimeoutError("native upgrade observation timed out")

        wait(lambda: (root / "started-success").exists())
        before = snapshot()
        assert RayTaskExecution.objects.get(task_id=tasks["success"].id).state == "RUNNING"
        assert snapshot() == before
        for case in ("success", "failure", "retry"):
            (root / f"release-{case}").touch()
        wait(
            lambda: RayTaskExecution.objects.filter(
                task_id=tasks["retry"].id, state="FAILED"
            ).exists()
        )
        retry = RayTaskExecution.objects.get(task_id=tasks["retry"].id)
        (root / "retry-again").touch()
        assert (
            retry_task(
                retry,
                expected_attempt_number=retry.attempt_number,
                expected_execution_generation=retry.execution_generation,
            )
            is not None
        )
        wait(
            lambda: (
                RayTaskExecution.objects.filter(task_id__in=ids, state__in=TERMINAL).count() == 4
            )
        )
        states = {
            case: RayTaskExecution.objects.get(task_id=task.id).state
            for case, task in tasks.items()
        }
        assert states == {
            "success": "SUCCEEDED",
            "failure": "FAILED",
            "retry": "SUCCEEDED",
            "cancelled": "CANCELLED",
        }
        assert not (root / "started-cancelled").exists()
        if settings.RUNNER == "ray_job":
            for case in ("success", "failure", "retry"):
                assert RayTaskExecution.objects.get(task_id=tasks[case].id).ray_job_id
    finally:
        try:
            if manager is not None:
                stop_signal = signal.SIGINT if settings.RUNNER == "ray_core" else signal.SIGTERM
                if manager.poll() is None:
                    manager.send_signal(stop_signal)
                try:
                    manager.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    manager.kill()
                    manager.wait(timeout=5)
                    raise
                # Native Ray replaces the SIGTERM handler. SIGINT retains the
                # manager's graceful drain/shutdown handler on both versions.
                assert manager.returncode == 128 + stop_signal, manager.returncode
        finally:
            import psutil

            children = psutil.Process().children(recursive=True)
            # Ray 2.56 has no wait_for_processes argument. Observe the owned
            # descendants explicitly so old Ray cannot overlap the next phase.
            ray.shutdown()
            _, alive = psutil.wait_procs(children, timeout=15)
            assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]
            log.close()
    assert not TaskWorkerLease.objects.filter(is_active=True).exists()
    assert not RayTaskExecution.objects.exclude(state__in=TERMINAL).exists()
    connections.close_all()
    return {
        "states": states,
        "queued_blockers": 4,
        "running_blocker": True,
        "manager_exit": manager.returncode,
        "active_leases": 0,
        "nonterminal": 0,
        "runner": settings.RUNNER,
    }


def main():
    if len(sys.argv) != 4 or not __debug__:
        raise SystemExit("expected phase, installed module and receipt")
    phase, expected_module, receipt = sys.argv[1:]
    if phase not in {
        "baseline-run",
        "baseline-read",
        "candidate-read",
        "candidate-run",
        "baseline-post-write",
    }:
        raise SystemExit("invalid native upgrade phase")
    import django_ray

    assert str(Path(django_ray.__file__).resolve()) == expected_module
    assert django_ray.__version__ == ("0.5.0" if phase.startswith("candidate") else "0.4.0")
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    root = settings.ROOT
    observations = {}
    if phase == "baseline-post-write":
        from django.db import connection
        from django.db.migrations.recorder import MigrationRecorder

        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute("PRAGMA query_only=ON")
                cursor.execute("PRAGMA query_only")
                assert cursor.fetchone() == (1,)
            else:
                cursor.execute("SET default_transaction_read_only=on")
                cursor.execute("SHOW default_transaction_read_only")
                assert cursor.fetchone() == ("on",)
        assert (
            MigrationRecorder.Migration.objects.filter(
                app="django_ray", name__startswith="0026_"
            ).count()
            == 1
        )
        observations["post_write_tasks_read"] = read_history(root, "candidate-history.json")
        assert observations["post_write_tasks_read"] == 8
        observations["database_read_only"] = True
        observations["migrations_retained"] = True
    if phase == "candidate-read":
        call_command("migrate", verbosity=0)
    if phase != "baseline-run":
        observations["historical_tasks"] = read_history(root)
    if phase.endswith("-run"):
        observations.update(run_work(root))
        if phase == "baseline-run":
            (root / "history.json").write_text(json.dumps(snapshot()))
            observations["historical_tasks"] = read_history(root)
        else:
            original = json.loads((root / "history.json").read_text())
            fields = {name: value["fields"] for name, value in original.items()}
            (root / "candidate-history.json").write_text(json.dumps(snapshot(fields)))
    Path(receipt).write_text(
        json.dumps(
            {
                "phase": phase,
                "version": django_ray.__version__,
                "module": expected_module,
                "observations": observations,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        from django.conf import settings

        log = settings.ROOT / "manager.log"
        if log.is_file():
            print(log.read_text(errors="replace")[-65536:], flush=True)
        raise
