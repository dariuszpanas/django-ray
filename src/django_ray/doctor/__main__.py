"""Standalone doctor: start the budget before importing application settings."""

from __future__ import annotations

import argparse
import os

from django_ray.doctor.contracts import Limits, render
from django_ray.doctor.runner import diagnose


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded read-only django-ray observations")
    parser.add_argument(
        "--settings",
        default=os.environ.get("DJANGO_SETTINGS_MODULE"),
        help="Dotted Django settings module to inspect (default: DJANGO_SETTINGS_MODULE).",
    )
    parser.add_argument(
        "--database",
        default="default",
        help="Django database alias to inspect (default: default).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format: text or json (default: text).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit JSON output (alias for --format json).",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=15,
        help="Total doctor time budget in seconds, 5-60, including 2s cleanup reserve (default: 15).",
    )
    parser.add_argument(
        "--probe-seconds",
        type=float,
        default=5,
        help="Per-probe time budget in seconds, 3-20, at most total-seconds minus 2 (default: 5).",
    )
    arguments = parser.parse_args()
    try:
        limits = Limits(arguments.total_seconds, arguments.probe_seconds)
    except ValueError as error:
        parser.error(str(error))
    report = diagnose(settings_module=arguments.settings, using=arguments.database, limits=limits)
    use_json = bool(arguments.as_json) or arguments.format == "json"
    print(render(report, as_json=use_json))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
