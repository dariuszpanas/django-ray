"""Verify, encrypt or rewrap a bounded page of retained RuntimeEnv snapshots."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from django_ray.conf.settings import get_settings
from django_ray.management.runtime_env_maintenance import (
    OUTPUT_BYTES,
    MaintenanceError,
    MaintenanceLimits,
    maintain_runtime_env,
)


class Command(BaseCommand):
    help = "Verify or explicitly rewrite bounded RuntimeEnv snapshots; never retire backup keys."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--format", choices=("human", "json"), default="human")
        parser.add_argument("--mode", choices=("dry-run", "check", "rewrite"), default="dry-run")
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--cursor", help="Continue the same policy/mode; use --cursor=TOKEN.")
        parser.add_argument("--max-rows", type=int, default=100)
        parser.add_argument("--max-bytes", type=int, default=65536)
        parser.add_argument("--max-total-bytes", type=int, default=1048576)
        parser.add_argument("--max-seconds", type=float, default=10)
        parser.add_argument("--max-details", type=int, default=20)

    def handle(self, *args, **options):
        try:
            limits = MaintenanceLimits(
                rows=options["max_rows"],
                snapshot_bytes=options["max_bytes"],
                total_bytes=options["max_total_bytes"],
                seconds=options["max_seconds"],
                details=options["max_details"],
            )
            report = maintain_runtime_env(
                using=options["database"],
                config=get_settings(),
                mode=options["mode"],
                confirm=options["confirm"],
                cursor=options["cursor"],
                limits=limits,
            )
        except MaintenanceError as error:
            raise CommandError(str(error)) from None
        if options["format"] == "json":
            rendered = json.dumps(report, ensure_ascii=True, separators=(",", ":"))
        else:
            lines = [
                f"RuntimeEnv {report['mode']}: processed {report['processed']} row(s).",
                f"Range complete: {report['range_complete']}; exit: {report['exit_code']}.",
                "Current database rows only; backups not verified; key retirement not authorized.",
                "Elapsed budget is cooperative, not a hard wall-clock deadline.",
            ]
            lines.extend(f"{code}: {count}" for code, count in report["counts"].items() if count)
            lines.extend(f"pk={item['pk']} outcome={item['outcome']}" for item in report["details"])
            if report["next_cursor"]:
                lines.append(f"Next cursor: {report['next_cursor']}")
            rendered = "\n".join(lines)
        if len(rendered.encode()) > OUTPUT_BYTES - 256:
            raise CommandError("RuntimeEnv maintenance report exceeded its output bound.")
        self.stdout.write(rendered)
        if report["exit_code"]:
            raise CommandError(
                "RuntimeEnv maintenance requires attention; inspect the report.",
                returncode=report["exit_code"],
            )
