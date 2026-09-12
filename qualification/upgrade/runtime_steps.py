"""Finite producer and database observations for fresh upgrade fixture processes.

These commands use public enqueue/cancellation and inspect actual stored rows.
Their output is input to the orchestrator, not native or complete upgrade proof.
No command starts a manager, Ray, a database server, or Kubernetes resources.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 1024 * 1024
MAX_ROWS = 32
CASES = (
    "old-success",
    "old-failure",
    "old-cancel",
    "old-retry",
    "old-gated",
    "current-core",
    "current-jobs",
)
OLD_CASES = CASES[:5]
PRESERVED_PAYLOAD = "released-upgrade-input:" + "x" * 4096


class StepError(ValueError):
    """A fixed refusal; never copy database, environment or provider exceptions."""


@dataclass(frozen=True)
class RuntimeStoreArguments:
    run_digest: str | None = None
    point: str | None = None
    artifacts_sha256: str | None = None
    dump_sha256: str | None = None
    system_identifier: str | None = None
    primary_database_oid: int | None = None
    scratch_database_oid: int | None = None


STORE_ACTIONS = {
    "create-scratch": {"run_digest", "point", "system_identifier", "primary_database_oid"},
    "bind-store": {"run_digest"},
    "backup-artifacts": {"run_digest", "point"},
    "restore-artifacts": {"run_digest", "point", "artifacts_sha256"},
    "backup-database": {
        "run_digest",
        "point",
        "artifacts_sha256",
        "system_identifier",
        "primary_database_oid",
    },
    "restore-database": {field.name for field in fields(RuntimeStoreArguments)},
}


def store_cli_args(action: str, arguments: RuntimeStoreArguments) -> list[str]:
    """Validate the complete finite schema before rendering or executing a command."""
    if (
        type(action) is not str
        or action not in STORE_ACTIONS
        or type(arguments) is not RuntimeStoreArguments
    ):
        raise StepError("upgrade-store-arguments-invalid")
    required = STORE_ACTIONS[action]
    result = [action]
    for field in fields(arguments):
        value = getattr(arguments, field.name)
        if (value is not None) != (field.name in required):
            raise StepError("upgrade-store-arguments-invalid")
        if value is None:
            continue
        if field.name in {"run_digest", "artifacts_sha256", "dump_sha256"}:
            valid = type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        elif field.name == "point":
            valid = type(value) is str and value in (
                {"blocked", "final"}
                if action.startswith("backup-")
                else {"blocked", "final", "rollback"}
            )
        elif field.name == "system_identifier":
            valid = (
                type(value) is str
                and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
                and int(value) < 2**64
            )
        else:
            valid = type(value) is int and 0 < value < 2**32
        if not valid:
            raise StepError("upgrade-store-arguments-invalid")
        result.extend(["--" + field.name.replace("_", "-"), str(value)])
    if (
        arguments.scratch_database_oid is not None
        and arguments.scratch_database_oid == arguments.primary_database_oid
    ):
        raise StepError("upgrade-store-arguments-invalid")
    return result


def _store_action(action: str, arguments: RuntimeStoreArguments) -> dict:
    store_cli_args(action, arguments)
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "qualification.upgrade.runtime_settings":
        raise StepError("upgrade-step-settings-mismatch")
    _epoch("baseline", "primary")
    from qualification.upgrade import runtime_artifacts

    if action == "bind-store":
        return runtime_artifacts.bind_artifact_store(arguments.run_digest)
    if action == "backup-artifacts":
        return runtime_artifacts.backup_artifacts(arguments.point, run_digest=arguments.run_digest)
    if action == "restore-artifacts":
        return runtime_artifacts.restore_artifacts(
            arguments.point,
            run_digest=arguments.run_digest,
            expected_artifacts_sha256=arguments.artifacts_sha256,
        )
    options = {
        "run_digest": arguments.run_digest,
        "expected_artifacts_sha256": arguments.artifacts_sha256,
        "expected_system_identifier": arguments.system_identifier,
        "expected_primary_database_oid": arguments.primary_database_oid,
    }
    if action == "create-scratch":
        from qualification.upgrade.runtime_scratch import create_scratch

        return create_scratch(
            arguments.point,
            run_digest=arguments.run_digest,
            expected_system_identifier=arguments.system_identifier,
            expected_primary_database_oid=arguments.primary_database_oid,
        )
    if action == "backup-database":
        from qualification.upgrade.runtime_database import backup_database

        return backup_database(arguments.point, **options)
    from qualification.upgrade.runtime_restore import restore_database

    return restore_database(
        arguments.point,
        **options,
        expected_dump_sha256=arguments.dump_sha256,
        expected_scratch_database_oid=arguments.scratch_database_oid,
    )


def _root() -> Path:
    raw = os.environ.get("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", "")
    root = Path(raw)
    if not raw or not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise StepError("upgrade-step-root-unavailable")
    return root


def _json(value) -> bytes:
    from django.core.serializers.json import DjangoJSONEncoder

    class ExactTimeEncoder(DjangoJSONEncoder):
        def default(self, o):
            # Django's normal wire encoder discards sub-millisecond precision.
            # Preservation evidence must retain every stored timestamp digit.
            if isinstance(o, datetime):
                return o.isoformat(timespec="microseconds")
            return super().default(o)

    raw = json.dumps(
        value, cls=ExactTimeEncoder, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(raw) > MAX_BYTES:
        raise StepError("upgrade-step-record-too-large")
    return raw


def _write_once(path: Path, value) -> None:
    raw = _json(value)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise StepError("upgrade-step-already-started") from None


def _read(path: Path):
    if path.is_symlink() or not path.is_file():
        raise StepError("upgrade-step-record-unavailable")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise StepError("upgrade-step-record-too-large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StepError("invalid-upgrade-record")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise StepError("invalid-upgrade-record")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, ValueError):
        raise StepError("invalid-upgrade-record") from None


def _directory(name: str) -> Path:
    if name not in {"inputs", "results", "runtime-effects", "observations"}:
        raise StepError("unsupported-upgrade-directory")
    directory = _root() / name
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise StepError("upgrade-step-directory-unavailable")
    return directory


def prepare() -> dict:
    """Prepare only the already mounted fixture's empty artifact directory."""
    root = _root()
    if any(root.iterdir()):
        raise StepError("upgrade-step-root-not-empty")
    for name in ("inputs", "results", "runtime-effects", "observations"):
        (root / name).mkdir(mode=0o700)
    return {"artifact_directories_prepared": True}


def _case_file(case: str) -> Path:
    if case not in CASES:
        raise StepError("unsupported-upgrade-case")
    return _directory("observations") / (case + ".json")


def _task(case: str):
    from django_ray.models import RayTaskExecution

    record = _read(_case_file(case))
    if type(record) is not dict or set(record) != {"case", "task_pk", "task_id", "callable_path"}:
        raise StepError("invalid-upgrade-case-record")
    if record["case"] != case or type(record["task_pk"]) is not int:
        raise StepError("invalid-upgrade-case-record")
    row = RayTaskExecution.objects.get(pk=record["task_pk"])
    if row.task_id != record["task_id"] or row.callable_path != record["callable_path"]:
        raise StepError("upgrade-task-identity-changed")
    expected_queue = "upgrade-core" if case == "current-core" else "upgrade-jobs"
    if row.queue_name != expected_queue:
        raise StepError("upgrade-task-queue-changed")
    return row


def enqueue(case: str) -> dict:
    import django_ray
    from django_ray.models import RayTaskExecution
    from qualification.upgrade import runtime_tasks

    path = _case_file(case)
    expected = "0.4.0" if case in OLD_CASES else "0.5.0"
    if django_ray.__version__ != expected:
        raise StepError("upgrade-enqueue-build-mismatch")
    if RayTaskExecution.objects.count() >= MAX_ROWS:
        raise StepError("upgrade-task-count-exceeded")
    # Reserve the one allowed producer call before touching the database. A lost
    # enqueue response or failed record write must never turn into a second call.
    _write_once(path.with_suffix(".reserved.json"), {"case": case})
    if case in {"old-success", "current-core"}:
        selected, args = runtime_tasks.value, (case, PRESERVED_PAYLOAD)
    elif case == "old-failure":
        selected, args = runtime_tasks.failed, ()
    elif case == "old-retry":
        selected, args = runtime_tasks.retried, ()
    else:
        selected, args = runtime_tasks.gated_effect, (case,)
    core = case == "current-core"
    configured = selected.using(
        backend="default" if core else "jobs",
        queue_name="upgrade-core" if core else "upgrade-jobs",
    )
    result = configured.enqueue(*args)
    row = RayTaskExecution.objects.get(task_id=result.id)
    if row.callable_path != configured.module_path or row.queue_name != configured.queue_name:
        raise StepError("upgrade-enqueue-observation-mismatch")
    record = {
        "case": case,
        "task_pk": row.pk,
        "task_id": row.task_id,
        "callable_path": row.callable_path,
    }
    _write_once(path, record)
    return record


def inspect(case: str) -> dict:
    row = _task(case)
    fields = (
        "task_id",
        "state",
        "attempt_number",
        "execution_generation",
        "execution_protocol_version",
        "claimed_by_worker",
        "started_at",
        "finished_at",
        "ray_job_id",
        "created_with_django_ray_version",
        "managed_with_django_ray_version",
    )
    available = {field.attname for field in row._meta.concrete_fields}
    absent = [name for name in fields if name not in available]
    result = {name: getattr(row, name) for name in fields if name in available}
    # Released 0.4.0 has no protocol/package stamp fields. Absence is an
    # observation of that schema, never an inferred version or protocol.
    result["absent_fields"] = absent
    result.update(
        case=case,
        task_pk=row.pk,
        input_referenced=row.input_reference is not None,
        result_referenced=row.result_reference is not None,
    )
    attempts = list(row.attempts.order_by("attempt_number").values("attempt_number", "state")[:3])
    if len(attempts) > 2:
        raise StepError("upgrade-attempt-count-exceeded")
    result["attempts"] = attempts
    return result


def cancel(case: str) -> dict:
    from django_ray.lifecycle import request_task_cancellation

    if case != "old-cancel":
        raise StepError("unsupported-upgrade-cancellation")
    row = _task(case)
    _started(row, case)
    requested = request_task_cancellation(
        row.pk,
        expected_attempt_number=row.attempt_number,
        expected_execution_generation=row.execution_generation,
    )
    if not requested.accepted:
        raise StepError("upgrade-cancellation-not-accepted")
    observed = inspect(case)
    observed["cancellation_requested"] = True
    observed["cancellation_request_status"] = str(requested.status)
    return observed


def _started(row, case: str) -> dict:
    marker = _read(_directory("runtime-effects") / f"{case}.{row.attempt_number}.started.json")
    identity = marker.get("identity", {}) if type(marker) is dict else {}
    if (
        type(marker) is not dict
        or set(marker) != {"schema", "case", "phase", "observed_at", "identity"}
        or type(marker.get("schema")) is not int
        or marker["schema"] != 1
        or marker["case"] != case
        or marker["phase"] != "started"
        or type(identity) is not dict
        or re.fullmatch(r"[0-9a-f]{8}", str(identity.get("native_job_id", ""))) is None
        or identity.get("package_version") != ("0.4.0" if case in OLD_CASES else "0.5.0")
        or identity.get("ray_version") != ("2.56.0" if case in OLD_CASES else "2.58.0")
        or identity.get("python") != platform.python_version()
        or identity.get("implementation") != "cpython"
        or identity.get("context_protocol") != (None if case in OLD_CASES else 3)
        or row.state != "RUNNING"
        or row.attempt_number != 1
        or identity.get("task_pk") != row.pk
        or identity.get("task_id") != row.task_id
        or identity.get("generation") != row.execution_generation
        or identity.get("attempt") != row.attempt_number
    ):
        raise StepError("upgrade-effect-identity-mismatch")
    return marker


def release(case: str) -> dict:
    if case not in {"old-gated", "current-jobs"}:
        raise StepError("unsupported-upgrade-release")
    row = _task(case)
    _started(row, case)
    with (_directory("runtime-effects") / f"{case}.release").open("xb") as stream:
        stream.write(b"release\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {"case": case, "task_id": row.task_id, "gate_released": True}


def _rows_snapshot(expected=None) -> dict:
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskInputPayload

    rows = [_task(case) for case in OLD_CASES]
    task_pks = [row.pk for row in rows]
    result = {}
    for model in (RayTaskExecution, TaskAttempt, TaskInputPayload):
        name = model.__name__
        fields = (
            [field.attname for field in model._meta.concrete_fields]
            if expected is None
            else expected[name]["fields"]
        )
        available = {field.attname for field in model._meta.concrete_fields}
        if (
            type(fields) is not list
            or not fields
            or any(type(field) is not str or field not in available for field in fields)
            or len(set(fields)) != len(fields)
            or model._meta.pk.attname not in fields
        ):
            raise StepError("upgrade-history-fields-invalid")
        query = model.objects.order_by(model._meta.pk.attname)
        if model is RayTaskExecution:
            query = query.filter(pk__in=task_pks)
        elif model is TaskAttempt:
            query = query.filter(execution_id__in=task_pks)
        elif expected is not None:
            query = query.filter(
                pk__in=[row[model._meta.pk.attname] for row in expected[name]["rows"]]
            )
        values = list(query.values(*fields)[: MAX_ROWS + 1])
        if len(values) > MAX_ROWS:
            raise StepError("upgrade-history-count-exceeded")
        result[name] = {"fields": fields, "rows": values}
    return json.loads(_json(result))


def history(*, compare=False) -> dict:
    rows = [_task(case) for case in OLD_CASES]
    expected_states = ["SUCCEEDED", "FAILED", "CANCELLED", "SUCCEEDED", "SUCCEEDED"]
    if [row.state for row in rows] != expected_states or rows[3].attempt_number != 2:
        raise StepError("upgrade-history-not-settled")
    path = _directory("observations") / "historical-rows.json"
    expected = _read(path) if compare else None
    actual = _rows_snapshot(expected)
    if compare:
        if actual != expected:
            raise StepError("upgrade-historical-rows-changed")
    else:
        _write_once(path, actual)
    return {"historical_rows_sha256": hashlib.sha256(_json(actual)).hexdigest()}


def _epoch(build: str, database: str) -> None:
    import django_ray

    if (
        os.environ.get("DJANGO_RAY_UPGRADE_BUILD") != build
        or os.environ.get("DJANGO_RAY_UPGRADE_DATABASE") != database
        or django_ray.__version__ != ("0.4.0" if build == "baseline" else "0.5.0")
    ):
        raise StepError("upgrade-step-epoch-mismatch")


def blocked_history() -> dict:
    # Observe the restored clone through the released wheel. The original
    # manager may still heartbeat on primary; sampling there before pg_dump
    # would give this comparison a different database snapshot.
    _epoch("baseline", "scratch")
    rows = [_task(case) for case in OLD_CASES]
    if [row.state for row in rows] != ["QUEUED", "FAILED", "CANCELLED", "SUCCEEDED", "RUNNING"]:
        raise StepError("upgrade-blocked-history-not-active")
    if rows[3].attempt_number != 2:
        raise StepError("upgrade-retry-not-observed")
    _started(rows[4], "old-gated")
    snapshot = _rows_snapshot()
    _write_once(_directory("observations") / "blocked-rows.json", snapshot)
    return {"original_rows_sha256": hashlib.sha256(_json(snapshot)).hexdigest()}


def blocked_migrate() -> dict:
    """Attempt actual activation only in the independently restored scratch DB."""
    _epoch("candidate", "scratch")
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    from django_ray.models import LegacyWorkerAdmissionToken, TaskExecutionProtocolPolicy

    expected = _read(_directory("observations") / "blocked-rows.json")
    # Current models cannot query the old schema before its additive migrations.
    call_command("migrate", "django_ray", "0034_cohort_timeouts", verbosity=0, interactive=False)
    before = _rows_snapshot(expected)
    if before != expected:
        raise StepError("upgrade-blocked-clone-history-mismatch")
    policy = TaskExecutionProtocolPolicy.objects.values().get()
    tokens = list(LegacyWorkerAdmissionToken.objects.values_list("singleton_key", flat=True))
    if policy["active_write_protocol_version"] != 1 or tokens != [1]:
        raise StepError("upgrade-blocked-clone-policy-mismatch")
    try:
        call_command(
            "migrate", "django_ray", "0035_activate_current_cohort", verbosity=0, interactive=False
        )
    except RuntimeError as error:
        if str(error) != "Current-cohort activation refuses unsupported nonterminal work":
            raise StepError("upgrade-activation-refusal-mismatch") from None
    else:
        raise StepError("upgrade-blocked-activation-was-accepted")
    recorded = (
        MigrationRecorder(connection)
        .migration_qs.filter(app="django_ray", name="0035_activate_current_cohort")
        .exists()
    )
    if (
        recorded
        or _rows_snapshot(expected) != before
        or TaskExecutionProtocolPolicy.objects.values().get() != policy
        or list(LegacyWorkerAdmissionToken.objects.values_list("singleton_key", flat=True))
        != tokens
    ):
        raise StepError("upgrade-refusal-changed-history")
    digest = hashlib.sha256(_json(before)).hexdigest()
    return {
        "activation_refused": True,
        "activation_recorded": False,
        "original_rows_before_sha256": digest,
        "original_rows_after_sha256": digest,
        "policy_protocol": 1,
        "legacy_token_present": True,
    }


def migrate() -> dict:
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    build = os.environ.get("DJANGO_RAY_UPGRADE_BUILD")
    if build not in {"baseline", "candidate"}:
        raise StepError("upgrade-step-epoch-mismatch")
    _epoch(build, "primary")
    call_command("migrate", verbosity=0, interactive=False)
    leaf = "0018_workflow_run_allocation" if build == "baseline" else "0035_activate_current_cohort"
    if not MigrationRecorder(connection).migration_qs.filter(app="django_ray", name=leaf).exists():
        raise StepError("upgrade-migration-not-recorded")
    observations = {"migration_leaf": leaf}
    if build == "candidate":
        from django_ray.models import LegacyWorkerAdmissionToken, TaskExecutionProtocolPolicy

        policy = TaskExecutionProtocolPolicy.objects.get()
        if (
            policy.active_write_protocol_version != 3
            or policy.legacy_worker_admission_enabled
            or LegacyWorkerAdmissionToken.objects.exists()
        ):
            raise StepError("upgrade-activation-policy-mismatch")
        observations.update(protocol=3, legacy_admission_open=False, **history(compare=True))
    return observations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "enqueue",
            "inspect",
            "cancel",
            "release",
            "history",
            "compare-history",
            "migrate",
            "blocked-history",
            "blocked-migrate",
            "read-history",
            *STORE_ACTIONS,
        ),
    )
    parser.add_argument("--case", choices=CASES)
    for field in fields(RuntimeStoreArguments):
        parser.add_argument(
            "--" + field.name.replace("_", "-"),
            type=int if field.name.endswith("_oid") else str,
        )
    args = parser.parse_args(argv)
    try:
        store = RuntimeStoreArguments(
            **{field.name: getattr(args, field.name) for field in fields(RuntimeStoreArguments)}
        )
        if args.action not in STORE_ACTIONS and store != RuntimeStoreArguments():
            raise StepError("upgrade-store-arguments-invalid")
        if (args.action in {"enqueue", "inspect", "cancel", "release"}) != (args.case is not None):
            raise StepError("upgrade-step-case-required")
        if args.action in STORE_ACTIONS:
            observations = _store_action(args.action, store)
        elif args.action == "prepare":
            observations = prepare()
        else:
            expected_settings = (
                "qualification.upgrade.runtime_history_settings"
                if args.action == "read-history"
                else "qualification.upgrade.runtime_settings"
            )
            if os.environ.get("DJANGO_SETTINGS_MODULE") != expected_settings:
                raise StepError("upgrade-step-settings-mismatch")
            import django

            django.setup()
            if args.action == "read-history":
                from qualification.upgrade.runtime_history import observe_runtime_history

                observations = observe_runtime_history()
            elif args.action in {"history", "compare-history"}:
                observations = history(compare=args.action == "compare-history")
            elif args.action in {"migrate", "blocked-history", "blocked-migrate"}:
                observations = {
                    "migrate": migrate,
                    "blocked-history": blocked_history,
                    "blocked-migrate": blocked_migrate,
                }[args.action]()
            else:
                observations = {
                    "enqueue": enqueue,
                    "inspect": inspect,
                    "cancel": cancel,
                    "release": release,
                }[args.action](args.case)
        result = {
            "schema": 1,
            "action": args.action,
            "pid": os.getpid(),
            "python": platform.python_version(),
            "observed_at": datetime.now(UTC).isoformat(),
            "observations": observations,
            "complete_upgrade_gate": False,
        }
        print(_json(result).decode(), flush=True)
        return 0
    except Exception:
        print('{"step_failed":true,"complete_upgrade_gate":false}', flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
