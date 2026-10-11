"""Standalone doctor: start the budget before importing application settings."""

from __future__ import annotations

import argparse
import os

from django_ray.doctor.contracts import Limits, render
from django_ray.doctor.runner import diagnose


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded read-only django-ray observations")
    parser.add_argument("--settings", default=os.environ.get("DJANGO_SETTINGS_MODULE"))
    parser.add_argument("--database", default="default")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--total-seconds", type=float, default=15)
    parser.add_argument("--probe-seconds", type=float, default=5)
    arguments = parser.parse_args()
    try:
        limits = Limits(arguments.total_seconds, arguments.probe_seconds)
    except ValueError:
        parser.error("Invalid doctor time budget")
    report = diagnose(settings_module=arguments.settings, using=arguments.database, limits=limits)
    print(render(report, as_json=arguments.format == "json"))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
