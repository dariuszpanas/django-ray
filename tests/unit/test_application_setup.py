"""Bounded receipt and failure behavior for disposable application setup."""

import json
from types import SimpleNamespace

import pytest

from qualification.application import setup_core


@pytest.fixture
def successful_setup(monkeypatch):
    receipt = {
        "schema_version": 1,
        "layer": "application_setup",
        "status": "passed",
        "complete_application_gate": False,
        "postgresql_major": 17,
        "migrations_applied": True,
        "static_collected": True,
        "administrator_created": True,
        "archives": {"source": {"sha256": "a" * 64}, "recovery": {"sha256": "b" * 64}},
    }
    monkeypatch.setattr(setup_core, "prepare", lambda **kwargs: receipt.copy())
    return receipt


def test_setup_reuses_only_identical_receipt(tmp_path, successful_setup, capsys):
    args = ["--runtime-root", str(tmp_path)]
    assert setup_core.main(args) == 0
    original = (tmp_path / "setup.json").read_bytes()
    assert setup_core.main(args) == 0
    assert (tmp_path / "setup.json").read_bytes() == original
    assert json.loads(original) == successful_setup
    assert all(
        json.loads(line)["status"] == "passed" for line in capsys.readouterr().out.splitlines()
    )


def test_setup_rejects_changed_existing_identity(tmp_path, successful_setup, capsys):
    path = tmp_path / "setup.json"
    original = json.dumps({**successful_setup, "archives": {}}).encode()
    path.write_bytes(original)
    assert setup_core.main(["--runtime-root", str(tmp_path)]) == 1
    assert path.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_setup_writes_to_explicit_shared_receipt_path(tmp_path, successful_setup):
    target = tmp_path / "receipt-volume" / "setup.json"
    target.parent.mkdir()
    assert setup_core.main(["--runtime-root", str(tmp_path), "--receipt", str(target)]) == 0
    assert json.loads(target.read_bytes()) == successful_setup
    assert not (tmp_path / "setup.json").exists()


def test_setup_refuses_oversize_receipt(tmp_path, successful_setup, capsys):
    successful_setup["archives"] = {"unexpected": "x" * (16 * 1024)}
    assert setup_core.main(["--runtime-root", str(tmp_path)]) == 1
    assert not (tmp_path / "setup.json").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_setup_requires_collected_static_manifest(tmp_path, successful_setup, capsys):
    successful_setup["static_collected"] = False
    assert setup_core.main(["--runtime-root", str(tmp_path)]) == 1
    assert not (tmp_path / "setup.json").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_setup_failure_does_not_print_private_exception(tmp_path, monkeypatch, capsys):
    def failure(**kwargs):
        raise ValueError("private database credential")

    monkeypatch.setattr(setup_core, "prepare", failure)
    assert setup_core.main(["--runtime-root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "private" not in output
    assert json.loads(output)["complete_application_gate"] is False
    assert not (tmp_path / "setup.json").exists()


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf")])
def test_setup_refuses_unbounded_database_wait_before_django(tmp_path, monkeypatch, timeout):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    with pytest.raises(ValueError, match="Database wait"):
        setup_core.prepare(base=tmp_path, runtime_root=tmp_path, database_timeout=timeout)


def test_setup_refuses_ordinary_settings_before_django(tmp_path, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    with pytest.raises(ValueError, match="qualification settings"):
        setup_core.prepare(base=tmp_path, runtime_root=tmp_path)


def test_setup_refuses_mismatched_archive_before_django(tmp_path, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setenv("DJANGO_RAY_RECOVERY_WORKING_DIR", "/wrong/recovery.zip")
    with pytest.raises(ValueError, match="match setup output"):
        setup_core.prepare(base=tmp_path, runtime_root=tmp_path)


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    import django
    import django.contrib.auth
    import django.core.management
    import django.db
    from django.conf import settings

    from testproject import runtime_env_bundles

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setenv("DJANGO_RAY_RECOVERY_WORKING_DIR", str(tmp_path / "recovery.zip"))
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "GeneratedUnitTestCredential0123456789")
    monkeypatch.setattr(settings, "STATIC_ROOT", str(tmp_path))
    calls = []
    monkeypatch.setattr(django, "setup", lambda: calls.append("django.setup"))
    connection = SimpleNamespace(
        vendor="postgresql",
        pg_version=170006,
        ensure_connection=lambda: calls.append("connect"),
        close=lambda: calls.append("close"),
    )
    monkeypatch.setattr(django.db, "connection", connection)

    def command(name, **kwargs):
        assert kwargs == {"interactive": False, "verbosity": 0}
        calls.append(name)
        if name == "collectstatic":
            (tmp_path / "staticfiles.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(django.core.management, "call_command", command)
    user = SimpleNamespace(
        check_password=lambda password: False,
        set_password=lambda password: calls.append("set_password"),
        save=lambda: calls.append("save_user"),
    )

    def get_or_create(**kwargs):
        assert kwargs == {"username": "qualification"}
        return user, True

    monkeypatch.setattr(
        django.contrib.auth,
        "get_user_model",
        lambda: SimpleNamespace(objects=SimpleNamespace(get_or_create=get_or_create)),
    )

    def bundle(*, base, target):
        assert base == tmp_path
        calls.append(target.name)
        target.write_bytes(b"bounded bundle fixture")

    monkeypatch.setattr(runtime_env_bundles, "build_source_bundle", bundle)
    monkeypatch.setattr(runtime_env_bundles, "build_recovery_bundle", bundle)
    return SimpleNamespace(calls=calls, connection=connection, user=user)


def test_setup_prepares_database_assets_admin_and_archive_pair(tmp_path, preparation):
    receipt = setup_core.prepare(base=tmp_path, runtime_root=tmp_path)
    assert preparation.calls == [
        "django.setup",
        "connect",
        "migrate",
        "collectstatic",
        "set_password",
        "save_user",
        "project.zip",
        "recovery.zip",
    ]
    assert (
        preparation.user.is_staff and preparation.user.is_superuser and preparation.user.is_active
    )
    assert receipt["postgresql_major"] == 17
    assert receipt["static_collected"] is True
    assert set(receipt["archives"]) == {"source", "recovery"}
    assert all(
        item["bytes"] == len(b"bounded bundle fixture") for item in receipt["archives"].values()
    )


@pytest.mark.parametrize("version", [160006, 180000])
def test_setup_rejects_other_postgresql_versions_before_migrations(tmp_path, preparation, version):
    preparation.connection.pg_version = version
    with pytest.raises(ValueError, match="PostgreSQL 17"):
        setup_core.prepare(base=tmp_path, runtime_root=tmp_path)
    assert preparation.calls == ["django.setup", "connect"]


def test_setup_times_out_database_without_migrating(tmp_path, preparation, monkeypatch):
    from django.db import OperationalError

    ticks = iter([0, 2])
    monkeypatch.setattr(setup_core.time, "monotonic", lambda: next(ticks))

    def unavailable():
        raise OperationalError("private database diagnostics")

    preparation.connection.ensure_connection = unavailable
    with pytest.raises(ValueError, match="did not become ready"):
        setup_core.prepare(base=tmp_path, runtime_root=tmp_path, database_timeout=1)
    assert preparation.calls == ["django.setup", "close"]


def test_setup_rejects_bad_admin_credential_before_django(tmp_path, preparation, monkeypatch):
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "invalid")
    with pytest.raises(ValueError, match="administrator credential"):
        setup_core.prepare(base=tmp_path, runtime_root=tmp_path)
    assert preparation.calls == []
