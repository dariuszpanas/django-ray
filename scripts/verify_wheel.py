"""Smoke-test an installed django-ray wheel and its package contents."""

from __future__ import annotations

import argparse
import importlib.metadata
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.version import Version

EXPECTED_FILES = {
    "django_ray/__init__.py",
    "django_ray/admin.py",
    "django_ray/execution_protocol.py",
    "django_ray/protocol_coordination.py",
    "django_ray/models.py",
    "django_ray/runtime/runtime_env_encryption.py",
    "django_ray/static/django_ray/admin/diagnostics.css",
    "django_ray/static/django_ray/admin/task_live.css",
    "django_ray/static/django_ray/admin/task_live.js",
    "django_ray/static/django_ray/admin/workflow_diagnostics.js",
    "django_ray/static/django_ray/admin/sensitive_task_data.css",
    "django_ray/static/django_ray/admin/sensitive_task_data_theme.js",
    "django_ray/templates/admin/django_ray/sensitive_task_data.html",
    "django_ray/templates/admin/django_ray/sensitive_task_data_limit.html",
    "django_ray/templates/admin/django_ray/raytaskexecution/change_form.html",
    "django_ray/templates/admin/django_ray/taskattempt/change_form.html",
    "django_ray/migrations/0001_initial.py",
    "django_ray/migrations/0014_raytaskexecution_ray_target_address.py",
    "django_ray/migrations/0015_raytaskexecution_task_id_unique.py",
    "django_ray/migrations/0016_raytaskexecution_queue_expiration.py",
    "django_ray/migrations/0017_raytaskexecution_sensitive_data_permission.py",
    "django_ray/migrations/0018_workflow_run_allocation.py",
    "django_ray/migrations/0019_execution_protocol_schema.py",
    "django_ray/migrations/0020_legacy_open_rollback_fence.py",
    "django_ray/management/commands/django_ray_worker.py",
}
EXPECTED_MIGRATION_LEAF = (
    "django_ray",
    "0020_legacy_open_rollback_fence",
)


def _verify_ray_security_floor(requirements: list[str], *, source: str) -> None:
    ray_requirements = [
        "".join(requirement.split()).lower().replace("_", "-")
        for requirement in requirements
        if requirement.partition(";")[0]
        .strip()
        .lower()
        .replace("_", "-")
        .startswith("ray[default]")
    ]
    if ray_requirements != ["ray[default]>=2.56.0"]:
        raise RuntimeError(
            f"{source} must contain exactly one ray[default]>=2.56.0 runtime security floor"
        )


def _verify_archive_metadata(metadata_text: str, *, expected_version: str, source: str) -> None:
    metadata = Parser().parsestr(metadata_text)
    if metadata.get("Name") != "django-ray":
        raise RuntimeError(f"{source} has unexpected package name {metadata.get('Name')!r}")
    if metadata.get("Version") != expected_version:
        raise RuntimeError(
            f"{source} metadata version {metadata.get('Version')!r} != {expected_version!r}"
        )
    _verify_ray_security_floor(metadata.get_all("Requires-Dist", []), source=source)


def verify_distribution_archives(dist_dir: Path, expected_version: str) -> None:
    """Verify the built wheel and sdist publish the same Ray security floor."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            "distribution directory must contain exactly one wheel and one .tar.gz sdist"
        )

    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise RuntimeError("wheel must contain exactly one .dist-info/METADATA file")
        wheel_metadata = archive.read(metadata_names[0]).decode("utf-8")
    _verify_archive_metadata(
        wheel_metadata,
        expected_version=expected_version,
        source="wheel archive metadata",
    )

    with tarfile.open(sdists[0], mode="r:gz") as archive:
        metadata_members = [
            member
            for member in archive.getmembers()
            if member.isfile() and member.name.endswith("/PKG-INFO") and member.name.count("/") == 1
        ]
        if len(metadata_members) != 1:
            raise RuntimeError("sdist must contain exactly one root package PKG-INFO file")
        metadata_file = archive.extractfile(metadata_members[0])
        if metadata_file is None:
            raise RuntimeError("sdist PKG-INFO could not be read")
        sdist_metadata = metadata_file.read().decode("utf-8")
    _verify_archive_metadata(
        sdist_metadata,
        expected_version=expected_version,
        source="sdist archive metadata",
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

    runtime_unfold_requirements = [
        requirement
        for requirement in (distribution.requires or ())
        if requirement.lower().replace("_", "-").startswith("django-unfold")
        and "extra ==" not in requirement.lower()
    ]
    if runtime_unfold_requirements:
        raise RuntimeError("django-unfold must not be a required wheel dependency")

    cryptography_requirements = [
        requirement
        for requirement in (distribution.requires or ())
        if requirement.partition(";")[0]
        .strip()
        .lower()
        .replace("_", "-")
        .startswith("cryptography")
    ]
    normalized_cryptography_requirements = [
        "".join(requirement.split()).lower().replace("_", "-")
        for requirement in cryptography_requirements
    ]
    if normalized_cryptography_requirements != ["cryptography>=42.0.8"]:
        raise RuntimeError(
            "cryptography>=42.0.8 must be exactly one unconditional runtime dependency"
        )

    pyasn1_requirements = [
        "".join(requirement.split()).lower().replace("_", "-")
        for requirement in (distribution.requires or ())
        if requirement.partition(";")[0].strip().lower().replace("_", "-").startswith("pyasn1")
        and not requirement.partition(";")[0]
        .strip()
        .lower()
        .replace("_", "-")
        .startswith("pyasn1-modules")
    ]
    if pyasn1_requirements != ["pyasn1>=0.6.4"]:
        raise RuntimeError("pyasn1>=0.6.4 must be exactly one runtime security floor")
    installed_pyasn1 = Version(importlib.metadata.version("pyasn1"))
    if installed_pyasn1 < Version("0.6.4"):
        raise RuntimeError(f"installed pyasn1 {installed_pyasn1} is below the 0.6.4 security floor")

    _verify_ray_security_floor(
        list(distribution.requires or ()),
        source="installed wheel metadata",
    )
    installed_ray = Version(importlib.metadata.version("ray"))
    if installed_ray < Version("2.56.0"):
        raise RuntimeError(f"installed Ray {installed_ray} is below the 2.56.0 security floor")

    import cryptography
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not cryptography.__version__ or len(AESGCM.generate_key(bit_length=256)) != 32:
        raise RuntimeError("cryptography runtime dependency could not be imported")

    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            SECRET_KEY="release-smoke-test",
            INSTALLED_APPS=[
                "django.contrib.admin",
                "django.contrib.auth",
                "django.contrib.contenttypes",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django_ray",
            ],
            DJANGO_RAY={"RAY_ADDRESS": "local"},
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        )
    django.setup()

    from django.contrib import admin
    from django.core.management import call_command, get_commands
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.recorder import MigrationRecorder

    from django_ray.admin import RayTaskExecutionAdmin, TaskAttemptAdmin, TaskWorkerLeaseAdmin
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    expected_admins = {
        RayTaskExecution: RayTaskExecutionAdmin,
        TaskAttempt: TaskAttemptAdmin,
        TaskWorkerLease: TaskWorkerLeaseAdmin,
    }
    for model, expected_admin in expected_admins.items():
        registered_admin = admin.site._registry.get(model)
        if not isinstance(registered_admin, expected_admin):
            raise RuntimeError(f"{model.__name__} admin was not discovered from the wheel")
        if not isinstance(registered_admin, admin.ModelAdmin):
            raise RuntimeError(f"{model.__name__} did not retain standard admin compatibility")

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
    parser.add_argument("--dist-dir", type=Path)
    args = parser.parse_args()
    if args.dist_dir is not None:
        verify_distribution_archives(args.dist_dir, args.version)
    verify_installed_wheel(args.version)
    archive_suffix = " and source distribution" if args.dist_dir is not None else ""
    print(f"Verified django-ray {args.version} wheel{archive_suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
