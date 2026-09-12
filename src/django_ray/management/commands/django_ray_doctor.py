"""Read-only database diagnostics with explicit unverified operational checks."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.doctor import DoctorError, build_doctor, render_doctor_json, render_doctor_text


class Command(BaseCommand):
    help = "Inspect bounded django-ray diagnostics without proving remote readiness or drain"
    # Django's general checks may invoke user code or add unbounded unrelated
    # output. This command performs its own bounded migration observation.
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--database", default="default", help="Django database alias to inspect"
        )
        parser.add_argument(
            "--json", action="store_true", dest="as_json", help="Emit versioned JSON"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        try:
            report = build_doctor(using=options["database"])
            rendered = (
                render_doctor_json(report) if options["as_json"] else render_doctor_text(report)
            )
        except DoctorError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(rendered)
