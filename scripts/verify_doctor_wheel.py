"""Verify doctor against an owned file database in the installed environment."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def _run(command: list[str], *, directory: Path, environment: dict[str, str]) -> tuple[int, bytes]:
    process = subprocess.Popen(
        command,
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=25)
        if stderr or len(stdout) > 32768:
            raise RuntimeError("Installed doctor emitted unexpected output")
        return process.returncode, stdout
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def verify() -> None:
    if not sys.platform.startswith("linux"):
        return
    with TemporaryDirectory(prefix="django-ray-doctor-wheel-") as temporary:
        directory = Path(temporary)
        database = directory / "owned.sqlite3"
        (directory / "owned_doctor_settings.py").write_text(
            "SECRET_KEY = 'owned-doctor-fixture'\nUSE_TZ = True\n"
            # A settings-selected application must never initialize in a probe.
            "INSTALLED_APPS = ['must_not_be_imported_by_doctor']\n"
            "DJANGO_RAY = {'RUNNER': 'ray_core'}\n"
            f"DATABASES = {{'default': {{'ENGINE': 'django.db.backends.sqlite3', 'NAME': {str(database)!r}}}}}\n",
            encoding="utf-8",
        )
        environment = {**os.environ, "PYTHONPATH": str(directory)}
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        setup = (
            "import django;from django.conf import settings;"
            "from owned_doctor_settings import DATABASES;"
            "settings.configure(INSTALLED_APPS=['django_ray.doctor.probe.DoctorAppConfig'],"
            "DATABASES=DATABASES,SECRET_KEY='owned',USE_TZ=True,LOGGING_CONFIG=None);"
            "django.setup();from django.core.management import call_command;"
            "call_command('migrate',verbosity=0,interactive=False)"
        )
        status, output = _run(
            [sys.executable, "-c", setup], directory=directory, environment=environment
        )
        if status or output:
            raise RuntimeError("Installed doctor fixture setup failed")
        before = hashlib.sha256(database.read_bytes()).digest()
        status, output = _run(
            [
                sys.executable,
                "-m",
                "django_ray.doctor",
                "--settings",
                "owned_doctor_settings",
                "--format",
                "json",
            ],
            directory=directory,
            environment=environment,
        )
        report = json.loads(output)
        if not (
            status == 2
            and report["schema"] == "django-ray.doctor"
            and report["schema_version"] == 1
            and report["exit_code"] == 2
            and not report["complete"]
            and report["cleanup_confirmed"]
            and report["sections"]["configuration"]["outcome"] == "observed"
            and report["sections"]["database"]["values"] == {"vendor": "sqlite", "read_only": True}
            and report["sections"]["migrations"]["values"]["missing"] == 0
            and report["sections"]["work"]["values"]["queued"] == 0
            and all(
                report["sections"][name]["code"] == "not_implemented"
                for name in (
                    "target",
                    "queue_capacity",
                    "artifact_availability",
                    "orphan_inventory",
                )
            )
            and hashlib.sha256(database.read_bytes()).digest() == before
        ):
            raise RuntimeError("Installed doctor did not preserve its observation boundary")
    if directory.exists():
        raise RuntimeError("Installed doctor fixture was not removed")
    print(
        "Installed doctor: owned SQLite observations, incomplete exit 2, read-only preservation and cleanup passed"
    )


if __name__ == "__main__":
    verify()
