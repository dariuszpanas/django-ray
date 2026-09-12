"""Real filesystem copy/refusal checks; no database or native runtime proof."""

from __future__ import annotations

import os

import pytest

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_steps as steps

RUN = "a" * 64


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(root))
    steps.prepare()
    artifacts.bind_artifact_store(RUN)
    (root / "inputs" / "payload.bin").write_bytes(b"original input")
    (root / "results" / "result.bin").write_bytes(b"original result")
    (root / "observations" / "old-success.json").write_text('{"task_pk":1}')
    (root / "results" / "empty").mkdir()
    return root


def test_restored_reads_are_independent_and_rollback_uses_original_final_backup(store):
    backup = artifacts.backup_artifacts("final", run_digest=RUN)
    original_digest = backup["artifacts_sha256"]
    (store / "inputs" / "payload.bin").write_bytes(b"new candidate input")
    for point in ("final", "rollback"):
        restored = artifacts.restore_artifacts(
            point, run_digest=RUN, expected_artifacts_sha256=original_digest
        )
        clone = store / ".upgrade-restores" / point
        assert (clone / "inputs" / "payload.bin").read_bytes() == b"original input"
        assert (clone / "results" / "empty").is_dir()
        assert not (clone / ".upgrade-backups").exists()
        assert restored["backup_point"] == "final"
        assert restored["complete_upgrade_gate"] is False
        assert not os.path.samefile(
            clone / "inputs" / "payload.bin", store / "inputs" / "payload.bin"
        )
    final_clone = store / ".upgrade-restores" / "final"
    (final_clone / "results" / "result.bin").write_bytes(b"corrupt clone only")
    assert (store / "results" / "result.bin").read_bytes() == b"original result"
    assert (
        store / ".upgrade-restores" / "rollback" / "results" / "result.bin"
    ).read_bytes() == b"original result"
    assert (
        artifacts._inventory(store / ".upgrade-backups" / "final" / "artifacts")
        == backup["inventory"]
    )


def test_backups_exclude_secrets_control_and_other_backup_trees(store):
    (store / "secret.txt").write_bytes(b"never copy")
    artifacts.backup_artifacts("blocked", run_digest=RUN)
    backup = artifacts.backup_artifacts("final", run_digest=RUN)
    assert all(
        item["path"].split("/")[0] in artifacts.DIRECTORIES for item in backup["inventory"]["files"]
    )
    assert not (store / ".upgrade-backups" / "final" / "artifacts" / "secret.txt").exists()


@pytest.mark.parametrize("point", ["rollback", "../final", "final/extra", "", None, True])
def test_backup_refuses_unsupported_points_before_writing(store, point):
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.backup_artifacts(point, run_digest=RUN)
    assert not list((store / ".upgrade-backups").iterdir())


def test_wrong_run_and_wrong_digest_refuse_without_copy(store):
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.backup_artifacts("final", run_digest="b" * 64)
    backup = artifacts.backup_artifacts("final", run_digest=RUN)
    assert backup["artifacts_sha256"] != "c" * 64
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.restore_artifacts("final", run_digest=RUN, expected_artifacts_sha256="c" * 64)
    assert not list((store / ".upgrade-restores").iterdir())


def test_existing_backup_or_clone_cannot_be_reused(store):
    backup = artifacts.backup_artifacts("blocked", run_digest=RUN)
    with pytest.raises(artifacts.ArtifactCopyError, match="destination-exists"):
        artifacts.backup_artifacts("blocked", run_digest=RUN)
    artifacts.restore_artifacts(
        "blocked", run_digest=RUN, expected_artifacts_sha256=backup["artifacts_sha256"]
    )
    with pytest.raises(artifacts.ArtifactCopyError, match="destination-exists"):
        artifacts.restore_artifacts(
            "blocked", run_digest=RUN, expected_artifacts_sha256=backup["artifacts_sha256"]
        )


def test_changed_backup_bytes_cannot_satisfy_original_receipt(store):
    backup = artifacts.backup_artifacts("final", run_digest=RUN)
    (store / ".upgrade-backups" / "final" / "artifacts" / "inputs" / "payload.bin").write_bytes(
        b"changed"
    )
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.restore_artifacts(
            "final", run_digest=RUN, expected_artifacts_sha256=backup["artifacts_sha256"]
        )
    assert not list((store / ".upgrade-restores").iterdir())


def test_failed_copy_retains_reservation_and_never_publishes_receipt(store, monkeypatch):
    original = artifacts._copy

    def fail_copy(source, destination, inventory):
        original(source, destination, inventory)
        raise OSError("copy completion interrupted")

    monkeypatch.setattr(artifacts, "_copy", fail_copy)
    with pytest.raises(OSError):
        artifacts.backup_artifacts("final", run_digest=RUN)
    destination = store / ".upgrade-backups" / "final"
    assert destination.is_dir()
    assert not (destination / "artifacts.json").exists()
    with pytest.raises(artifacts.ArtifactCopyError, match="destination-exists"):
        artifacts.backup_artifacts("final", run_digest=RUN)


def test_storage_budget_refuses_before_reserving_copy(store, monkeypatch):
    monkeypatch.setattr(
        artifacts.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 0})()
    )
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.backup_artifacts("final", run_digest=RUN)
    assert not list((store / ".upgrade-backups").iterdir())


@pytest.mark.parametrize(
    "bound,value",
    [("MAX_FILE_BYTES", 1), ("MAX_TOTAL_BYTES", 1), ("MAX_FILES", 1), ("MAX_DIRECTORIES", 4)],
)
def test_inventory_bounds_refuse_before_writing(store, monkeypatch, bound, value):
    monkeypatch.setattr(artifacts, bound, value)
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.backup_artifacts("final", run_digest=RUN)
    assert not list((store / ".upgrade-backups").iterdir())


def test_hard_link_source_is_refused(store):
    os.link(store / "inputs" / "payload.bin", store / "results" / "linked.bin")
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.backup_artifacts("final", run_digest=RUN)


def test_binding_nonempty_or_already_bound_store_is_refused(store):
    with pytest.raises(artifacts.ArtifactCopyError):
        artifacts.bind_artifact_store(RUN)


@pytest.mark.parametrize("change", ["access-time", "hardlink", "replacement"])
def test_read_close_metadata_accepts_access_time_but_refuses_identity_changes(
    store, monkeypatch, change
):
    path = store / "inputs" / "payload.bin"
    expected = artifacts._file_digest(path)
    original = artifacts._regular
    calls = 0

    def read_metadata(selected):
        nonlocal calls
        calls += 1
        if calls == 2:
            previous = selected.stat()
            if change == "access-time":
                os.utime(selected, ns=(previous.st_atime_ns + 1_000_000_000, previous.st_mtime_ns))
            elif change == "hardlink":
                os.link(selected, store / "new-link")
            else:
                replacement = store / "replacement"
                replacement.write_bytes(selected.read_bytes())
                os.replace(replacement, selected)
        return original(selected)

    monkeypatch.setattr(artifacts, "_regular", read_metadata)
    if change == "access-time":
        assert artifacts._file_digest(path) == expected
    else:
        with pytest.raises(artifacts.ArtifactCopyError):
            artifacts._file_digest(path)
