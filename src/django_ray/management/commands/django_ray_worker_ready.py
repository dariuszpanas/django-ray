"""Probe an exact task-manager worker lease without changing it."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.worker_readiness import check_worker_lease_readiness


class Command(BaseCommand):
    """Expose bounded lease readiness, independently of Ray or queue capacity."""

    help = "Check one exact Django task-manager worker lease (not Ray readiness)"
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--database", default="default")
        parser.add_argument("--queue", required=True)
        parser.add_argument("--hostname", required=True)
        parser.add_argument("--worker-id", default=None)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        report = check_worker_lease_readiness(
            using=options["database"],
            queue=options["queue"],
            hostname=options["hostname"],
            worker_id=options["worker_id"],
        )
        self.stdout.write(
            json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"))
            if options["as_json"]
            else f"Worker lease: {report.status} ({report.reason})."
        )
        if report.exit_code:
            raise CommandError("Worker lease is not ready.", returncode=report.exit_code)
