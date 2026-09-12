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
    assert "django_ray/runtime/cohort_job.py" in EXPECTED_FILES
    assert "django_ray/runtime/cohort_job_entrypoint.py" in EXPECTED_FILES
    assert {
        "django_ray/maintenance.py",
        "django_ray/management/commands/django_ray_maintenance.py",
        "django_ray/runtime/cohort_entrypoint.py",
        "django_ray/runtime/cohort_execution.py",
        "django_ray/runtime/cohort_nested.py",
        "django_ray/runner/cohort_claims.py",
        "django_ray/runner/cohort_cancel_request.py",
        "django_ray/runner/cohort_cancellation.py",
        "django_ray/runner/cohort_cleanup_recovery.py",
        "django_ray/runner/cohort_completion.py",
        "django_ray/runner/cohort_connection.py",
        "django_ray/runner/cohort_dispatch.py",
        "django_ray/runner/cohort_expiration.py",
        "django_ray/runner/cohort_job_execution_control.py",
        "django_ray/runner/cohort_recovery.py",
        "django_ray/runner/cohort_timeout.py",
        "django_ray/runner/cohort_worker.py",
    } <= EXPECTED_FILES
    assert "django_ray/runtime_env_transport.py" in EXPECTED_FILES

    assert EXPECTED_TARGET_MODULE_FILES == {
        "django_ray/target/__init__.py",
        "django_ray/target/attestation.py",
        "django_ray/target/capabilities.py",
        "django_ray/target/cohort_claim.py",
        "django_ray/target/cohort_claim_storage.py",
        "django_ray/target/cohort_contract.py",
        "django_ray/target/cohort_intent.py",
        "django_ray/target/cohort_intent_storage.py",
        "django_ray/target/cohort_job_cleanup.py",
        "django_ray/target/cohort_job_control.py",
        "django_ray/target/cohort_job_retirement.py",
        "django_ray/target/cohort_job_http.py",
        "django_ray/target/cohort_job_receipt.py",
        "django_ray/target/cohort_job_receipt_storage.py",
        "django_ray/target/cohort_probe.py",
        "django_ray/target/cohort_probe_challenges.py",
        "django_ray/target/cohort_publication.py",
        "django_ray/target/cohort_runtime.py",
        "django_ray/target/cohort_sync.py",
        "django_ray/target/cohort_transport.py",
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
    assert "django_ray/migrations/0027_ray_target_probe_challenges.py" in EXPECTED_FILES
    assert "django_ray/migrations/0028_ray_task_cohort_intent.py" in EXPECTED_FILES
    assert "django_ray/migrations/0029_cohort_job_receipts.py" in EXPECTED_FILES
    assert "django_ray/migrations/0030_cohort_claims.py" in EXPECTED_FILES
    assert "django_ray/migrations/0031_maintenance_admission.py" in EXPECTED_FILES
    assert "django_ray/migrations/0032_maintenance_controls.py" in EXPECTED_FILES
    assert "django_ray/migrations/0033_cohort_job_cleanup.py" in EXPECTED_FILES
    assert "django_ray/migrations/0034_cohort_timeouts.py" in EXPECTED_FILES
    assert EXPECTED_MIGRATION_LEAF == (
        "django_ray",
        "0034_cohort_timeouts",
    )


def test_source_package_matches_installed_wheel_layout_contract() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    files = {
        path.relative_to(source_root).as_posix()
        for path in (source_root / "django_ray").rglob("*.py")
    }
    _verify_canonical_module_layout(files)
    assert {path for path in EXPECTED_FILES if path.endswith(".py")} <= files


def test_source_migration_graph_matches_installed_wheel_leaf_without_database() -> None:
    from django.db.migrations.loader import MigrationLoader

    # Loading from disk with no connection catches new leaves and divergent
    # branches without creating a database or running a migration on the host.
    graph = MigrationLoader(None).graph
    assert set(graph.leaf_nodes("django_ray")) == {EXPECTED_MIGRATION_LEAF}


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


def _metadata(*, ray_requirement: str = "ray[default]>=2.58.0") -> bytes:
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
    wheel_requirement: str = "ray[default]>=2.58.0",
    sdist_requirement: str = "ray[default]>=2.58.0",
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
@pytest.mark.parametrize("old_floor", ["2.53.0", "2.56.0", "2.57.0"])
def test_distribution_archives_reject_a_pre_floor_ray_requirement(
    tmp_path: Path,
    artifact: str,
    old_floor: str,
) -> None:
    requirements = {
        "wheel_requirement": "ray[default]>=2.58.0",
        "sdist_requirement": "ray[default]>=2.58.0",
    }
    requirements[f"{artifact}_requirement"] = f"ray[default]>={old_floor}"
    _write_distributions(tmp_path, **requirements)

    with pytest.raises(RuntimeError, match=r"ray\[default\]>=2\.58\.0"):
        verify_distribution_archives(tmp_path, "0.4.0")
