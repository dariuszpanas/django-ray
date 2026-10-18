"""Inspect or explicitly change the database-scoped common worker lease policy."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils.connection import ConnectionDoesNotExist

from django_ray.lease_policy import (
    LeasePolicyError,
    configured_lease_seconds,
    read_lease_policy,
    set_lease_policy,
)


class Command(BaseCommand):
    help = "Read lease policy, or change it only during a stopped, drained cohort."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--set-seconds", type=int)
        parser.add_argument("--expected-seconds", type=int)
        parser.add_argument("--expected-revision", type=int)
        parser.add_argument("--all-lease-users-stopped", action="store_true")

    def handle(self, *args, **options):
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
            self.stdout.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope": "worker_lease_policy",
                        "status": "matched" if configured == policy.seconds else "mismatch",
                        "configured_seconds": configured,
                        "lease_seconds": policy.seconds,
                        "revision": policy.revision,
                    },
                    sort_keys=True,
                )
            )
            if configured != policy.seconds and options["set_seconds"] is None:
                raise CommandError("lease_policy_mismatch", returncode=2)
        except LeasePolicyError as error:
            raise CommandError(error.reason, returncode=2) from None
        except (DatabaseError, ConnectionDoesNotExist, KeyError, TypeError, ValueError):
            raise CommandError("lease_policy_unavailable", returncode=2) from None
