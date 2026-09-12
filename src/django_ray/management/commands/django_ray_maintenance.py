"""Explicit revision-checked admission controls for a trusted operator shell."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.execution_codec import ExecutionIdentity
from django_ray.maintenance import (
    MaintenanceAdmissionError,
    MaintenancePolicySnapshot,
    MaintenanceReason,
    MaintenanceScope,
    _control_identity,
    _execution_identity,
    _scopes,
    read_maintenance_policy,
    replace_maintenance_policy,
    request_worker_retirement,
    set_task_quarantine,
)
from django_ray.models import (
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity

_MAX_OUTPUT_BYTES = 262144
_WORKER_FIELDS = ("worker_id", "worker_hostname", "worker_pid", "worker_started_at")
_TASK_FIELDS = ("task_pk", "task_id", "attempt", "generation")
_ENTITY_ACTIONS = ("retire_worker", "quarantine_task", "release_task")


def _invalid(message: str) -> None:
    raise CommandError(message)


def _entity_requested(options):
    return any(options[name] is not None for name in (*_WORKER_FIELDS, *_TASK_FIELDS)) or any(
        options[name] for name in _ENTITY_ACTIONS
    )


def _entity_report(options):
    """Read or mutate one exact identity; cleanup proof is never supplied by CLI."""
    worker = any(options[name] is not None for name in _WORKER_FIELDS)
    task = any(options[name] is not None for name in _TASK_FIELDS)
    if worker == task:
        _invalid("Select exactly one complete worker incarnation or task generation.")
    fields = _WORKER_FIELDS if worker else _TASK_FIELDS
    if any(options[name] is None for name in fields):
        _invalid("All exact identity fields are required.")
    if options["all_scopes"] or any(
        options[name] is not None
        for name in (
            "queue",
            "protocol",
            "target",
            "enqueue_change",
            "claim_change",
        )
    ):
        _invalid("Entity controls cannot be combined with admission scope controls.")
    if any(type(options[name]) is not bool for name in _ENTITY_ACTIONS):
        _invalid("Entity action flags must be explicit booleans.")
    actions = [name for name in _ENTITY_ACTIONS if options[name]]
    changing = options["apply"] or options["dry_run"]
    if changing:
        if (
            len(actions) != 1
            or (worker and actions != ["retire_worker"])
            or (task and actions == ["retire_worker"])
        ):
            _invalid("Choose the action matching the exact identity.")
        if not options["authorized"] or not options["actor"] or not options["reason"]:
            _invalid("Changes require --authorized, --actor and --reason.")
        revision = options["expected_revision"]
        if type(revision) is not int or not 0 <= revision < (1 << 63) - 1:
            _invalid("Entity changes require --expected-revision (zero for no prior control).")
    elif (
        actions
        or options["authorized"]
        or any(
            options[name] is not None
            for name in (
                "expected_revision",
                "actor",
                "reason",
            )
        )
    ):
        _invalid("Change arguments require explicit --dry-run or --apply.")

    using = options["database"]
    # The same supported database and policy checks apply even to entity status.
    read_maintenance_policy(using=using)
    if worker:
        timestamp = options["worker_started_at"]
        if type(timestamp) is not str or len(timestamp) > 64:
            _invalid("Worker start time must be an aware ISO 8601 timestamp.")
        try:
            started = datetime.fromisoformat(timestamp)
        except ValueError:
            raise CommandError("Worker start time must be an aware ISO 8601 timestamp.") from None
        identity = _control_identity(
            WorkerLeaseIdentity(
                options["worker_id"],
                options["worker_hostname"],
                options["worker_pid"],
                started,
            )
        )
        history = RayWorkerRetirement.objects.using(using).filter(**identity.database_filters())
        present = (
            TaskWorkerLease.objects.using(using).filter(**identity.database_filters()).exists()
        )
        rendered_identity = {
            **identity.database_filters(),
            "started_at": identity.started_at.isoformat(),
        }
    else:
        identity = _execution_identity(
            ExecutionIdentity(
                options["task_pk"],
                options["task_id"],
                options["attempt"],
                options["generation"],
            )
        )
        rendered_identity = asdict(identity)
        history = RayTaskQuarantine.objects.using(using).filter(
            task_execution_pk=identity.task_execution_pk
        )
        present = (
            RayTaskExecution.objects.using(using)
            .filter(
                pk=identity.task_execution_pk,
                task_id=identity.task_id,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
            )
            .exists()
        )
        if not present:
            history = history.filter(**rendered_identity)
    current = history.order_by("-revision").first()
    if not present and current is None:
        raise MaintenanceAdmissionError(MaintenanceReason.IDENTITY_CHANGED)
    current_revision = current.revision if current else 0
    state = current.state if current else "UNCONTROLLED"
    report = {
        "schema_version": 1,
        "kind": "worker-retirement" if worker else "task-quarantine",
        "mode": "status",
        "changed": False,
        "previous_revision": current_revision,
        "revision": current_revision,
        "state": state,
        "identity": rendered_identity,
        "current_identity_present": present,
        "control_identity": (
            {field: getattr(current, field) for field in rendered_identity}
            if current is not None and not worker
            else None
        ),
        "drain_verified": False,
    }
    if changing:
        kwargs = {
            "expected_revision": options["expected_revision"],
            "actor": options["actor"],
            "reason": options["reason"],
            "authorized": True,
            "dry_run": options["dry_run"],
            "using": using,
        }
        result = (
            request_worker_retirement(identity, **kwargs)
            if worker
            else set_task_quarantine(
                identity,
                quarantined=options["quarantine_task"],
                **kwargs,
            )
        )
        report.update(
            mode="dry-run" if options["dry_run"] else "applied",
            changed=result.changed,
            previous_revision=result.previous_revision,
            revision=result.revision,
            state=result.state,
        )
        if result.changed and not worker:
            report["control_identity"] = rendered_identity
    if options["as_json"]:
        rendered = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    else:
        rendered = (
            f"{report['kind']} {report['mode']}: {report['state']}; revision {report['revision']}; "
            f"changed={str(report['changed']).lower()}.\n"
            f"Exact identity: {json.dumps(rendered_identity, sort_keys=True, ensure_ascii=True)}.\n"
            f"Current identity present: {str(present).lower()}.\nDrain verification: not performed."
        )
    if len(rendered.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        _invalid("Maintenance status exceeds its output bound.")
    return rendered


def _selected(options):
    selected = []
    for kind, name in (("queue", "queue"), ("protocol", "protocol"), ("target", "target")):
        values = options[name]
        if values is None:
            continue
        if type(values) is not list or len(values) > 64:
            _invalid("Selectors must be finite lists with at most 64 entries in total.")
        for value in values:
            if kind == "protocol":
                if type(value) is not int or not 1 <= value <= 32767:
                    _invalid("Protocol selectors must be positive supported-range integers.")
            elif type(value) is not str:
                _invalid("Queue and target selectors must be exact strings.")
            selected.append((kind, value))
    if len(selected) > 64 or len(set(selected)) != len(selected):
        _invalid("Select at most 64 distinct scopes.")
    try:
        _scopes(
            tuple(
                MaintenanceScope(
                    kind,
                    **{
                        {
                            "queue": "queue_name",
                            "protocol": "protocol_version",
                            "target": "target_id",
                        }[kind]: value
                    },
                )
                for kind, value in selected
            )
        )
    except MaintenanceAdmissionError:
        raise CommandError("Invalid exact maintenance scope selector.") from None
    return selected


def _key(scope):
    return scope.kind, {
        "queue": scope.queue_name,
        "protocol": scope.protocol_version,
        "target": scope.target_id,
    }[scope.kind]


def _proposal(policy, selected, *, all_scopes, enqueue_change, claim_change):
    pause_enqueues, pause_claims = policy.pause_enqueues, policy.pause_claims
    scopes = {_key(scope): scope for scope in policy.scopes}
    if all_scopes:
        if enqueue_change is not None:
            pause_enqueues = enqueue_change
        if claim_change is not None:
            pause_claims = claim_change
    else:
        for kind, value in selected:
            key = (kind, value)
            scope = scopes.get(key)
            if scope is None:
                field = {
                    "queue": "queue_name",
                    "protocol": "protocol_version",
                    "target": "target_id",
                }[kind]
                scope = MaintenanceScope(
                    kind, **{field: value}, pause_enqueues=False, pause_claims=False
                )
            if enqueue_change is not None:
                scope = replace(scope, pause_enqueues=enqueue_change)
            if claim_change is not None:
                scope = replace(scope, pause_claims=claim_change)
            if scope.pause_enqueues or scope.pause_claims:
                scopes[key] = scope
            else:
                scopes.pop(key, None)
    return tuple(scopes.values()), pause_enqueues, pause_claims


def _render(policy: MaintenancePolicySnapshot, *, mode, changed, previous_revision, as_json):
    report = {
        "schema_version": 1,
        "mode": mode,
        "changed": changed,
        "previous_revision": previous_revision,
        "revision": policy.revision,
        "pause_enqueues": policy.pause_enqueues,
        "pause_claims": policy.pause_claims,
        "scopes": [asdict(scope) for scope in policy.scopes],
        "updated_at": policy.updated_at.isoformat(),
        "drain_verified": False,
    }
    if as_json:
        rendered = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    else:
        lines = [
            f"Maintenance admission {mode}: revision {policy.revision}; changed={str(changed).lower()}.",
            f"Deployment enqueue admission: {'paused' if policy.pause_enqueues else 'open'}.",
            f"Deployment new claims: {'paused' if policy.pause_claims else 'open'}.",
        ]
        for scope in policy.scopes:
            kind, value = _key(scope)
            lines.append(
                f"{kind} {json.dumps(value, ensure_ascii=True)}: "
                f"enqueues={'paused' if scope.pause_enqueues else 'open'}, "
                f"claims={'paused' if scope.pause_claims else 'open'}."
            )
        lines.append("Drain verification: not performed.")
        rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        _invalid("Maintenance status exceeds its output bound.")
    return rendered


class Command(BaseCommand):
    help = "Inspect or explicitly change revisioned admission pauses; never infer remote drain"
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--database", default="default", help="Django database alias")
        parser.add_argument(
            "--json", action="store_true", dest="as_json", help="Emit versioned JSON"
        )
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument(
            "--dry-run", action="store_true", help="Review a change without publishing it"
        )
        modes.add_argument(
            "--apply", action="store_true", help="Publish the reviewed revision-CAS change"
        )
        parser.add_argument(
            "--expected-revision", type=int, help="Exact currently reviewed policy revision"
        )
        parser.add_argument(
            "--actor", help="Bounded audit attribution, not an authentication credential"
        )
        parser.add_argument("--reason", help="Bounded audit reason code, without spaces or secrets")
        parser.add_argument(
            "--authorized",
            action="store_true",
            help="Acknowledge authorization to operate this deployment",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_scopes",
            help="Change deployment-wide flags only; preserve exact scopes",
        )
        parser.add_argument(
            "--queue", action="append", help="Exact queue name; repeat for multiple scopes"
        )
        parser.add_argument(
            "--protocol",
            action="append",
            type=int,
            help="Exact execution protocol; repeat as needed",
        )
        parser.add_argument(
            "--target", action="append", help="Exact immutable target key; claims only"
        )
        for field in _WORKER_FIELDS + _TASK_FIELDS:
            parser.add_argument(
                "--" + field.replace("_", "-"),
                type=int if field in {"worker_pid", "task_pk", "attempt", "generation"} else str,
                help="Exact retained worker incarnation or task generation field",
            )
        entity_actions = parser.add_mutually_exclusive_group()
        for action in _ENTITY_ACTIONS:
            entity_actions.add_argument("--" + action.replace("_", "-"), action="store_true")
        enqueues = parser.add_mutually_exclusive_group()
        enqueues.add_argument(
            "--pause-enqueues",
            action="store_const",
            const=True,
            default=None,
            dest="enqueue_change",
        )
        enqueues.add_argument(
            "--resume-enqueues", action="store_const", const=False, dest="enqueue_change"
        )
        claims = parser.add_mutually_exclusive_group()
        claims.add_argument(
            "--pause-claims", action="store_const", const=True, default=None, dest="claim_change"
        )
        claims.add_argument(
            "--resume-claims", action="store_const", const=False, dest="claim_change"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        for flag in ("apply", "dry_run", "all_scopes", "authorized", "as_json", *_ENTITY_ACTIONS):
            if type(options[flag]) is not bool:
                _invalid("Control flags must be explicit booleans.")
        if options["apply"] and options["dry_run"]:
            _invalid("Choose either --dry-run or --apply.")
        if _entity_requested(options):
            try:
                rendered = _entity_report(options)
            except MaintenanceAdmissionError as error:
                raise CommandError(f"Maintenance operation refused: {error.reason.value}") from None
            except CommandError:
                raise
            except Exception:
                raise CommandError("Maintenance operation failed.") from None
            self.stdout.write(rendered)
            return
        for name in ("enqueue_change", "claim_change"):
            if options[name] is not None and type(options[name]) is not bool:
                _invalid("Admission changes must be explicit pause or resume controls.")
        selected = _selected(options)
        change_mode = options["apply"] or options["dry_run"]
        changed_fields = (
            options["enqueue_change"] is not None or options["claim_change"] is not None
        )
        if not change_mode:
            if (
                selected
                or changed_fields
                or options["all_scopes"]
                or options["authorized"]
                or any(
                    options[name] is not None for name in ("expected_revision", "actor", "reason")
                )
            ):
                _invalid("Change arguments require explicit --dry-run or --apply.")
        else:
            if not options["authorized"]:
                _invalid("Changes require --authorized from a trusted operator shell.")
            revision = options["expected_revision"]
            if type(revision) is not int or not 1 <= revision <= (1 << 63) - 1:
                _invalid("Changes require a positive --expected-revision.")
            if not options["actor"] or not options["reason"]:
                _invalid("Changes require --actor and --reason audit attribution.")
            if not changed_fields or bool(selected) == options["all_scopes"]:
                _invalid("Select --all or exact scopes, plus at least one pause/resume control.")
            if (
                any(kind == "target" for kind, _value in selected)
                and options["enqueue_change"] is not None
            ):
                _invalid("Target scopes support claim controls only.")
        try:
            policy = read_maintenance_policy(using=options["database"])
            previous_revision = policy.revision
            changed = False
            mode = "status"
            if change_mode:
                if options["expected_revision"] != policy.revision:
                    raise MaintenanceAdmissionError(MaintenanceReason.REVISION_CHANGED)
                scopes, pause_enqueues, pause_claims = _proposal(
                    policy,
                    selected,
                    all_scopes=options["all_scopes"],
                    enqueue_change=options["enqueue_change"],
                    claim_change=options["claim_change"],
                )
                result = replace_maintenance_policy(
                    scopes,
                    pause_enqueues=pause_enqueues,
                    pause_claims=pause_claims,
                    expected_revision=options["expected_revision"],
                    actor=options["actor"],
                    reason=options["reason"],
                    authorized=True,
                    dry_run=options["dry_run"],
                    using=options["database"],
                )
                policy, changed = result.policy, result.changed
                mode = "dry-run" if options["dry_run"] else "applied"
            rendered = _render(
                policy,
                mode=mode,
                changed=changed,
                previous_revision=previous_revision,
                as_json=options["as_json"],
            )
        except MaintenanceAdmissionError as error:
            reason = (
                error.reason.value if type(error.reason) is MaintenanceReason else "unavailable"
            )
            raise CommandError(f"Maintenance operation refused: {reason}") from None
        except CommandError:
            raise
        except Exception:
            # Database settings/provider errors may contain credentials. Keep
            # status and apply diagnostics fixed and never render raw messages.
            raise CommandError("Maintenance operation failed.") from None
        self.stdout.write(rendered)
