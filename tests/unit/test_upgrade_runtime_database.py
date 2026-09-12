"""Resource-free backup boundaries with fake PostgreSQL/process observations.

Actual files exercise reservation and copy binding. No pg_dump, psql, PostgreSQL
or Ray process runs, and successful adapter tests are not native backup proof.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_database as database
from qualification.upgrade import runtime_steps as steps

RUN = "a" * 64
SYSTEM = "7450000000000000001"
OID = 16384
IDENTITY = {"system_identifier": SYSTEM, "primary_database_oid": OID, "server_version": "170011"}
PASSWORD = "fixture:password\\with-delimiters"
CONNECTION = database._Connection(
    "upgrade-postgres", "5432", "upgrade_owner", "primary", "scratch", PASSWORD
)
DUMP = b"PGDMP\x01fixture-only-fake-dump"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    for name in artifacts.DIRECTORIES:
        (root / name).mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(root))
    artifacts.bind_artifact_store(RUN)
    (root / "inputs" / "fixture.bin").write_bytes(b"unchanged application input")
    receipt = artifacts.backup_artifacts("blocked", run_digest=RUN)
    original = tempfile.TemporaryDirectory
    monkeypatch.setattr(
        database.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: original(prefix=kwargs["prefix"], dir=tmp_path),
    )
    monkeypatch.setattr(database.platform, "system", lambda: "Linux")
    monkeypatch.setattr(database, "POSTGRES_BIN", PurePosixPath("/usr/lib/postgresql/17/bin"))
    monkeypatch.setattr(database, "_connection", lambda: CONNECTION)
    calls = []

    def client(command, **kwargs):
        calls.append((command, kwargs))
        passfile = Path(kwargs["environment"]["PGPASSFILE"])
        assert passfile.exists()
        assert PASSWORD not in repr(command) and PASSWORD not in repr(kwargs["environment"])
        assert not (root / ".upgrade-backups" / "blocked" / "database.json").exists()
        if command[0].endswith("/psql"):
            return f"{SYSTEM}\t{OID}\tprimary\t170011\tf\tupgrade_owner\n".encode()
        assert command[0] == "/usr/lib/postgresql/17/bin/pg_dump"
        kwargs["output"].write(DUMP)
        return b""

    monkeypatch.setattr(database, "_run_client", client)
    return SimpleNamespace(root=root, receipt=receipt, calls=calls, client=client)


def backup(store, **changes):
    arguments = {
        "point": "blocked",
        "run_digest": RUN,
        "expected_artifacts_sha256": store.receipt["artifacts_sha256"],
        "expected_system_identifier": SYSTEM,
        "expected_primary_database_oid": OID,
    }
    arguments.update(changes)
    return database.backup_database(**arguments)


@pytest.mark.parametrize("point", ["blocked", "final"])
def test_fixed_dump_receipt_requires_two_identity_reads_and_complete_artifact_copy(store, point):
    if point == "final":
        store.receipt = artifacts.backup_artifacts("final", run_digest=RUN)
    result = backup(store, point=point)
    destination = store.root / ".upgrade-backups" / point
    assert steps._read(destination / "database.json") == result
    assert result == {
        "schema": 1,
        "run_digest": RUN,
        "backup_point": point,
        "artifacts_sha256": store.receipt["artifacts_sha256"],
        "postgresql": IDENTITY,
        "database": "primary",
        "format": "custom",
        "dump_bytes": len(DUMP),
        "dump_sha256": hashlib.sha256(DUMP).hexdigest(),
        "complete_upgrade_gate": False,
    }
    assert (destination / "database.dump").read_bytes() == DUMP
    assert [Path(call[0][0]).name for call in store.calls] == ["psql", "pg_dump", "psql"]
    first, dump, final = store.calls
    assert first[0] == final[0]
    assert "--no-psqlrc" in first[0] and "--set=ON_ERROR_STOP=1" in first[0]
    assert "--command=" + database._IDENTITY_SQL in first[0]
    assert dump[0] == (
        "/usr/lib/postgresql/17/bin/pg_dump",
        "--format=custom",
        "--encoding=UTF8",
        "--lock-wait-timeout=5000",
        "--host=upgrade-postgres",
        "--port=5432",
        "--username=upgrade_owner",
        "--dbname=primary",
        "--no-password",
    )
    assert dump[1]["timeout"] == 60 and dump[1]["maximum"] == 64 * 1024 * 1024
    assert all(not Path(call[1]["directory"]).exists() for call in store.calls)
    assert PASSWORD not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"point": "rollback"},
        {"point": "../final"},
        {"point": True},
        {"run_digest": "b" * 64},
        {"expected_artifacts_sha256": "b" * 64},
        {"expected_system_identifier": "01"},
        {"expected_system_identifier": 1},
        {"expected_system_identifier": str(2**64)},
        {"expected_primary_database_oid": True},
        {"expected_primary_database_oid": 0},
        {"expected_primary_database_oid": 2**32},
    ],
)
def test_invalid_or_cross_bound_inputs_refuse_before_client(store, changes):
    with pytest.raises(database.DatabaseBackupError):
        backup(store, **changes)
    assert store.calls == []
    assert not (store.root / ".upgrade-backups" / "blocked" / "database.dump").exists()


def test_native_operation_is_refused_off_linux_before_configuration(store, monkeypatch):
    monkeypatch.setattr(database.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        database, "_connection", lambda: pytest.fail("no settings or Secret access")
    )
    with pytest.raises(database.DatabaseBackupError):
        backup(store)
    assert store.calls == []


@pytest.mark.parametrize("which", ["root", "backup", "receipt", "incomplete"])
def test_artifact_change_or_incomplete_copy_refuses_before_client(store, which):
    destination = store.root / ".upgrade-backups" / "blocked"
    if which == "root":
        (store.root / "inputs" / "fixture.bin").write_bytes(b"changed")
    elif which == "backup":
        (destination / "artifacts" / "inputs" / "fixture.bin").write_bytes(b"changed")
    elif which == "receipt":
        (destination / "artifacts.json").write_bytes(b"{}")
    else:
        (destination / "artifacts.json").unlink()
    with pytest.raises(database.DatabaseBackupError):
        backup(store)
    assert store.calls == []


@pytest.mark.parametrize(
    "kind", ["identity-before", "identity-after", "artifact-after", "dump", "magic"]
)
def test_failed_backup_keeps_reservation_without_completion_or_replay(store, monkeypatch, kind):
    calls = 0

    def client(command, **kwargs):
        nonlocal calls
        calls += 1
        result = store.client(command, **kwargs)
        if (kind == "identity-before" and calls == 1) or (kind == "identity-after" and calls == 3):
            return result.replace(SYSTEM.encode(), b"7450000000000000002")
        if calls == 2:
            if kind == "dump":
                raise OSError(PASSWORD)
            if kind == "artifact-after":
                (store.root / "inputs" / "fixture.bin").write_bytes(b"changed")
            if kind == "magic":
                kwargs["output"].seek(0)
                kwargs["output"].write(b"wrong")
        return result

    monkeypatch.setattr(database, "_run_client", client)
    with pytest.raises(database.DatabaseBackupError) as error:
        backup(store)
    assert PASSWORD not in str(error.value)
    destination = store.root / ".upgrade-backups" / "blocked"
    assert not (destination / "database.json").exists()
    assert (destination / "database.dump").exists() == (kind != "identity-before")
    assert all(not Path(call[1]["directory"]).exists() for call in store.calls)
    if kind != "identity-before":
        count = len(store.calls)
        with pytest.raises(database.DatabaseBackupError):
            backup(store)
        assert len(store.calls) == count


def test_completed_backup_cannot_be_overwritten(store):
    backup(store)
    before = len(store.calls)
    with pytest.raises(database.DatabaseBackupError):
        backup(store)
    assert len(store.calls) == before


def test_disk_budget_refuses_before_client_and_dump_reservation(store, monkeypatch):
    monkeypatch.setattr(database.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(database.DatabaseBackupError):
        backup(store)
    assert store.calls == []
    assert not (store.root / ".upgrade-backups" / "blocked" / "database.dump").exists()


def test_password_file_escaping_and_child_environment_do_not_inherit_routing(tmp_path, monkeypatch):
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGDATABASE", "PGPASSWORD", "LD_PRELOAD"):
        monkeypatch.setenv(name, "untrusted-routing-or-secret")
    environment = database._client_environment(CONNECTION, tmp_path)
    assert set(environment) == {
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "PGPASSFILE",
        "PGCONNECT_TIMEOUT",
        "PGCLIENTENCODING",
        "PGSSLMODE",
        "PGGSSENCMODE",
        "PGOPTIONS",
    }
    assert "untrusted-routing-or-secret" not in repr(environment)
    assert PASSWORD not in repr(environment) and PASSWORD not in repr(CONNECTION)
    assert (tmp_path / "pgpass").read_text() == (
        "upgrade-postgres:5432:primary:upgrade_owner:fixture\\:password\\\\with-delimiters\n"
    )
    if os.name == "posix":
        assert (tmp_path / "pgpass").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        database._client_environment(CONNECTION, tmp_path)


@pytest.mark.parametrize("change", ["access-time", "hard-link", "replacement", "content"])
def test_dump_hash_rechecks_stable_file_identity_without_rejecting_read_metadata(
    tmp_path, monkeypatch, change
):
    dump = tmp_path / "database.dump"
    dump.write_bytes(DUMP)
    original = Path.lstat
    calls = 0

    def checked(path, *args, **kwargs):
        nonlocal calls
        if path == dump:
            calls += 1
            if calls == 2:
                metadata = original(path)
                if change == "access-time":
                    # Some filesystems publish read metadata only on handle close.
                    os.utime(path, ns=(metadata.st_atime_ns + 1_000_000_000, metadata.st_mtime_ns))
                elif change == "hard-link":
                    os.link(path, tmp_path / "unexpected-link")
                elif change == "replacement":
                    replacement = tmp_path / "replacement"
                    replacement.write_bytes(DUMP)
                    os.replace(replacement, path)
                else:
                    path.write_bytes(DUMP + b"changed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", checked)
    if change == "access-time":
        assert database._dump_identity(dump) == (len(DUMP), hashlib.sha256(DUMP).hexdigest())
    else:
        with pytest.raises(database.DatabaseBackupError):
            database._dump_identity(dump)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"secret provider error",
        b"1\t2\tprimary\t170011\tf\tupgrade_owner\nextra",
        b"1\t2\tscratch\t170011\tf\tupgrade_owner\n",
        b"1\t2\tprimary\t180001\tf\tupgrade_owner\n",
        b"1\t2\tprimary\t170011\tt\tupgrade_owner\n",
        b"1\t2\tprimary\t170011\tf\tother_user\n",
        b"18446744073709551616\t2\tprimary\t170011\tf\tupgrade_owner\n",
    ],
)
def test_identity_parser_rejects_wrong_database_role_version_and_provider_text(
    raw, monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "_run_client", lambda *args, **kwargs: raw)
    with pytest.raises(database.DatabaseBackupError, match="identity-refused"):
        database._identity(CONNECTION, tmp_path, {})


class Pipe:
    def __init__(self, number):
        self.number = number
        self.closed = False

    def fileno(self):
        return self.number

    def close(self):
        self.closed = True


@pytest.fixture
def process_case(monkeypatch):
    case = SimpleNamespace(
        blocks={10: [b"PGDMP123", b""], 11: [b""]},
        clock=0.0,
        stalled=False,
        code=0,
        wait_failure=False,
        killed=[],
        waits=[],
        launches=[],
    )

    class Process:
        pid = 31415
        stdout = Pipe(10)
        stderr = Pipe(11)

        def wait(self, *, timeout):
            case.waits.append(timeout)
            if case.wait_failure and not case.killed:
                raise subprocess.TimeoutExpired("fixed-test-command", timeout)
            return case.code

    case.process = Process()

    def launch(command, **kwargs):
        case.launches.append((command, kwargs))
        return case.process

    class Selector:
        def __init__(self):
            self.mapping = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def register(self, pipe, _events, name):
            self.mapping[pipe.number] = SimpleNamespace(fd=pipe.number, fileobj=pipe, data=name)

        def unregister(self, pipe):
            self.mapping.pop(pipe.number)

        def get_map(self):
            return self.mapping

        def select(self, timeout):
            case.clock += timeout
            return [] if case.stalled else [(key, 1) for key in self.mapping.values()]

    monkeypatch.setattr(database.subprocess, "Popen", launch)
    monkeypatch.setattr(database.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(database.os, "set_blocking", lambda *_: None)
    monkeypatch.setattr(database.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(database.os, "read", lambda fd, _size: case.blocks[fd].pop(0))
    monkeypatch.setattr(
        database.os, "killpg", lambda pid, signal: case.killed.append((pid, signal)), raising=False
    )
    monkeypatch.setattr(database.time, "monotonic", lambda: case.clock)
    return case


def run_process(tmp_path, *, maximum=8, output=None):
    return database._run_client(
        ("/usr/lib/postgresql/17/bin/pg_dump", "--format=custom"),
        directory=tmp_path,
        environment={"HOME": str(tmp_path)},
        timeout=1,
        maximum=maximum,
        output=output,
    )


def test_pipe_capture_is_bounded_without_threads_or_shell_and_waits_only_to_deadline(
    process_case, tmp_path
):
    output = io.BytesIO()
    assert run_process(tmp_path, output=output) == b""
    assert output.getvalue() == b"PGDMP123"
    assert process_case.killed == []
    assert 0 < process_case.waits[0] < 1
    options = process_case.launches[0][1]
    assert options["shell"] is False and options["start_new_session"] is True
    assert options["close_fds"] is True and options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == options["stderr"] == subprocess.PIPE
    assert process_case.process.stdout.closed and process_case.process.stderr.closed


@pytest.mark.parametrize(
    "kind", ["stdout", "stderr", "timeout", "wait-timeout", "clock", "exit", "warning"]
)
def test_output_deadline_clock_and_failed_exit_never_accept_process(process_case, tmp_path, kind):
    if kind == "stdout":
        process_case.blocks[10] = [b"123456789"]
    elif kind == "stderr":
        process_case.blocks[11] = [b"s" * (database.MAX_DIAGNOSTIC_BYTES + 1)]
    elif kind == "timeout":
        process_case.stalled = True
    elif kind == "wait-timeout":
        process_case.wait_failure = True
    elif kind == "clock":
        process_case.clock = float("nan")
    elif kind == "exit":
        process_case.code = 1
    else:
        process_case.blocks[11] = [b"secret warning", b""]
    output = io.BytesIO()
    with pytest.raises((database.DatabaseBackupError, subprocess.TimeoutExpired)) as error:
        run_process(tmp_path, output=output)
    assert "secret warning" not in str(error.value)
    assert len(output.getvalue()) <= 8
    if kind == "clock":
        assert process_case.launches == []
    else:
        assert process_case.process.stdout.closed and process_case.process.stderr.closed
        if kind not in {"exit", "warning"}:
            assert process_case.killed == [(31415, database.signal.SIGKILL)]
            assert process_case.waits[-1] == database.CLEANUP_TIMEOUT


def test_uncertain_cleanup_is_fixed_refusal_not_a_success(process_case, tmp_path, monkeypatch):
    process_case.stalled = True

    def cannot_reap(*, timeout):
        raise subprocess.TimeoutExpired("fixed-client", timeout)

    monkeypatch.setattr(process_case.process, "wait", cannot_reap)
    with pytest.raises(database.DatabaseBackupError, match="cleanup-unconfirmed"):
        run_process(tmp_path)
    assert process_case.killed


def test_failed_signal_still_attempts_bounded_reap_without_claiming_cleanup(
    process_case, tmp_path, monkeypatch
):
    process_case.stalled = True

    def denied(*args):
        raise PermissionError("provider detail must not escape")

    monkeypatch.setattr(database.os, "killpg", denied)
    with pytest.raises(database.DatabaseBackupError, match="cleanup-unconfirmed"):
        run_process(tmp_path)
    assert process_case.waits == [database.CLEANUP_TIMEOUT]


@pytest.mark.parametrize("change", ["candidate", "scratch", "package", "ray"])
def test_only_actual_released_runtime_primary_may_reach_configuration_builder(monkeypatch, change):
    import django_ray

    calls = []
    fake = SimpleNamespace(build_runtime_settings=lambda *args, **kwargs: calls.append(args))
    monkeypatch.setitem(sys.modules, "qualification.upgrade.runtime_settings", fake)
    monkeypatch.setenv(
        "DJANGO_RAY_UPGRADE_BUILD", "candidate" if change == "candidate" else "baseline"
    )
    monkeypatch.setenv(
        "DJANGO_RAY_UPGRADE_DATABASE", "scratch" if change == "scratch" else "primary"
    )
    monkeypatch.setattr(django_ray, "__version__", "0.5.0" if change == "package" else "0.4.0")
    versions = {
        "django-ray": django_ray.__version__,
        "ray": "2.58.0" if change == "ray" else "2.56.0",
    }
    monkeypatch.setattr(database, "version", versions.__getitem__)
    with pytest.raises(database.DatabaseBackupError):
        database._connection()
    assert calls == []
