"""Prepare one disposable application database, static tree and RuntimeEnv pair."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path


def prepare(*, base: Path, runtime_root: Path, database_timeout: float = 120) -> dict:
    """Run inside the web init container; the executor owns its outer deadline."""
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
        raise ValueError("Application setup requires the explicit qualification settings")
    if not math.isfinite(database_timeout) or not 0 < database_timeout <= 300:
        raise ValueError("Database wait must be positive and at most 300 seconds")
    recovery = runtime_root / "recovery.zip"
    if os.environ.get("DJANGO_RAY_RECOVERY_WORKING_DIR") != str(recovery):
        raise ValueError("The configured recovery archive must match setup output")
    password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
    if len(password) < 32 or len(set(password)) < 5:
        raise ValueError("The disposable administrator credential is missing or invalid")

    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import InterfaceError, OperationalError, connection

    from qualification.application.generic_nodes import archive_identity
    from testproject.runtime_env_bundles import build_recovery_bundle, build_source_bundle

    if connection.vendor != "postgresql":
        raise ValueError("Application qualification requires PostgreSQL")
    deadline = time.monotonic() + database_timeout
    while True:
        try:
            connection.ensure_connection()
            break
        except (InterfaceError, OperationalError):
            connection.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Application database did not become ready") from None
            time.sleep(min(1, remaining))
    if not 170000 <= connection.pg_version < 180000:
        raise ValueError("Application qualification requires PostgreSQL 17")

    # One namespace-owned DB and one web Pod with Recreate: no migration peers.
    call_command("migrate", interactive=False, verbosity=0)
    call_command("collectstatic", interactive=False, verbosity=0)
    user, _ = get_user_model().objects.get_or_create(username="qualification")
    user.is_staff = user.is_superuser = user.is_active = True
    if not user.check_password(password):
        user.set_password(password)
    user.save()

    source = runtime_root / "project.zip"
    build_source_bundle(base=base, target=source)
    build_recovery_bundle(base=base, target=recovery)
    archives = {"source": archive_identity(source), "recovery": archive_identity(recovery)}
    # Hashes and fixed facts only; credentials and serialized RuntimeEnv remain
    # outside receipts. The image/source identity is supplied by the executor.
    return {
        "schema_version": 1,
        "layer": "application_setup",
        "status": "passed",
        "complete_application_gate": False,
        "postgresql_major": connection.pg_version // 10000,
        "migrations_applied": True,
        "static_collected": Path(settings.STATIC_ROOT, "staticfiles.json").is_file(),
        "administrator_created": True,
        "archives": archives,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/app"))
    parser.add_argument("--runtime-root", type=Path, default=Path("/runtime"))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--database-timeout", type=float, default=120)
    args = parser.parse_args(argv)
    passed = False
    receipt = {
        "schema_version": 1,
        "layer": "application_setup",
        "status": "failed",
        "complete_application_gate": False,
    }
    try:
        receipt = prepare(
            base=args.base, runtime_root=args.runtime_root, database_timeout=args.database_timeout
        )
        if receipt["static_collected"] is not True:
            raise ValueError("Application static manifest is missing")
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 16 * 1024:
            raise ValueError("Application setup receipt exceeds its byte limit")
        target = args.receipt or args.runtime_root / "setup.json"
        # Web restarts may repeat setup against the same run-owned volume. Never
        # replace a previous source/archive identity with different bytes.
        if target.exists():
            with target.open("rb") as stream:
                if stream.read(len(encoded) + 1) != encoded:
                    raise ValueError("Application setup changed the existing receipt")
        else:
            with target.open("xb") as stream:
                stream.write(encoded)
        passed = True
    except Exception:
        receipt = {
            "schema_version": 1,
            "layer": "application_setup",
            "status": "failed",
            "complete_application_gate": False,
        }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
