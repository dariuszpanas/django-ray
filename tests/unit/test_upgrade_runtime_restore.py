"""Scratch-only restore boundaries with fake clients, never native SQL proof."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_database as database
from qualification.upgrade import runtime_restore as restore
from qualification.upgrade import runtime_steps as steps

RUN = "a" * 64
SYSTEM = "7450000000000000001"
PRIMARY_OID = 16384
SCRATCH_OID = 16385


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(root))
    steps.prepare()
    artifacts.bind_artifact_store(RUN)
    (root / "inputs" / "old.bin").write_bytes(b"old original input")
    connection = database._Connection("postgres", "5432", "owner", "primary", "scratch", "private")
    monkeypatch.setattr(database, "_connection", lambda: connection)
    monkeypatch.setattr(database.platform, "system", lambda: "Linux")
    monkeypatch.setattr(database, "POSTGRES_BIN", PurePosixPath("/usr/lib/postgresql/17/bin"))
    temporary = tempfile.TemporaryDirectory
    monkeypatch.setattr(
        database.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: temporary(prefix=kwargs["prefix"], dir=tmp_path),
    )
    calls = []

    def client(command, **kwargs):
        calls.append((command, kwargs))
        assert "private" not in repr(command)
        assert "private" not in repr(kwargs["environment"])
        assert Path(kwargs["environment"]["PGPASSFILE"]).is_file()
        if command[0].endswith("/pg_dump"):
            kwargs["output"].write(b"PGDMP-fake-test-only-database")
            return b""
        if command[0].endswith("/pg_restore"):
            assert "--dbname=scratch" in command
            assert "--dbname=primary" not in command
            assert "--single-transaction" in command
            assert not {"--clean", "--create"}.intersection(command)
            return b""
        assert command[0].endswith("/psql")
        if "--command=" + restore._EMPTY_SQL in command:
            return b"t\tt\tt\tt\tt\tt\tt\tt\n"
        if "--command=" + restore._NO_OTHER_SESSIONS_SQL in command:
            return b"t\n"
        assert "--command=" + database._IDENTITY_SQL in command
        name, oid = (
            ("scratch", SCRATCH_OID) if "--dbname=scratch" in command else ("primary", PRIMARY_OID)
        )
        return f"{SYSTEM}\t{oid}\t{name}\t170011\tf\towner\n".encode()

    monkeypatch.setattr(database, "_run_client", client)
    backups = {}
    for point in ("blocked", "final"):
        artifact = artifacts.backup_artifacts(point, run_digest=RUN)
        backups[point] = database.backup_database(
            point,
            run_digest=RUN,
            expected_artifacts_sha256=artifact["artifacts_sha256"],
            expected_system_identifier=SYSTEM,
            expected_primary_database_oid=PRIMARY_OID,
        )
    for point in ("blocked", "final", "rollback"):
        artifacts.restore_artifacts(
            point,
            run_digest=RUN,
            expected_artifacts_sha256=backups["final" if point == "rollback" else point][
                "artifacts_sha256"
            ],
        )
    calls.clear()
    return SimpleNamespace(root=root, calls=calls, client=client, backups=backups)


def restore_point(store, point="final", **changes):
    backup = store.backups[
        "final" if point == "rollback" else "blocked" if point == "blocked" else "final"
    ]
    arguments = {
        "run_digest": RUN,
        "expected_artifacts_sha256": backup["artifacts_sha256"],
        "expected_dump_sha256": backup["dump_sha256"],
        "expected_system_identifier": SYSTEM,
        "expected_primary_database_oid": PRIMARY_OID,
        "expected_scratch_database_oid": SCRATCH_OID,
    }
    arguments.update(changes)
    return restore.restore_database(point, **arguments)


@pytest.mark.parametrize("point", ["blocked", "final", "rollback"])
def test_fixed_restore_only_writes_observed_scratch_and_publishes_after_verification(store, point):
    result = restore_point(store, point)
    assert result["scratch_database"] == "scratch"
    assert result["scratch_database_oid"] == SCRATCH_OID
    assert result["backup_point"] == ("final" if point == "rollback" else point)
    assert result["transactional_restore_completed"] is True
    assert result["released_history_read_verified"] is result["complete_upgrade_gate"] is False
    assert (
        steps._read(store.root / ".upgrade-control" / (point + "-database-restored.json")) == result
    )
    restores = [call for call in store.calls if call[0][0].endswith("/pg_restore")]
    assert len(restores) == 1
    command, options = restores[0]
    assert options["timeout"] == 60
    assert options["maximum"] == database.MAX_DIAGNOSTIC_BYTES
    assert "--no-owner" in command and "--no-acl" in command and "--no-tablespaces" in command
    assert "default_transaction_read_only=off" in options["environment"]["PGOPTIONS"]
    assert all(
        "default_transaction_read_only=on" in options["environment"]["PGOPTIONS"]
        for command, options in store.calls
        if command[0].endswith("/psql")
    )
    assert all(not Path(options["directory"]).exists() for _, options in store.calls)


def test_rollback_uses_final_backup_despite_new_primary_artifact_writes(store):
    (store.root / "inputs" / "new.bin").write_bytes(b"candidate-only input")
    result = restore_point(store, "rollback")
    assert result["backup_point"] == "final"
    assert (store.root / "inputs" / "new.bin").read_bytes() == b"candidate-only input"
    assert not (store.root / ".upgrade-restores" / "rollback" / "inputs" / "new.bin").exists()


@pytest.mark.parametrize("point", ["primary", "../final", "", None, True])
def test_unknown_restore_point_refused_before_any_client(store, point):
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store, point)
    assert store.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_scratch_database_oid": PRIMARY_OID},
        {"expected_scratch_database_oid": True},
        {"expected_scratch_database_oid": 0},
        {"expected_scratch_database_oid": 2**32},
        {"expected_system_identifier": "01"},
        {"expected_dump_sha256": "wrong"},
        {"run_digest": "b" * 64},
    ],
)
def test_invalid_identity_refused_before_any_client(store, changes):
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store, **changes)
    assert store.calls == []


def test_changed_scratch_incarnation_refused_before_mutation(store):
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store, expected_scratch_database_oid=SCRATCH_OID + 1)
    assert all(not command[0].endswith("/pg_restore") for command, _ in store.calls)


@pytest.mark.parametrize("column", range(8))
def test_nonempty_or_busy_scratch_cannot_be_restored_over(store, monkeypatch, column):
    def client(command, **kwargs):
        if "--command=" + restore._EMPTY_SQL in command:
            fields = ["t"] * 8
            fields[column] = "f"
            return ("\t".join(fields) + "\n").encode()
        return store.client(command, **kwargs)

    monkeypatch.setattr(database, "_run_client", client)
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store)
    assert all(not command[0].endswith("/pg_restore") for command, _ in store.calls)
    assert not (store.root / ".upgrade-control" / "final-database-restore-reserved.json").exists()


def test_lost_restore_outcome_retains_reservation_and_cannot_retry(store, monkeypatch):
    def client(command, **kwargs):
        if command[0].endswith("/pg_restore"):
            raise OSError("private provider diagnostic must not escape")
        return store.client(command, **kwargs)

    monkeypatch.setattr(database, "_run_client", client)
    with pytest.raises(restore.DatabaseRestoreError, match="^upgrade-database-restore-refused$"):
        restore_point(store)
    assert (store.root / ".upgrade-control" / "final-database-restore-reserved.json").exists()
    assert not (store.root / ".upgrade-control" / "final-database-restored.json").exists()
    monkeypatch.setattr(database, "_run_client", store.client)
    store.calls.clear()
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store)
    assert all(not command[0].endswith("/pg_restore") for command, _ in store.calls)


@pytest.mark.parametrize("part", ["dump", "clone", "receipt"])
def test_corrupt_or_unverified_restore_inputs_refuse_before_mutation(store, part):
    if part == "dump":
        (store.root / ".upgrade-backups" / "final" / "database.dump").write_bytes(b"PGDMP-corrupt")
    elif part == "clone":
        (store.root / ".upgrade-restores" / "final" / "inputs" / "old.bin").write_bytes(b"changed")
    else:
        (store.root / ".upgrade-control" / "final-artifacts-restored.json").unlink()
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store)
    assert all(not command[0].endswith("/pg_restore") for command, _ in store.calls)


def test_non_linux_refuses_before_clients_or_reservation(store, monkeypatch):
    monkeypatch.setattr(restore.platform, "system", lambda: "Windows")
    with pytest.raises(restore.DatabaseRestoreError):
        restore_point(store)
    assert store.calls == []
