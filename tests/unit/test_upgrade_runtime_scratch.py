"""Real reservation files with fake PostgreSQL clients; no native SQL proof."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_database as database
from qualification.upgrade import runtime_restore as restore
from qualification.upgrade import runtime_scratch as scratch
from qualification.upgrade import runtime_steps as steps

RUN = "a" * 64
SYSTEM = "7450000000000000001"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(root))
    steps.prepare()
    artifacts.bind_artifact_store(RUN)
    connection = database._Connection("postgres", "5432", "owner", "primary", "scratch", "secret")
    monkeypatch.setattr(database, "_connection", lambda: connection)
    monkeypatch.setattr(scratch.platform, "system", lambda: "Linux")
    monkeypatch.setattr(database, "POSTGRES_BIN", PurePosixPath("/usr/lib/postgresql/17/bin"))
    temporary = tempfile.TemporaryDirectory
    monkeypatch.setattr(
        scratch.tempfile,
        "TemporaryDirectory",
        lambda **kw: temporary(prefix=kw["prefix"], dir=tmp_path),
    )
    state = SimpleNamespace(root=root, calls=[], exists=False, fail=None, connection=connection)

    def client(command, **options):
        state.calls.append((command, options))
        assert "secret" not in repr(command) + repr(options)
        assert Path(options["environment"]["PGPASSFILE"]).is_file()
        if command[0].endswith("/createdb"):
            assert (root / ".upgrade-control" / "blocked-scratch-create-reserved.json").is_file()
            assert not state.exists
            state.exists = True
            if state.fail == "lost-response":
                raise RuntimeError("secret provider diagnostics")
            return b""
        if "--command=" + database._IDENTITY_SQL in command:
            is_scratch = "--dbname=scratch" in command
            assert not is_scratch or state.exists
            oid = 16385 if is_scratch else 16384
            name = "scratch" if is_scratch else "primary"
            system = "7450000000000000002" if state.fail == "system" else SYSTEM
            return f"{system}\t{oid}\t{name}\t170011\tf\towner\n".encode()
        if "--command=" + restore._EMPTY_SQL in command:
            return (
                b"f\tt\tt\tt\tt\tt\tt\tt\n"
                if state.fail == "nonempty"
                else b"t\tt\tt\tt\tt\tt\tt\tt\n"
            )
        query = next(item for item in command if item.startswith("--command="))
        if "NOT EXISTS" in query:
            return b"f\n" if state.exists else b"t\n"
        assert "datdba=" in query
        return b"f\n" if state.fail == "owner" else b"t\n"

    monkeypatch.setattr(database, "_run_client", client)
    return state


def create(**changes):
    args = {
        "run_digest": RUN,
        "expected_system_identifier": SYSTEM,
        "expected_primary_database_oid": 16384,
    }
    args.update(changes)
    return scratch.create_scratch("blocked", **args)


def test_create_records_new_oid_after_empty_owner_and_identity_checks(store):
    result = create()
    assert result["scratch_database_oid"] == 16385
    assert result["empty_scratch_observed"] is True
    assert result["complete_upgrade_gate"] is False
    assert steps._read(store.root / ".upgrade-control" / "blocked-scratch-created.json") == result
    writes = [(cmd, opts) for cmd, opts in store.calls if cmd[0].endswith("/createdb")]
    assert len(writes) == 1
    cmd, opts = writes[0]
    assert cmd[-2:] == ("--", "scratch")
    assert "--maintenance-db=primary" in cmd and "--template=template0" in cmd
    assert "--owner=owner" in cmd and "--encoding=UTF8" in cmd
    assert opts["timeout"] == 60
    assert "read_only=off" in opts["environment"]["PGOPTIONS"]
    assert all(
        "read_only=on" in opts["environment"]["PGOPTIONS"]
        for cmd, opts in store.calls
        if cmd[0].endswith("/psql")
    )
    assert all(not Path(opts["directory"]).exists() for _, opts in store.calls)


@pytest.mark.parametrize("failure", ["lost-response", "nonempty", "owner"])
def test_uncertain_or_invalid_creation_never_retries_or_publishes_success(store, failure):
    store.fail = failure
    with pytest.raises(scratch.ScratchCreationError, match="^upgrade-scratch-creation-refused$"):
        create()
    assert store.exists
    assert not (store.root / ".upgrade-control" / "blocked-scratch-created.json").exists()
    before = len(store.calls)
    with pytest.raises(scratch.ScratchCreationError):
        create()
    assert len(store.calls) == before


@pytest.mark.parametrize("failure", ["existing", "system"])
def test_preconditions_refuse_without_database_mutation(store, failure):
    store.exists = failure == "existing"
    store.fail = failure
    with pytest.raises(scratch.ScratchCreationError):
        create()
    assert not any(cmd[0].endswith("/createdb") for cmd, _ in store.calls)
    assert not (store.root / ".upgrade-control" / "blocked-scratch-create-reserved.json").exists()


@pytest.mark.parametrize(
    "name", ["primary", "postgres", "template0", "template1", "dbname=x", "a'b"]
)
def test_reserved_or_ambiguous_targets_refused_before_clients(store, monkeypatch, name):
    monkeypatch.setattr(database, "_connection", lambda: replace(store.connection, scratch=name))
    with pytest.raises(scratch.ScratchCreationError):
        create()
    assert not store.calls


@pytest.mark.parametrize(
    "change",
    [
        {"run_digest": "bad"},
        {"expected_primary_database_oid": True},
        {"expected_system_identifier": "01"},
        {"expected_system_identifier": str(2**64)},
    ],
)
def test_invalid_observed_identifiers_refused_before_clients(store, change):
    with pytest.raises(scratch.ScratchCreationError):
        create(**change)
    assert not store.calls


def test_non_linux_refused_before_clients(store, monkeypatch):
    monkeypatch.setattr(scratch.platform, "system", lambda: "Windows")
    with pytest.raises(scratch.ScratchCreationError):
        create()
    assert not store.calls
