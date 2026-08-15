"""Built-distribution metadata verification tests."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.verify_wheel import (
    EXPECTED_FILES,
    EXPECTED_MIGRATION_LEAF,
    verify_distribution_archives,
)


def test_release_boundary_tracks_latest_schema_migration() -> None:
    assert "django_ray/execution_codec.py" in EXPECTED_FILES
    assert "django_ray/execution_protocol.py" in EXPECTED_FILES
    assert "django_ray/ray_job_protocol.py" in EXPECTED_FILES
    assert "django_ray/ray_job_request_storage.py" in EXPECTED_FILES
    assert "django_ray/ray_target_probe.py" in EXPECTED_FILES
    assert "django_ray/protocol_coordination.py" in EXPECTED_FILES
    assert "django_ray/protocol_status.py" in EXPECTED_FILES
    assert "django_ray/management/commands/django_ray_protocol_status.py" in EXPECTED_FILES
    assert "django_ray/runner/ray_job.py" in EXPECTED_FILES
    assert "django_ray/runtime/entrypoint.py" in EXPECTED_FILES
    assert "django_ray/target_attestation.py" in EXPECTED_FILES
    assert "django_ray/target_capabilities.py" in EXPECTED_FILES
    assert "django_ray/target_coordination.py" in EXPECTED_FILES
    assert "django_ray/target_execution_codec.py" in EXPECTED_FILES
    assert "django_ray/target_execution_evidence.py" in EXPECTED_FILES
    assert "django_ray/target_routing.py" in EXPECTED_FILES
    assert "django_ray/runner/ray_core.py" in EXPECTED_FILES
    assert "django_ray/runtime/remote.py" in EXPECTED_FILES
    assert "django_ray/migrations/0019_execution_protocol_schema.py" in EXPECTED_FILES
    assert "django_ray/migrations/0020_legacy_open_rollback_fence.py" in EXPECTED_FILES
    assert "django_ray/migrations/0021_ray_job_request_reference.py" in EXPECTED_FILES
    assert "django_ray/migrations/0022_ray_target_persistence.py" in EXPECTED_FILES
    assert "django_ray/migrations/0023_ray_task_target_binding.py" in EXPECTED_FILES
    assert "django_ray/migrations/0024_ray_target_routes.py" in EXPECTED_FILES
    assert "django_ray/migrations/0025_ray_worker_target_capabilities.py" in EXPECTED_FILES
    assert "django_ray/migrations/0026_ray_task_target_execution_evidence.py" in EXPECTED_FILES
    assert EXPECTED_MIGRATION_LEAF == (
        "django_ray",
        "0026_ray_task_target_execution_evidence",
    )


def _metadata(*, ray_requirement: str = "ray[default]>=2.56.0") -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        "Name: django-ray\n"
        "Version: 0.4.0\n"
        f"Requires-Dist: {ray_requirement}\n"
        "\n"
    ).encode()


def _write_distributions(
    dist_dir: Path,
    *,
    wheel_requirement: str = "ray[default]>=2.56.0",
    sdist_requirement: str = "ray[default]>=2.56.0",
) -> None:
    wheel = dist_dir / "django_ray-0.4.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "django_ray-0.4.0.dist-info/METADATA",
            _metadata(ray_requirement=wheel_requirement),
        )

    sdist = dist_dir / "django_ray-0.4.0.tar.gz"
    payload = _metadata(ray_requirement=sdist_requirement)
    member = tarfile.TarInfo("django_ray-0.4.0/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(sdist, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))


def test_distribution_archives_publish_the_ray_security_floor(tmp_path: Path) -> None:
    _write_distributions(tmp_path)

    verify_distribution_archives(tmp_path, "0.4.0")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_distribution_archives_reject_a_pre_floor_ray_requirement(
    tmp_path: Path,
    artifact: str,
) -> None:
    requirements = {
        "wheel_requirement": "ray[default]>=2.56.0",
        "sdist_requirement": "ray[default]>=2.56.0",
    }
    requirements[f"{artifact}_requirement"] = "ray[default]>=2.53.0"
    _write_distributions(tmp_path, **requirements)

    with pytest.raises(RuntimeError, match=r"ray\[default\]>=2\.56\.0"):
        verify_distribution_archives(tmp_path, "0.4.0")
