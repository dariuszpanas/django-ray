"""Hosted Linux metadata, isolated SQLite, and real process deadline evidence."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from django_ray.payload_inventory import contracts, filesystem, process

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux inventory contract"
)


def payload(root: Path, body=b"private payload", *, old=False):
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    if old:
        os.utime(target, (1, 1))
    reference = f"resultfs://sha256/{digest}?rel={relative}&bytes={len(body)}"
    return target, digest, reference


def scan(root, lookup=lambda *_args: None, **options):
    frames = []
    filesystem.Scan(
        limits=contracts.Limits(**options), deadline=time.monotonic() + 5, emit=frames.append
    ).run(str(root), lookup)
    return frames


def test_scan_uses_only_directory_opens_and_metadata_preserving_bytes(tmp_path, monkeypatch):
    old, _digest, reference = payload(tmp_path, old=True)
    recent, _, _ = payload(tmp_path, b"recent private payload")
    before = {path: path.read_bytes() for path in (old, recent)}
    original = os.open
    calls = []

    def directory_only(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
        calls.append(path)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(filesystem.os, "open", directory_only)
    observed = scan(tmp_path)
    assert observed[-1]["code"] == "observed" and observed[-1]["objects"] == 2
    assert observed[-1]["counts"]["unregistered_aged"] == 1
    assert observed[-1]["counts"]["unregistered_recent"] == 1
    assert calls and reference not in json.dumps(observed)
    assert "private" not in json.dumps(observed) and str(tmp_path) not in json.dumps(observed)
    assert {path: path.read_bytes() for path in before} == before


def test_scan_rejects_symlinks_foreign_layout_and_nonregular_payloads(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    foreign = tmp_path / "foreign"
    target, digest, _ = payload(foreign)
    (owned / digest[:2]).symlink_to(foreign / digest[:2], target_is_directory=True)
    assert scan(owned)[-1]["counts"]["malformed"] == 1
    (owned / digest[:2]).unlink()
    destination = owned / digest[:2] / digest[2:4]
    destination.mkdir(parents=True)
    (destination / target.name).symlink_to(target)
    (destination / "wrong.json").write_bytes(b"foreign")
    os.mkfifo(destination / ("f" * 64 + ".json"))
    observed = scan(owned)
    assert observed[-1]["objects"] == 0 and observed[-1]["counts"]["malformed"] == 3
    alias = tmp_path / "alias"
    alias.symlink_to(owned, target_is_directory=True)
    assert scan(alias)[-1]["code"] == "filesystem_unavailable"


def test_root_parent_segments_cannot_hide_symlink_namespace_changes(tmp_path):
    safe = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe.mkdir()
    (outside / "child").mkdir(parents=True)
    (safe / "link").symlink_to(outside / "child", target_is_directory=True)
    payload(safe / "inputs", b"wrong namespace distractor")
    payload(outside / "inputs", b"runtime namespace")
    observed = scan(str(safe) + "/link/../inputs")
    assert observed[-1]["code"] == "filesystem_unavailable"
    assert observed[-1]["objects"] == 0


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"max_entries": 1}, "entry_budget_exhausted"),
        ({"max_objects": 1}, "object_budget_exhausted"),
        ({"max_details": 0}, "observed"),
    ],
)
def test_scan_budgets_bound_all_entries_and_report_omitted_details(tmp_path, options, expected):
    payload(tmp_path, b"first")
    payload(tmp_path, b"second")
    observed = scan(tmp_path, **options)
    assert observed[-1]["code"] == expected
    assert observed[-1]["entries"] <= options.get("max_entries", 1000)
    assert observed[-1]["objects"] <= options.get("max_objects", 100)
    assert len(observed) - 1 <= options.get("max_details", 20)


def test_shard_directly_selects_its_two_directories_and_counts_them(tmp_path):
    _, digest, _ = payload(tmp_path)
    for index in range(30):
        (tmp_path / f"foreign-{index}").touch()
    result = scan(tmp_path, shard=digest[:4], max_entries=3)
    assert result[-1]["entries"] == 3 and result[-1]["objects"] == 1
    assert result[-1]["code"] == "observed"
    assert scan(tmp_path, shard=digest[:4], max_entries=1)[-1]["code"] == "entry_budget_exhausted"
    assert scan(tmp_path, shard="0000")[-1]["objects"] == 0


def test_hardlinks_future_timestamps_and_disappearing_files_are_not_unregistered(
    tmp_path, monkeypatch
):
    target, _, _ = payload(tmp_path)
    os.link(target, tmp_path / "other")
    assert scan(tmp_path)[-1]["counts"]["ambiguous"] == 1
    (tmp_path / "other").unlink()
    os.utime(target, (time.time() + 1000, time.time() + 1000))
    assert scan(tmp_path)[-1]["counts"]["ambiguous"] == 1
    original = os.stat

    def disappear(path, *args, **kwargs):
        if path == target.name:
            raise FileNotFoundError("private path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(filesystem.os, "stat", disappear)
    result = scan(tmp_path)
    assert result[-1]["code"] == "filesystem_unavailable"
    assert result[-1]["counts"]["unavailable"] == 1


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    root = tmp_path / "payloads"
    root.mkdir()
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE django_ray_taskinputpayload (reference TEXT PRIMARY KEY, "
            "payload_kind TEXT, backend TEXT, digest TEXT, size_bytes INTEGER, "
            "envelope_version INTEGER, state TEXT)"
        )
    module = tmp_path / "inventory_owned_settings.py"
    module.write_text(
        f"DATABASES={{'default':{{'ENGINE':'django.db.backends.sqlite3','NAME':{str(database)!r}}}}}\n"
        f"DJANGO_RAY={{'INPUT_STORAGE_BACKEND':'filesystem','INPUT_STORAGE_FILESYSTEM_PATH':{str(root)!r}}}\n"
        "SECRET_KEY='inventory-test'\nUSE_TZ=True\n"
        "INSTALLED_APPS=['application_ready_must_not_import']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return root, database, module


def insert(database, reference, digest, size, *, kind="task_input", state="ACTIVE", version=1):
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO django_ray_taskinputpayload VALUES (?, ?, ?, ?, ?, ?, ?)",
            (reference, kind, "filesystem", digest, size, version, state),
        )


def test_real_isolated_probe_observes_native_wrapped_tombstone_and_conflicting_size(
    isolated_settings,
):
    root, database, module = isolated_settings
    for index, (kind, state, mismatch) in enumerate(
        (
            ("task_input", "ACTIVE", False),
            ("ray_job_request", "ACTIVE", False),
            ("task_input", "PURGED", False),
            ("task_input", "ACTIVE", True),
        )
    ):
        target, digest, reference = payload(root, f"private-{index}".encode())
        if kind == "ray_job_request":
            reference = "rq2ref:v1:" + reference
        insert(database, reference, digest, target.stat().st_size, kind=kind, state=state)
        if mismatch:
            target.write_bytes(b"changed byte count")
    before = database.read_bytes()
    value = process.inventory(settings_module=module.stem)
    assert value["complete"] and value["cleanup_confirmed"]
    assert value["counts"] == {
        **dict.fromkeys(contracts.OUTCOMES, 0),
        "registered_active": 2,
        "registered_tombstone": 1,
        "ambiguous": 1,
    }
    assert database.read_bytes() == before
    assert "private" not in json.dumps(value)


def test_real_probe_conflicting_aliases_fail_closed(isolated_settings):
    root, database, module = isolated_settings
    target, digest, reference = payload(root)
    insert(database, reference, digest, target.stat().st_size)
    insert(
        database, "rq2ref:v1:" + reference, digest, target.stat().st_size, kind="ray_job_request"
    )
    value = process.inventory(settings_module=module.stem)
    assert value["complete"] and value["counts"]["ambiguous"] == 1


@pytest.mark.parametrize(
    "configuration,expected",
    [
        (
            {"INPUT_STORAGE_BACKEND": "s3", "INPUT_STORAGE_S3_BUCKET": "private"},
            "filesystem_required",
        ),
        ({"INPUT_STORAGE_BACKEND": "gcs"}, "filesystem_required"),
        ({"INPUT_STORAGE_BACKEND": "filesystem"}, "configuration_invalid"),
    ],
)
def test_real_probe_does_not_initialize_cloud_storage(isolated_settings, configuration, expected):
    _, _, module = isolated_settings
    with module.open("a", encoding="utf-8") as handle:
        handle.write(f"DJANGO_RAY={configuration!r}\n")
    value = process.inventory(settings_module=module.stem)
    assert value["code"] == expected and value["cleanup_confirmed"]


def test_settings_import_is_bounded_and_cannot_print_private_text(isolated_settings):
    _, _, module = isolated_settings
    module.write_text("import time\nprint('private import output', flush=True)\ntime.sleep(60)\n")
    started = time.monotonic()
    value = process.inventory(settings_module=module.stem, limits=contracts.Limits(total_seconds=5))
    assert time.monotonic() - started < 6
    assert not value["complete"] and value["cleanup_confirmed"]
    assert value["code"] == "probe_protocol_invalid"
    assert "private" not in json.dumps(value)


def test_real_supervisor_deadline_kills_child_descendants_and_keeps_partial_observation(tmp_path):
    marker = tmp_path / "child.pid"
    detail = {"kind": "detail", "object_sha256": "a" * 64, "outcome": "unregistered_aged"}
    child = (
        "import os,time,signal;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "pid=os.fork();"
        f"open({str(marker)!r},'w').write(str(os.getpid())) if pid==0 else None;"
        f"print({json.dumps(detail)!r},flush=True) if pid!=0 else None;time.sleep(60)"
    )
    harness = (
        "import json,sys,time;from django_ray.payload_inventory.process import supervise;"
        "from django_ray.payload_inventory.contracts import Limits;"
        "sys.exit(supervise(json.loads(sys.argv[1]),deadline=time.monotonic()+3,"
        "limits=Limits(),emit=lambda value:print(json.dumps(value),flush=True)))"
    )
    owned = subprocess.Popen(
        [sys.executable, "-c", harness, json.dumps([sys.executable, "-c", child])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = owned.communicate(timeout=5)
        frames = [json.loads(line) for line in stdout.splitlines()]
        assert frames[1] == detail and not stderr
        assert frames[-1] == {"kind": "done", "code": "probe_timeout", "cleanup_confirmed": True}
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    finally:
        if owned.poll() is None:
            os.killpg(owned.pid, signal.SIGKILL)
            owned.wait(timeout=2)
