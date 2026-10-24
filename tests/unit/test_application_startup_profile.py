"""Resource-free contracts for the isolated, diagnostic-only startup control."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.startup import probe
from scripts import investigate_application_startup as controller


def sample_record(**updates):
    return {
        "schema_version": 1,
        "kind": "sample",
        "outcome": "complete",
        "import_setup_wall_ms": 8000,
        "import_setup_cpu_ms": 1800,
        "imported_source_modules": 20,
        "imported_checked_caches": 0,
        "child_wall_ms": 8100,
        "child_cpu_ms": 1900,
        **updates,
    }


def census_record(**updates):
    return {
        "schema_version": 1,
        "kind": "census",
        "source_sha256": "a" * 64,
        "source_files": 20,
        "python_files": 10,
        "checked_hash_caches": 0,
        "cache_files": 0,
        "python_version": [3, 12, 12],
        "cache_tag": "cpython-312",
        "optimize": 0,
        **updates,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"extra": "arbitrary"},
        {"outcome": "unknown"},
        {"outcome": []},
        {"import_setup_wall_ms": None},
        {"child_cpu_ms": -1},
        {"child_cpu_ms": True},
        {"imported_checked_caches": 21},
        {"imported_source_modules": 0},
        {"child_wall_ms": 120001},
    ],
)
def test_sample_schema_refuses_unknown_malformed_and_contradictory_fields(updates):
    with pytest.raises(controller.RefusedError):
        controller.validate_record(json.dumps(sample_record(**updates)).encode(), "sample")


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"optimize": False},
        {"source_sha256": 5},
        {"python_version": [3, 12, "bad"]},
        {"python_version": None},
        {"python_version": [3, 13, 1]},
        {"source_files": 4097},
        {"checked_hash_caches": 11},
        {"python_files": 0},
        {"cache_tag": "other"},
    ],
)
def test_census_schema_refuses_malformed_identity(updates):
    with pytest.raises(controller.RefusedError):
        controller.validate_record(json.dumps(census_record(**updates)).encode(), "census")


@pytest.mark.parametrize(
    "raw", [b"", b"x" * (probe.MAX_REPORT + 1), b"{}{}", b'{"schema_version":1,"schema_version":1}']
)
def test_protocol_refuses_oversize_duplicate_or_multiple_json(raw):
    with pytest.raises(controller.RefusedError):
        controller.validate_record(raw, "sample")


def test_container_command_preserves_settings_and_closed_resource_admission():
    args = controller.create_args("owned", "owner", "sha256:" + "a" * 64, "/probe.py", "sample")
    for option, expected in {
        "--network": "none",
        "--cpus": "0.25",
        "--memory": "768m",
        "--memory-swap": "768m",
        "--pids-limit": "64",
        "--user": "1000:1000",
        "--workdir": "/app",
        "--entrypoint": "/usr/bin/env",
    }.items():
        assert args[args.index(option) + 1] == expected
    assert "--read-only" in args and "--no-healthcheck" in args
    assert args[args.index("sha256:" + "a" * 64) + 1] == "-i"
    assert "DJANGO_SETTINGS_MODULE=testproject.settings_qualification" in args
    assert "DATABASE_ENGINE=django.db.backends.postgresql" in args
    assert args[-3:] == ["/app/.venv/bin/python", "/diagnostic/probe.py", "sample"]
    assert all("DJANGO_RAY_QUALIFICATION_CLIENT_ADDRESS" not in arg for arg in args)


def test_source_manifest_tracks_bytes_and_modes_but_not_caches(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    source = root / "example.py"
    source.write_text("value = 1\n")
    first, _ = probe.source_manifest((root,))
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "example.cpython-312.pyc").write_bytes(b"not executable")
    assert probe.source_manifest((root,))[0] == first
    source.write_text("value = 2\n")
    assert probe.source_manifest((root,))[0] != first


def test_cache_validation_requires_interpreter_checked_hash_and_source(tmp_path):
    path = tmp_path / "example.py"
    path.write_bytes(b"value = 1\n")
    cached = Path(importlib.util.cache_from_source(str(path)))
    cached.parent.mkdir()
    header = (
        importlib.util.MAGIC_NUMBER
        + (3).to_bytes(4, "little")
        + importlib.util.source_hash(path.read_bytes())
    )
    cached.write_bytes(header)
    assert probe.cache_valid(path)
    path.write_bytes(b"value = 2\n")
    assert not probe.cache_valid(path)
    cached.write_bytes(header[:4] + (0).to_bytes(4, "little") + header[8:])
    assert not probe.cache_valid(path)


def test_guards_do_not_import_measured_modules_when_installed(monkeypatch):
    monkeypatch.setattr(probe.sys, "modules", {})
    monkeypatch.setattr(probe.sys, "meta_path", [])
    audit = Mock()
    monkeypatch.setattr(probe.sys, "addaudithook", audit)
    guard = probe.Guards()
    guard.install()
    assert probe.sys.modules == {}
    assert probe.sys.meta_path == [guard]
    audit.assert_called_once_with(guard.audit)


def test_ray_is_refused_before_finder_or_loader_can_run(monkeypatch):
    find = Mock(side_effect=AssertionError("loader must not run"))
    monkeypatch.setattr(probe.importlib.machinery.PathFinder, "find_spec", find)
    guard = probe.Guards()
    with pytest.raises(probe.RefusedError):
        guard.find_spec("ray.runtime_env")
    assert guard.hit
    find.assert_not_called()


@pytest.mark.parametrize("name", ["django.db.backends.base.base", "psycopg", "psycopg2"])
def test_lazy_database_guard_replaces_connection_before_consumer_receives_module(monkeypatch, name):
    original_connect = Mock(side_effect=AssertionError("must not connect"))
    wrapper = type("Wrapper", (), {"connect": original_connect})
    module = SimpleNamespace(
        BaseDatabaseWrapper=wrapper,
        connect=original_connect,
        Connection=type("Connection", (), {"connect": original_connect}),
        AsyncConnection=type("Async", (), {"connect": original_connect}),
    )
    original = SimpleNamespace(create_module=lambda _spec: None, exec_module=lambda _module: None)
    spec = SimpleNamespace(loader=original)
    monkeypatch.setattr(probe.importlib.machinery.PathFinder, "find_spec", lambda *args: spec)
    guard = probe.Guards()
    spec = guard.find_spec(name)
    spec.loader.exec_module(module)
    connection = wrapper().connect if name.startswith("django") else module.connect
    with pytest.raises(probe.RefusedError):
        connection()
    assert guard.hit
    original_connect.assert_not_called()


def test_caught_forbidden_operation_still_refuses_sample(monkeypatch):
    guard = probe.Guards()
    monkeypatch.setattr(probe, "Guards", lambda: guard)
    monkeypatch.setattr(guard, "install", lambda: None)

    def setup():
        try:
            guard.refuse()
        except probe.RefusedError:
            pass

    monkeypatch.setitem(sys.modules, "django", SimpleNamespace(setup=setup))
    monkeypatch.setattr(probe, "imported_source_caches", lambda: (3, 0))
    write = Mock()
    monkeypatch.setattr(probe.os, "write", write)
    monkeypatch.setattr(probe.sys, "path", list(sys.path))
    probe.setup_child(5)
    assert json.loads(write.call_args.args[1])["outcome"] == "guard_refused"


def test_module_origins_match_docker_copy_and_reject_shadow(monkeypatch):
    modules = {
        "django_ray": SimpleNamespace(__file__="/app/src/django_ray/__init__.py"),
        "testproject.settings_qualification": SimpleNamespace(
            __file__="/app/testproject/settings_qualification.py"
        ),
    }
    for module in modules.values():
        module.__cached__ = importlib.util.cache_from_source(module.__file__)
    monkeypatch.setattr(probe.sys, "modules", modules)
    monkeypatch.setattr(probe, "cache_valid", lambda _path: False)
    assert probe.imported_source_caches() == (2, 0)
    modules["testproject.settings_qualification"].__file__ = "/foreign/settings_qualification.py"
    with pytest.raises(ValueError, match="module_origin"):
        probe.imported_source_caches()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/example.py").write_text("value = 1\n")
    (source / "testproject").mkdir()
    (source / "testproject/example.py").write_text("value = 2\n")
    (source / "qualification/startup").mkdir(parents=True)
    (source / "qualification/startup/probe.py").write_bytes(Path(probe.__file__).read_bytes())
    for name in ("Dockerfile", "uv.lock", "pyproject.toml"):
        (source / name).write_text("fixture")
    subject = controller.Controller("sha256:" + "a" * 64, source, tmp_path / "out", "123-1")
    monkeypatch.setattr(controller.sys, "platform", "linux")
    monkeypatch.setattr(subject, "prepare", lambda: "sha256:" + "b" * 64)
    monkeypatch.setattr(subject, "cleanup", lambda: True)
    expected = probe.source_manifest((source / "src", source / "testproject"))[0]
    calls = []

    def sample(image, kind, index):
        calls.append((image, kind, index))
        if kind == "census":
            compiled = index == "census-b"
            return census_record(
                source_sha256=expected,
                checked_hash_caches=10 if compiled else 0,
                cache_files=10 if compiled else 0,
            )
        return sample_record()

    monkeypatch.setattr(subject, "sample", sample)
    return subject, calls


def test_fixed_abba_has_four_samples_and_never_acceptance(prepared):
    subject, calls = prepared
    result = subject.investigate()
    assert result["status"] == "complete" and result["cleanup_confirmed"]
    assert result["acceptance"] is False
    assert [item["variant"] for item in result["samples"]] == list(controller.ORDER)
    assert len(calls) == 6
    assert [image[-1] for image, kind, _index in calls if kind == "sample"] == list("abba")


@pytest.mark.parametrize(
    "failure", ["source_mismatch", "baseline_already_cached", "incomplete_precompile"]
)
def test_source_or_cache_mismatch_prevents_all_measurements(prepared, monkeypatch, failure):
    subject, calls = prepared
    original = subject.sample

    def changed(image, kind, index):
        result = original(image, kind, index)
        if index == "census-a" and failure == "source_mismatch":
            result["source_sha256"] = "c" * 64
        if index == "census-a" and failure == "baseline_already_cached":
            result["cache_files"] = 1
        if index == "census-b" and failure == "incomplete_precompile":
            result["checked_hash_caches"] = 0
        return result

    monkeypatch.setattr(subject, "sample", changed)
    result = subject.investigate()
    assert result["reason"] == failure and result["status"] == "incomplete"
    assert not result["samples"] and len(calls) == 2


def test_changed_helper_prevents_docker_work(prepared):
    subject, calls = prepared
    (subject.source / "qualification/startup/probe.py").write_text("changed")
    result = subject.investigate()
    assert result["reason"] == "source_helper_mismatch" and not calls


def test_cleanup_failure_dominates_complete_observations(prepared, monkeypatch):
    subject, _calls = prepared
    monkeypatch.setattr(subject, "cleanup", lambda: False)
    result = subject.investigate()
    assert result["reason"] == "cleanup_unconfirmed" and result["status"] == "incomplete"


def test_one_total_deadline_preserves_cleanup_reserve(monkeypatch, tmp_path):
    monkeypatch.setattr(controller.time, "monotonic", lambda: 100)
    subject = controller.Controller("image", tmp_path, tmp_path, "owner")
    monkeypatch.setattr(controller.time, "monotonic", lambda: 500)
    run = Mock(return_value=(0, b""))
    monkeypatch.setattr(controller, "command", run)
    subject.run(["mock"], 180)
    assert run.call_args.args[1] == 3
    subject.run(["mock"], 180, cleanup=True)
    assert run.call_args.args[1] == 48


def test_unadmitted_cleanup_never_touches_docker(tmp_path, monkeypatch):
    subject = controller.Controller("image", tmp_path, tmp_path, "owner")
    run = Mock(side_effect=AssertionError("no Docker"))
    monkeypatch.setattr(subject, "run", run)
    assert subject.cleanup()
    run.assert_not_called()


@pytest.mark.parametrize(
    "platform,owner,reason",
    [
        ("win32", "123-1", "linux_required"),
        ("linux", "foreign/invalid", "invalid_identity"),
    ],
)
def test_preallocation_refusal_never_calls_docker(tmp_path, monkeypatch, platform, owner, reason):
    subject = controller.Controller("sha256:" + "a" * 64, tmp_path, tmp_path, owner)
    monkeypatch.setattr(controller.sys, "platform", platform)
    run = Mock(side_effect=AssertionError("no Docker"))
    monkeypatch.setattr(subject, "run", run)
    result = subject.investigate()
    assert result["reason"] == reason
    assert result["cleanup_confirmed"]
    run.assert_not_called()


@pytest.mark.parametrize("existing", ["container", "tag"])
def test_preexisting_owner_resource_is_refused_and_never_deleted(tmp_path, monkeypatch, existing):
    subject = controller.Controller("sha256:" + "a" * 64, tmp_path, tmp_path, "123-1")
    calls = []

    def run(args, *extra, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "ps", "-aq"]:
            return 0, b"a" * 12 if existing == "container" else b""
        return 0, b""

    monkeypatch.setattr(subject, "run", run)
    monkeypatch.setattr(subject, "inspect_image", lambda *_args: {"Id": subject.image})
    with pytest.raises(controller.RefusedError, match="existing_"):
        subject.prepare()
    before = list(calls)
    assert subject.cleanup()
    assert calls == before
    assert all("rm" not in args and "tag" not in args and "create" not in args for args in calls)


def test_cleanup_refuses_foreign_name_even_with_matching_owner_label(tmp_path, monkeypatch):
    subject = controller.Controller("image", tmp_path, tmp_path, "123-1")
    subject.admitted = True
    subject.names = ["owned-name"]
    calls = []

    def checked(args, *extra, **kwargs):
        calls.append(args)
        return b"a" * 12

    monkeypatch.setattr(subject, "checked", checked)
    monkeypatch.setattr(subject, "inspect_container", lambda _name: {"Name": "/foreign-name"})
    assert not subject.cleanup()
    assert not any("rm" in args for args in calls)


def test_retained_writer_cannot_hold_controller_pipe_open(monkeypatch):
    stream = Mock()
    stream.fileno.return_value = 5
    process = Mock(stdout=stream, pid=123, returncode=None)
    process.poll.return_value = 0  # CLI exited, descendant still owns stdout.
    popen = Mock(return_value=process)
    monkeypatch.setattr(controller.subprocess, "Popen", popen)
    monkeypatch.setattr(controller, "peek_exit", lambda _process: 0)
    monkeypatch.setattr(controller.signal, "SIGKILL", 9, raising=False)
    select = Mock()
    select.select.return_value = []
    monkeypatch.setattr(controller.selectors, "DefaultSelector", lambda: select)
    monkeypatch.setattr(controller.os, "set_blocking", Mock())
    kill = Mock()
    monkeypatch.setattr(controller.os, "killpg", kill, raising=False)
    ticks = iter((0, 0.01, 0.1))
    monkeypatch.setattr(controller.time, "monotonic", lambda: next(ticks))
    with pytest.raises(controller.RefusedError, match="command_timeout"):
        controller.command(["mock"], 0.05)
    kill.assert_called_once_with(123, controller.signal.SIGKILL)
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["bufsize"] == 0
    process.wait.assert_called_once_with(timeout=2)
    stream.close.assert_called_once()
    select.close.assert_called_once()
    process.poll.assert_not_called()


def test_exit_peek_preserves_leader_until_final_group_signal(monkeypatch):
    for name, value in {
        "P_PID": 1,
        "WEXITED": 4,
        "WNOHANG": 1,
        "WNOWAIT": 8,
        "CLD_EXITED": 1,
    }.items():
        monkeypatch.setattr(controller.os, name, value, raising=False)
    wait = Mock(return_value=SimpleNamespace(si_status=0, si_code=1))
    monkeypatch.setattr(controller.os, "waitid", wait, raising=False)
    process = Mock(pid=123, returncode=None)
    assert controller.peek_exit(process) == 0
    wait.assert_called_once_with(1, 123, 13)
    process.poll.assert_not_called()
    process.wait.assert_not_called()


def test_selector_initialization_failure_still_kills_and_reaps_owned_cli(monkeypatch):
    process = Mock(pid=123)
    monkeypatch.setattr(controller.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(controller, "peek_exit", lambda _process: None)
    monkeypatch.setattr(controller.selectors, "DefaultSelector", Mock(side_effect=OSError("fixed")))
    monkeypatch.setattr(controller.signal, "SIGKILL", 9, raising=False)
    kill = Mock()
    monkeypatch.setattr(controller.os, "killpg", kill, raising=False)
    with pytest.raises(OSError):
        controller.command(["mock"], 1)
    kill.assert_called_once_with(123, 9)
    process.wait.assert_called_once_with(timeout=2)
    process.stdout.close.assert_called_once()


def test_lost_leader_ownership_prevents_final_group_signal(monkeypatch):
    process = Mock(pid=123, returncode=None)
    monkeypatch.setattr(controller.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(controller.selectors, "DefaultSelector", Mock(side_effect=OSError("fixed")))
    monkeypatch.setattr(controller, "peek_exit", Mock(side_effect=ChildProcessError()))
    kill = Mock()
    monkeypatch.setattr(controller.os, "killpg", kill, raising=False)
    with pytest.raises(controller.RefusedError, match="command_ownership_lost"):
        controller.command(["mock"], 1)
    kill.assert_not_called()
    process.wait.assert_not_called()
    process.stdout.close.assert_called_once()


def test_previously_reaped_leader_is_not_owned():
    with pytest.raises(ChildProcessError):
        controller.peek_exit(SimpleNamespace(returncode=0, pid=123))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process ownership requires hosted Linux")
def test_linux_inherited_stdout_descendant_cannot_extend_deadline():
    import time

    started = time.monotonic()
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "sys.exit(0)"
    )
    with pytest.raises(controller.RefusedError, match="command_timeout"):
        controller.command([sys.executable, "-c", script], 0.5)
    assert time.monotonic() - started < 4
