"""One fresh Django process using either the released or the candidate wheel."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

from qualification.upgrade.contract import BASELINE_VERSION, CANDIDATE_VERSION, PHASES

FIXTURE_IDS = (
    "upgrade-success",
    "upgrade-failed",
    "upgrade-cancelled",
    "upgrade-retried",
    "upgrade-external",
    "upgrade-job",
    "upgrade-queued",
    "upgrade-uncertain",
)
RELEASED_INPUT = "released-input:" + "x" * 2048


def _json(value):
    from django.core.serializers.json import DjangoJSONEncoder

    return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"))


def _models():
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskInputPayload, TaskWorkerLease

    return (RayTaskExecution, TaskAttempt, TaskInputPayload, TaskWorkerLease)


def _snapshot(fields=None):
    result = {}
    for model in _models():
        names = (
            [field.attname for field in model._meta.concrete_fields]
            if fields is None
            else fields[model.__name__]
        )
        query = model.objects.order_by(model._meta.pk.attname)
        if model.__name__ == "RayTaskExecution":
            query = query.filter(task_id__in=FIXTURE_IDS)
        result[model.__name__] = {"fields": names, "rows": list(query.values(*names))}
    # Normalize datetime and UUID instances identically across isolated interpreters.
    return json.loads(_json(result))


def _seed(root, artifacts):
    from django.core.management import call_command
    from django.db import transaction
    from django.utils import timezone

    from django_ray.input_storage import prepare_task_input, register_task_input
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
    from django_ray.result_storage import FilesystemResultStorage

    call_command("migrate", verbosity=0)
    assert RayTaskExecution.objects.count() == 0
    with zipfile.ZipFile(artifacts / "runtime.zip", "x") as archive:
        archive.writestr("released-marker.txt", "released-upgrade-fixture")
    reference = FilesystemResultStorage(artifacts / "results").store(
        serialized_result='{"preserved":42}'
    )
    prepared = prepare_task_input([RELEASED_INPUT], {"preserve": True})
    assert prepared.input_reference is not None
    now = timezone.now()
    states = (
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED,
        TaskState.QUEUED,
        TaskState.RUNNING,
    )
    with transaction.atomic():
        register_task_input(prepared)
        for identity, state in zip(FIXTURE_IDS, states, strict=True):
            row = RayTaskExecution.objects.create(
                task_id=identity,
                callable_path="retired_upgrade_application.task",
                queue_name="removed-upgrade-queue",
                state=state,
                attempt_number=2 if identity == "upgrade-retried" else 1,
                execution_generation=1,
                args_json=prepared.args_json if identity == "upgrade-external" else "[42]",
                kwargs_json=prepared.kwargs_json if identity == "upgrade-external" else "{}",
                input_reference=prepared.input_reference
                if identity == "upgrade-external"
                else None,
                result_data="42" if state == TaskState.SUCCEEDED else None,
                result_reference=reference if identity == "upgrade-external" else None,
                ray_job_id="released-job-fixture" if identity == "upgrade-job" else None,
                claimed_by_worker="released-upgrade-worker" if state == TaskState.RUNNING else None,
                started_at=now if state != TaskState.QUEUED else None,
                finished_at=now if state not in (TaskState.QUEUED, TaskState.RUNNING) else None,
                error_message="released failure" if state == TaskState.FAILED else None,
                error_traceback="released traceback\n  preserved line"
                if state == TaskState.FAILED
                else None,
                progress_data=_json({"schema_version": 1, "fixture": "released-history"}),
                runtime_env_json=_json(
                    {"working_dir": "runtime.zip", "env_vars": {"UPGRADE": "released"}}
                ),
            )
            if identity == "upgrade-external":
                row.result_data = None
                row.save(update_fields=["result_data"])
            if state not in (TaskState.QUEUED, TaskState.RUNNING):
                TaskAttempt.objects.create(
                    execution=row,
                    attempt_number=row.attempt_number,
                    state=state,
                    result_data=row.result_data,
                    result_reference=row.result_reference,
                    finished_at=now,
                    error_message=row.error_message,
                )
            if identity == "upgrade-retried":
                TaskAttempt.objects.create(
                    execution=row,
                    attempt_number=1,
                    state=TaskState.FAILED,
                    finished_at=now,
                    error_message="released first-attempt failure",
                )
        TaskWorkerLease.objects.create(
            worker_id="released-upgrade-worker",
            hostname="synthetic-fixture",
            pid=1,
            queue_name="removed-upgrade-queue",
            is_active=True,
        )
    assert RayTaskExecution.objects.count() == len(FIXTURE_IDS)
    (root / "seed-snapshot.json").write_text(_json(_snapshot()), encoding="utf-8")
    return {"tasks": 8, "fixture_kind": "synthetic-released-models"}


def _blocked(root):
    from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease

    before = _snapshot()
    blockers = list(
        RayTaskExecution.objects.filter(
            state__in=[TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING],
        )
        .order_by("task_id")
        .values_list("task_id", flat=True)
    )
    assert blockers == ["upgrade-queued", "upgrade-uncertain"]
    assert TaskWorkerLease.objects.filter(is_active=True).count() == 1
    assert before == json.loads((root / "seed-snapshot.json").read_text(encoding="utf-8"))
    return {"blocked_tasks": 2, "active_leases": 1, "read_only": True}


def _settle(root):
    from django.utils import timezone

    from django_ray.lifecycle import record_lost, request_task_cancellation
    from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease

    queued = RayTaskExecution.objects.get(task_id="upgrade-queued")
    outcome = request_task_cancellation(
        queued.pk,
        expected_attempt_number=1,
        expected_execution_generation=1,
    )
    assert outcome.state == TaskState.CANCELLED
    uncertain = RayTaskExecution.objects.get(task_id="upgrade-uncertain")
    # This is an explicit synthetic-fixture disposition, not evidence of remote
    # quiescence or permission to repeat an unknown external effect.
    assert record_lost(
        uncertain,
        error_message="Synthetic fixture: no external work was submitted",
        expected_attempt_number=1,
        expected_execution_generation=1,
    )
    TaskWorkerLease.objects.filter(worker_id="released-upgrade-worker").update(
        is_active=False,
        stopped_at=timezone.now(),
    )
    assert not RayTaskExecution.objects.filter(
        state__in=[TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING],
    ).exists()
    snapshot = _snapshot()
    (root / "history.json").write_text(_json(snapshot), encoding="utf-8")
    return {"nonterminal_tasks": 0, "active_leases": 0, "settlement": "synthetic-only"}


def _historical(root, artifacts, *, inert):
    from django.tasks import task_backends

    from django_ray.input_storage import load_task_input
    from django_ray.models import RayTaskExecution
    from django_ray.result_storage import ResultStorageError, load_result_reference

    expected = json.loads((root / "history.json").read_text(encoding="utf-8"))
    fields = {name: value["fields"] for name, value in expected.items()}
    assert _snapshot(fields) == expected, "historical rows changed"
    external = RayTaskExecution.objects.get(task_id="upgrade-external")
    assert load_task_input(
        args_json=external.args_json,
        kwargs_json=external.kwargs_json,
        input_reference=external.input_reference,
    ) == ([RELEASED_INPUT], {"preserve": True})
    assert json.loads(load_result_reference(external.result_reference)) == {"preserved": 42}
    refused = 0
    if inert:
        module = root / "retired_upgrade_application.py"
        module.write_text("raise RuntimeError('historical read imported removed application')\n")
        sys.path.insert(0, str(root))
        for identity in FIXTURE_IDS:
            result = task_backends["default"].get_result(identity)
            assert result.id == identity
            assert result.task.module_path == "retired_upgrade_application.task"
            try:
                result.task.enqueue()
            except TypeError as error:
                assert "read-only" in str(error)
                refused += 1
            else:
                raise AssertionError("historical task became executable")
        assert "retired_upgrade_application" not in sys.modules
        assert refused == len(FIXTURE_IDS)
        assert _snapshot(fields) == expected
    observations = {
        "historical_sha256": hashlib.sha256(_json(expected).encode()).hexdigest(),
        "tasks": len(FIXTURE_IDS),
        "inert_execution_refusals": refused,
        "input_and_result_artifacts_read": True,
    }
    if inert:
        result_files = list((artifacts / "results").rglob("*.json"))
        assert len(result_files) == 1
        path = result_files[0]
        original = path.read_bytes()
        assert len(original) < 65536 and not path.is_symlink()
        try:
            for damaged in (None, b"corrupt"):
                if damaged is None:
                    path.unlink()
                else:
                    path.write_bytes(damaged)
                try:
                    load_result_reference(external.result_reference)
                except ResultStorageError:
                    pass
                else:
                    raise AssertionError("missing or corrupt result was accepted")
        finally:
            path.write_bytes(original)
        observations["missing_and_corrupt_result_rejected"] = True
    return observations


def main():
    if len(sys.argv) != 7 or sys.argv[4] not in PHASES or not __debug__:
        raise SystemExit(
            "expected fixture, backend, database, phase, installed module and artifacts"
        )
    root, backend, database, phase, module, artifacts_arg = sys.argv[1:]
    root, artifacts = Path(root), Path(artifacts_arg)
    assert root.is_dir() and not root.is_symlink() and (root / "owned-fixture").is_file()
    assert backend in ("sqlite", "postgresql") and database in ("baseline", "restored", "rollback")
    assert artifacts.is_dir() and artifacts.is_relative_to(root)
    import django_ray

    assert str(Path(django_ray.__file__).resolve()) == module
    if phase.startswith("baseline-") or phase in ("restored-baseline-read", "backup-rollback-read"):
        assert django_ray.__version__ == BASELINE_VERSION
    else:
        assert django_ray.__version__ == CANDIDATE_VERSION
    from django.conf import settings

    config = (
        {"ENGINE": "django.db.backends.sqlite3", "NAME": str(root / f"{database}.sqlite3")}
        if backend == "sqlite"
        else {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database,
            "USER": "qualification",
            "HOST": str(root / "socket"),
            "PORT": "5432",
            "OPTIONS": {"connect_timeout": 5, "options": "-c statement_timeout=10000"},
        }
    )
    settings.configure(
        SECRET_KEY="disposable-upgrade-fixture",
        USE_TZ=True,
        INSTALLED_APPS=["django_ray"],
        DATABASES={"default": config},
        TASKS={"default": {"BACKEND": "django_ray.backends.RayTaskBackend"}},
        DJANGO_RAY={
            "RAY_ADDRESS": "auto",
            "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(artifacts / "inputs"),
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(artifacts / "results"),
        },
    )
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connections

    if phase == "baseline-seed":
        observations = _seed(root, artifacts)
    elif phase == "baseline-blocked":
        observations = _blocked(root)
    elif phase == "baseline-settle-fixture":
        observations = _settle(root)
    elif phase == "candidate-new-write":
        from django_ray.models import RayTaskExecution
        from qualification.upgrade.tasks import current_task

        result = current_task.enqueue(7)
        assert RayTaskExecution.objects.count() == len(FIXTURE_IDS) + 1
        assert result.task is current_task
        observations = {"current_enqueue": True, "candidate_only_rows": 1}
    else:
        if phase == "candidate-migrate-read":
            call_command("migrate", verbosity=0)
        observations = _historical(root, artifacts, inert=phase == "candidate-migrate-read")
        if phase == "backup-rollback-read":
            from django_ray.models import RayTaskExecution

            assert RayTaskExecution.objects.count() == len(FIXTURE_IDS)
            observations["candidate_writes_absent_from_old_backup"] = True
    connections.close_all()
    print(
        _json(
            {
                "phase": phase,
                "status": "passed",
                "pid": os.getpid(),
                "module": module,
                "version": django_ray.__version__,
                "observations": observations,
            }
        )
    )


if __name__ == "__main__":
    main()
