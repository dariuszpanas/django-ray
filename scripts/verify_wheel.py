"""Smoke-test an installed django-ray wheel and its package contents."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path

EXPECTED_FILES = {
    "django_ray/__init__.py",
    "django_ray/models.py",
    "django_ray/migrations/0001_initial.py",
    "django_ray/migrations/0014_raytaskexecution_ray_target_address.py",
    "django_ray/management/commands/django_ray_worker.py",
}
EXPECTED_MIGRATION_LEAF = (
    "django_ray",
    "0014_raytaskexecution_ray_target_address",
)


def verify_installed_wheel(expected_version: str) -> None:
    """Check metadata, import version, migrations, command discovery, and contents."""
    distribution = importlib.metadata.distribution("django-ray")
    if distribution.version != expected_version:
        raise RuntimeError(
            f"installed metadata version {distribution.version!r} != {expected_version!r}"
        )

    import django_ray

    if django_ray.__version__ != expected_version:
        raise RuntimeError(
            f"imported __version__ {django_ray.__version__!r} != {expected_version!r}"
        )

    imported_package = Path(django_ray.__file__).resolve().parent
    distribution_package = Path(distribution.locate_file("django_ray")).resolve()
    if imported_package != distribution_package:
        raise RuntimeError(
            "imported django_ray does not come from the installed distribution: "
            f"{imported_package} != {distribution_package}"
        )

    files = {str(path).replace("\\", "/") for path in (distribution.files or ())}
    missing = sorted(EXPECTED_FILES - files)
    if missing:
        raise RuntimeError(f"wheel is missing expected files: {', '.join(missing)}")

    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            SECRET_KEY="release-smoke-test",
            INSTALLED_APPS=["django_ray"],
            DJANGO_RAY={"RAY_ADDRESS": "local"},
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        )
    django.setup()

    from django.core.management import call_command, get_commands
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.recorder import MigrationRecorder

    if "django_ray_worker" not in get_commands():
        raise RuntimeError("django_ray_worker management command was not discovered")

    migration_leaves = set(MigrationLoader(connection).graph.leaf_nodes("django_ray"))
    if migration_leaves != {EXPECTED_MIGRATION_LEAF}:
        raise RuntimeError(
            "installed django_ray migration leaves do not match the release boundary: "
            f"{sorted(migration_leaves)!r}"
        )

    call_command("migrate", "django_ray", interactive=False, verbosity=0)
    applied = MigrationRecorder(connection).applied_migrations()
    if EXPECTED_MIGRATION_LEAF not in applied:
        raise RuntimeError(f"installed migration leaf was not applied: {EXPECTED_MIGRATION_LEAF!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    verify_installed_wheel(args.version)
    print(f"Verified django-ray {args.version} wheel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
