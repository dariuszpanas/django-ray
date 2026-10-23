"""Observe filesystem payload metadata under explicit read-only budgets."""

from __future__ import annotations

from django.conf import Settings, settings
from django.core.management.base import BaseCommand, CommandError

from django_ray.payload_inventory.contracts import Limits, render
from django_ray.payload_inventory.process import inventory


class Command(BaseCommand):
    help = "Inventory filesystem input/request payload metadata read-only (Linux only)"
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--total-seconds", type=float, default=15)
        parser.add_argument("--max-entries", type=int, default=1000)
        parser.add_argument("--max-objects", type=int, default=100)
        parser.add_argument("--max-details", type=int, default=20)
        parser.add_argument("--grace-hours", type=int, default=24)
        parser.add_argument("--shard")

    def handle(self, *args, **options):
        try:
            limits = Limits(
                **{
                    name: options[name]
                    for name in (
                        "total_seconds",
                        "max_entries",
                        "max_objects",
                        "max_details",
                        "grace_hours",
                        "shard",
                    )
                }
            )
        except ValueError:
            raise CommandError("Invalid inventory limits", returncode=2) from None
        module = settings.SETTINGS_MODULE if type(settings._wrapped) is Settings else None
        value = inventory(settings_module=module, using=options["database"], limits=limits)
        self.stdout.write(render(value, as_json=options["format"] == "json"))
        if value["exit_code"]:
            raise CommandError("Payload inventory is incomplete or unavailable", returncode=2)
