"""Read-only observations using independent, bounded Linux child processes."""

from __future__ import annotations

from django.conf import Settings, settings
from django.core.management.base import BaseCommand, CommandError

from django_ray.doctor.contracts import Limits, render
from django_ray.doctor.runner import diagnose


class Command(BaseCommand):
    help = "Report bounded read-only django-ray observations (Linux only)"
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--total-seconds", type=float, default=15)
        parser.add_argument("--probe-seconds", type=float, default=5)

    def handle(self, *args, **options):
        try:
            limits = Limits(options["total_seconds"], options["probe_seconds"])
        except ValueError:
            raise CommandError("Invalid doctor time budget", returncode=2) from None
        # configure()/override_settings cannot be reconstructed by a fresh child.
        # Never silently fall back to a different database or application config.
        module = settings.SETTINGS_MODULE if type(settings._wrapped) is Settings else None
        report = diagnose(settings_module=module, using=options["database"], limits=limits)
        self.stdout.write(render(report, as_json=options["format"] == "json"))
        if report["exit_code"]:
            raise CommandError(
                "Doctor observations are incomplete or contain blockers",
                returncode=report["exit_code"],
            )
