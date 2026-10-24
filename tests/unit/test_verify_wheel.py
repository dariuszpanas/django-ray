"""Built-distribution metadata verification tests."""

from __future__ import annotations

import io
import itertools
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_wheel
from scripts.verify_wheel import (
    EXPECTED_FILES,
    EXPECTED_MIGRATION_LEAF,
    EXPECTED_TARGET_MODULE_FILES,
    EXPECTED_WORKFLOW_MODULE_FILES,
    _verify_canonical_module_layout,
    _verify_registered_payload_purge,
    verify_distribution_archives,
)


def test_installed_request_reference_fixture_preserves_and_deletes_typed_history() -> None:
    verify_wheel._verify_rq2_reference_discriminator()


def test_release_boundary_tracks_latest_schema_migration() -> None:
    assert "django_ray/execution_codec.py" in EXPECTED_FILES
    assert "django_ray/execution_protocol.py" in EXPECTED_FILES
    assert "django_ray/ray_job_protocol.py" in EXPECTED_FILES
    assert "django_ray/ray_job_request_storage.py" in EXPECTED_FILES
    assert "django_ray/protocol_coordination.py" in EXPECTED_FILES
    assert "django_ray/protocol_status.py" in EXPECTED_FILES
    assert "django_ray/management/commands/django_ray_protocol_status.py" in EXPECTED_FILES
    assert "django_ray/management/commands/django_ray_purge_inputs.py" in EXPECTED_FILES
    assert "django_ray/management/purge_cursor.py" in EXPECTED_FILES
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
        "django_ray/workflow/progress/terminal_input.py",
        "django_ray/workflow/progress/capture_diagnostics.py",
        "django_ray/workflow/progress/reporting_diagnostics.py",
        "django_ray/workflow/progress/terminal_capture.py",
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
    assert "django_ray/migrations/0027_workflow_reporting_diagnostics.py" in EXPECTED_FILES
    assert "django_ray/migrations/0028_worker_lease_policy.py" in EXPECTED_FILES
    assert EXPECTED_MIGRATION_LEAF == (
        "django_ray",
        "0028_worker_lease_policy",
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


def _metadata(
    *,
    ray_requirement: str = "ray[default]>=2.58.0",
    transitive_requirements: tuple[str, ...] = (
        "certifi>=2026.10.19",
        "filelock>=4.2.1",
        "multidict>=6.9.1",
        "opentelemetry-api>=1.45.1",
        "urllib3>=2.8.0",
        "virtualenv>=21.14.10",
    ),
) -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        "Name: django-ray\n"
        "Version: 0.4.0\n"
        f"Requires-Dist: {ray_requirement}\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in transitive_requirements)
        + "\n"
    ).encode()


def _write_distributions(
    dist_dir: Path,
    *,
    wheel_requirement: str = "ray[default]>=2.58.0",
    sdist_requirement: str = "ray[default]>=2.58.0",
    wheel_transitives: tuple[str, ...] = (
        "certifi>=2026.10.19",
        "filelock>=4.2.1",
        "multidict>=6.9.1",
        "opentelemetry-api>=1.45.1",
        "urllib3>=2.8.0",
        "virtualenv>=21.14.10",
    ),
    sdist_transitives: tuple[str, ...] = (
        "certifi>=2026.10.19",
        "filelock>=4.2.1",
        "multidict>=6.9.1",
        "opentelemetry-api>=1.45.1",
        "urllib3>=2.8.0",
        "virtualenv>=21.14.10",
    ),
) -> None:
    wheel = dist_dir / "django_ray-0.4.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "django_ray-0.4.0.dist-info/METADATA",
            _metadata(ray_requirement=wheel_requirement, transitive_requirements=wheel_transitives),
        )

    sdist = dist_dir / "django_ray-0.4.0.tar.gz"
    payload = _metadata(
        ray_requirement=sdist_requirement, transitive_requirements=sdist_transitives
    )
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


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    ("name", "floor", "old_floor"),
    [
        ("certifi", "2026.10.19", "2026.6.17"),
        ("filelock", "4.2.1", "4.2.0"),
        ("multidict", "6.9.1", "6.9.0"),
        ("opentelemetry-api", "1.45.1", "1.45.0"),
        ("urllib3", "2.8.0", "2.7.0"),
        ("virtualenv", "21.14.10", "21.14.9"),
    ],
)
@pytest.mark.parametrize("defect", ["missing", "old", "optional", "duplicate"])
def test_distribution_archives_reject_vulnerable_transitive_requirements(
    tmp_path: Path, artifact: str, name: str, floor: str, old_floor: str, defect: str
) -> None:
    requirements = [
        "certifi>=2026.10.19",
        "filelock>=4.2.1",
        "multidict>=6.9.1",
        "opentelemetry-api>=1.45.1",
        "urllib3>=2.8.0",
        "virtualenv>=21.14.10",
    ]
    requirements.remove(f"{name}>={floor}")
    if defect == "old":
        requirements.append(f"{name}>={old_floor}")
    elif defect == "optional":
        requirements.append(f'{name}>={floor}; extra == "sample"')
    elif defect == "duplicate":
        requirements.extend([f"{name}>={floor}"] * 2)
    if artifact == "wheel":
        _write_distributions(tmp_path, wheel_transitives=tuple(requirements))
    else:
        _write_distributions(tmp_path, sdist_transitives=tuple(requirements))

    with pytest.raises(RuntimeError, match=f"unconditional {name}"):
        verify_distribution_archives(tmp_path, "0.4.0")


@pytest.fixture
def leading_dash_purge_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    from django_ray.management import purge_cursor

    counter = itertools.count()

    def token_bytes(size: int) -> bytes:
        assert size == 12
        return b"\xfb" + next(counter).to_bytes(11, "big")

    # Replace this module's randomness provider, without changing global secrets.
    monkeypatch.setattr(purge_cursor, "secrets", SimpleNamespace(token_bytes=token_bytes))


@pytest.mark.usefixtures("leading_dash_purge_nonce")
def test_purge_cursor_equals_argument_accepts_authenticated_leading_dash_token() -> None:
    from django.core.management.base import CommandError
    from django.utils import timezone

    from django_ray.management.commands.django_ray_purge_inputs import Command
    from django_ray.management.purge_cursor import (
        PurgeCursor,
        decode_purge_cursor,
        encode_purge_cursor,
    )

    original = PurgeCursor("fixture", timezone.now(), 30, False)
    token = encode_purge_cursor(original)
    assert token.startswith("-")
    assert decode_purge_cursor(token) == original
    parser = Command().create_parser("manage.py", "django_ray_purge_inputs")
    with pytest.raises(CommandError, match="expected one argument"):
        parser.parse_args(["--cursor", token])
    options = parser.parse_args([f"--cursor={token}"])
    assert decode_purge_cursor(options.cursor) == original


@pytest.mark.django_db
@pytest.mark.usefixtures("leading_dash_purge_nonce")
@pytest.mark.parametrize("fail_deletion", [False, True])
def test_purge_wheel_smoke_cleans_owned_fixture_after_success_or_partial_deletion_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, fail_deletion: bool
) -> None:
    from django.db import connection

    from django_ray.models import RayTaskExecution, TaskInputPayload
    from django_ray.result_storage import FilesystemResultStorage

    if connection.vendor != "sqlite":
        pytest.skip("The installed-wheel fixture explicitly owns an in-memory SQLite database")
    created_directories = []
    original_directory = verify_wheel.TemporaryDirectory

    def owned_directory(**kwargs):
        directory = original_directory(dir=tmp_path, **kwargs)
        created_directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(verify_wheel, "TemporaryDirectory", owned_directory)
    if fail_deletion:
        original_delete = FilesystemResultStorage.delete
        calls = 0

        def fail_second_delete(self, *, reference):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixed wheel smoke failure")
            return original_delete(self, reference=reference)

        monkeypatch.setattr(FilesystemResultStorage, "delete", fail_second_delete)
        with pytest.raises(RuntimeError, match="report or continuation contract"):
            _verify_registered_payload_purge()
        assert calls == 2
        assert "Verified installed bounded purge" not in capsys.readouterr().out
    else:
        _verify_registered_payload_purge()
        assert "Verified installed bounded purge" in capsys.readouterr().out
    assert not TaskInputPayload.objects.exists()
    assert not RayTaskExecution.objects.exists()
    assert len(created_directories) == 1
    assert not created_directories[0].exists()


def test_purge_wheel_smoke_refuses_non_memory_database_before_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from django.db import connection

    monkeypatch.setitem(connection.settings_dict, "NAME", str(tmp_path / "not-owned.sqlite3"))
    with pytest.raises(RuntimeError, match="isolated in-memory SQLite"):
        _verify_registered_payload_purge()
    assert not (tmp_path / "not-owned.sqlite3").exists()
