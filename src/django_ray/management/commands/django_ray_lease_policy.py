"""Inspect or explicitly change the database-scoped common worker lease policy."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils.connection import ConnectionDoesNotExist

from django_ray.lease_policy import (
    LeasePolicy,
    LeasePolicyError,
    configured_lease_seconds,
    read_lease_policy,
    set_lease_policy,
)


def _remediation_for_reason(
    reason: str,
    *,
    configured: int | None = None,
    policy: LeasePolicy | None = None,
) -> str:
    """Return a fixed, secret-free remediation message naming exact flags/settings."""
    if reason == "lease_policy_mismatch":
        if configured is not None and policy is not None:
            return (
                f"Configured DJANGO_RAY['WORKER_LEASE_SECONDS'] ({configured}s) does not match "
                f"database lease authority ({policy.seconds}s, revision {policy.revision}). "
                f"Set WORKER_LEASE_SECONDS={policy.seconds} or update during a drained window "
                f"with: python manage.py django_ray_lease_policy --set-seconds={configured} "
                f"--expected-seconds={policy.seconds} --expected-revision={policy.revision} "
                f"--all-lease-users-stopped"
            )
        return (
            "Configured DJANGO_RAY['WORKER_LEASE_SECONDS'] does not match database lease authority. "
            "Align WORKER_LEASE_SECONDS or update during a drained window with --set-seconds, "
            "--expected-seconds, --expected-revision, and --all-lease-users-stopped."
        )
    hints = {
        "lease_policy_transition_invalid": (
            "To inspect policy, omit --expected-seconds, --expected-revision, and "
            "--all-lease-users-stopped. To change policy, pass --set-seconds (1-86400) together "
            "with --expected-seconds, --expected-revision, and --all-lease-users-stopped."
        ),
        "lease_policy_active_leases": (
            "Stop all active django_ray_worker processes until no active TaskWorkerLease rows "
            "remain, then retry with --all-lease-users-stopped."
        ),
        "lease_policy_active_work": (
            "Wait for all RUNNING and CANCELLING tasks to finish or cancel them before changing "
            "the lease policy."
        ),
        "lease_policy_changed": (
            "Re-run 'python manage.py django_ray_lease_policy' to read the current lease_seconds "
            "and revision, then retry with updated --expected-seconds and --expected-revision."
        ),
        "lease_policy_invalid": (
            "Ensure migration 0028_worker_lease_policy is applied and "
            "DJANGO_RAY['WORKER_HEARTBEAT_SECONDS'] < WORKER_LEASE_SECONDS (1-86400)."
        ),
        "lease_policy_busy": (
            "Another worker or policy transition holds the lease authority lock; retry once "
            "the cohort is stopped."
        ),
        "lease_policy_transaction_required": (
            "Run django_ray_lease_policy outside an open transaction with autocommit enabled."
        ),
        "lease_policy_revision_exhausted": (
            "The lease policy revision counter reached its 64-bit maximum and cannot increment."
        ),
        "lease_policy_unavailable": (
            "Verify --database names a supported, migrated PostgreSQL or SQLite database."
        ),
    }
    return hints.get(reason, hints["lease_policy_unavailable"])


class Command(BaseCommand):
    help = "Read lease policy, or change it only during a stopped, drained cohort."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias whose lease policy to inspect or update (default: default).",
        )
        parser.add_argument(
            "--set-seconds",
            type=int,
            help=(
                "Target common worker lease duration in seconds (1-86400); requires "
                "--expected-seconds, --expected-revision, and --all-lease-users-stopped."
            ),
        )
        parser.add_argument(
            "--expected-seconds",
            type=int,
            help="Current lease duration in seconds expected by the CAS update (with --set-seconds).",
        )
        parser.add_argument(
            "--expected-revision",
            type=int,
            help="Current lease policy revision expected by the CAS update (with --set-seconds).",
        )
        parser.add_argument(
            "--all-lease-users-stopped",
            action="store_true",
            help="Confirm all workers and lease users are stopped before updating policy.",
        )
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="json",
            help="Output format: json or text (default: json).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit JSON output (alias for --format json).",
        )

    def _fail(
        self,
        reason: str,
        *,
        use_json: bool,
        configured: int | None = None,
        policy: LeasePolicy | None = None,
    ) -> None:
        remediation = _remediation_for_reason(reason, configured=configured, policy=policy)
        if use_json:
            self.stderr.write(f"Hint ({reason}): {remediation}")
            raise CommandError(reason, returncode=2)
        raise CommandError(f"{reason}: {remediation}", returncode=2)

    def handle(self, *args, **options):
        use_json = bool(options.get("as_json")) or options.get("format", "json") == "json"
        try:
            using = options["database"]
            if options["set_seconds"] is None:
                if (
                    any(
                        options[name] is not None
                        for name in ("expected_seconds", "expected_revision")
                    )
                    or options["all_lease_users_stopped"]
                ):
                    raise LeasePolicyError("lease_policy_transition_invalid")
                policy = read_lease_policy(using=using, matching=False)
            else:
                policy = set_lease_policy(
                    seconds=options["set_seconds"],
                    expected_seconds=options["expected_seconds"],
                    expected_revision=options["expected_revision"],
                    all_lease_users_stopped=options["all_lease_users_stopped"],
                    using=using,
                )
            configured = configured_lease_seconds()
            status = "matched" if configured == policy.seconds else "mismatch"
            if use_json:
                self.stdout.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "scope": "worker_lease_policy",
                            "status": status,
                            "configured_seconds": configured,
                            "lease_seconds": policy.seconds,
                            "revision": policy.revision,
                        },
                        sort_keys=True,
                    )
                )
            else:
                self.stdout.write(
                    f"Worker lease policy: {status} "
                    f"(configured={configured}s, authority={policy.seconds}s, "
                    f"revision={policy.revision})."
                )
            if configured != policy.seconds and options["set_seconds"] is None:
                self._fail(
                    "lease_policy_mismatch",
                    use_json=use_json,
                    configured=configured,
                    policy=policy,
                )
        except LeasePolicyError as error:
            self._fail(error.reason, use_json=use_json)
        except (DatabaseError, ConnectionDoesNotExist, KeyError, TypeError, ValueError):
            self._fail("lease_policy_unavailable", use_json=use_json)
