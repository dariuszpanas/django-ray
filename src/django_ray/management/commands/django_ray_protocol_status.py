"""Render bounded read-only execution-protocol rollout status."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.protocol_status import (
    ProtocolStatusError,
    build_protocol_status,
    render_protocol_status_json,
    render_protocol_status_text,
)


class Command(BaseCommand):
    """Inspect execution-protocol rollout state without mutating it."""

    help = "Report bounded read-only django-ray execution-protocol rollout status"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias to inspect (default: default)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit canonical versioned JSON",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        try:
            report = build_protocol_status(using=str(options["database"]))
            rendered = (
                render_protocol_status_json(report)
                if bool(options["as_json"])
                else render_protocol_status_text(report)
            )
        except ProtocolStatusError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(rendered)
