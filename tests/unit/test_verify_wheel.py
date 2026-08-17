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
    EXPECTED_TARGET_MODULE_FILES,
    EXPECTED_WORKFLOW_MODULE_FILES,
    _verify_canonical_module_layout,
    verify_distribution_archives,
)


def test_release_boundary_tracks_latest_schema_migration() -> None:
    assert "django_ray/execution_codec.py" in EXPECTED_FILES
    assert "django_ray/execution_protocol.py" in EXPECTED_FILES
    assert "django_ray/ray_job_protocol.py" in EXPECTED_FILES
    assert "django_ray/ray_job_request_storage.py" in EXPECTED_FILES
    assert "django_ray/protocol_coordination.py" in EXPECTED_FILES
    assert "django_ray/protocol_status.py" in EXPECTED_FILES
    assert "django_ray/management/commands/django_ray_protocol_status.py" in EXPECTED_FILES
    assert "django_ray/runner/ray_job.py" in EXPECTED_FILES
    assert "django_ray/runtime/entrypoint.py" in EXPECTED_FILES

    assert EXPECTED_TARGET_MODULE_FILES == {
        "django_ray/target/__init__.py",
        "django_ray/target/attestation.py",
        "django_ray/target/capabilities.py",
        "django_ray/target/coordination.py",
        "django_ray/target/execution_codec.py",
        "django_ray/target/execution_evidence.py",
        "django_ray/target/probe.py",
        "django_ray/target/routing.py",
    }
    assert EXPECTED_WORKFLOW_MODULE_FILES == {
        "django_ray/workflows.py",
        "django_ray/workflow/__init__.py",
        "django_ray/workflow/admin_graph.py",
        "django_ray/workflow/contracts.py",
        "django_ray/workflow/plans.py",
        "django_ray/workflow/previews.py",
        "django_ray/workflow/progress/__init__.py",
        "django_ray/workflow/progress/cleanup.py",
        "django_ray/workflow/progress/limits.py",
        "django_ray/workflow/progress/preparation.py",
        "django_ray/workflow/progress/producer.py",
        "django_ray/workflow/progress/protocol.py",
        "django_ray/workflow/progress/publication.py",
        "django_ray/workflow/progress/reads.py",
        "django_ray/workflow/progress/runs.py",
        "django_ray/workflow/progress/storage.py",
        "django_ray/workflow/progress/summary.py",
    }
    _verify_canonical_module_layout(EXPECTED_FILES)
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


@pytest.mark.parametrize(
    "removed_module",
    [
        "django_ray/ray_target_probe.py",
        "django_ray/target_attestation.py",
        "django_ray/workflow/_compat.py",
        "django_ray/admin_workflow_graph.py",
        "django_ray/workflow_plans.py",
    ],
)
def test_wheel_layout_rejects_removed_private_modules(removed_module: str) -> None:
    with pytest.raises(RuntimeError, match=r"unexpected=.*" + removed_module.replace(".", r"\.")):
        _verify_canonical_module_layout(EXPECTED_FILES | {removed_module})


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
