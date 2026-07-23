"""Fail-closed tests for bounded Ray cleanup evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ray_residue


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "complete": True,
        "errors": [],
        "processes": [],
        "listeners": [],
        "shared_memory": [],
        "global_temp": [],
    }


def test_ray_process_detection_normalizes_windows_native_names() -> None:
    assert ray_residue._is_ray_process("raylet.exe", "")
    assert ray_residue._is_ray_process("gcs_server.exe", "")
    assert ray_residue._is_ray_process(
        "python.exe", "python ray/_private/workers/default_worker.py"
    )
    assert not ray_residue._is_ray_process("python.exe", "python scripts/ray_residue.py")


@pytest.mark.parametrize(
    "process_name",
    (
        "dashboard",
        "dashboard_agent",
        "gcs_server",
        "log_monitor",
        "monitor",
        "python-core-driver",
        "python-core-worker",
        "raylet",
        "ray_client_server",
        "reaper",
        "redis_server",
        "runtime_env_agent",
        "worker",
    ),
)
def test_ray_process_detection_covers_declared_ray_process_types(
    process_name: str,
) -> None:
    assert ray_residue._is_ray_process(process_name, "")


@pytest.mark.parametrize(
    "command",
    (
        "python ray/autoscaler/_private/monitor.py",
        "python ray/autoscaler/v2/monitor.py",
        "python ray/dashboard/dashboard.py",
        "python ray/_private/ray_process_reaper.py",
        "python ray/_private/workers/setup_worker.py -m ray.util.client.server",
        r"python ray\autoscaler\_private\monitor.py",
    ),
)
def test_ray_process_detection_covers_python_entrypoints(command: str) -> None:
    assert ray_residue._is_ray_process("python", command)


def test_assert_clean_removes_only_owned_ray_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    owned = root / "artifacts" / "sample" / "ray-tmp"
    (owned / "ray" / "session").mkdir(parents=True)
    (owned / "ray" / "session" / "log.txt").write_text("bounded", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("outside", encoding="utf-8")
    if os.name != "nt":
        (owned / "external-link").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(ray_residue, "capture_snapshot", _snapshot)

    report = ray_residue.assert_clean(_snapshot(), owned, root)

    assert report["valid"] is True
    assert report["additions"] == {
        "processes": [],
        "listeners": [],
        "shared_memory": [],
        "global_temp": [],
    }
    assert report["owned_temp"]["removed"] is True
    assert report["owned_temp"]["exists_after"] is False
    expected_entries = 4 if os.name != "nt" else 3
    assert report["owned_temp"]["entries_observed"] == expected_entries
    assert report["owned_temp"]["scan_truncated"] is False
    assert not owned.exists()
    assert (external / "keep.txt").read_text(encoding="utf-8") == "outside"

    assert ray_residue._owned_temp_entry_scan(owned, limit=2) == (0, False)
    owned.mkdir(parents=True)
    for index in range(3):
        (owned / f"entry-{index}").touch()
    assert ray_residue._owned_temp_entry_scan(owned, limit=2) == (3, True)
    for entry in owned.iterdir():
        entry.unlink()
    owned.rmdir()

    owned.mkdir(parents=True)
    (owned / "large-diagnostic.log").write_text("bounded", encoding="utf-8")
    monkeypatch.setattr(
        ray_residue,
        "_owned_temp_entry_scan",
        lambda _path: (ray_residue.OWNED_TEMP_SCAN_LIMIT + 1, True),
    )

    truncated_report = ray_residue.assert_clean(_snapshot(), owned, root)

    assert truncated_report["valid"] is True
    assert truncated_report["owned_temp"] == {
        "entries_observed": ray_residue.OWNED_TEMP_SCAN_LIMIT + 1,
        "scan_limit": ray_residue.OWNED_TEMP_SCAN_LIMIT,
        "scan_truncated": True,
        "scan_error": None,
        "removed": True,
        "exists_after": False,
    }
    assert not owned.exists()

    owned.mkdir(parents=True)
    (owned / "unscanned.log").write_text("bounded", encoding="utf-8")

    def _fail_scan(_path: Path) -> tuple[int, bool]:
        raise ray_residue.ResidueError("scanner unavailable")

    monkeypatch.setattr(ray_residue, "_owned_temp_entry_scan", _fail_scan)

    scan_error_report = ray_residue.assert_clean(_snapshot(), owned, root)

    assert scan_error_report["valid"] is True
    assert scan_error_report["owned_temp"] == {
        "entries_observed": None,
        "scan_limit": ray_residue.OWNED_TEMP_SCAN_LIMIT,
        "scan_truncated": None,
        "scan_error": "scanner unavailable",
        "removed": True,
        "exists_after": False,
    }
    assert not owned.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("processes", {}),
        ("listeners", {}),
        ("shared_memory", {}),
        ("global_temp", {}),
        ("errors", {}),
        ("complete", 1),
        ("schema_version", True),
    ),
)
def test_assert_clean_rejects_malformed_snapshot_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    baseline = _snapshot()
    baseline[field] = value
    monkeypatch.setattr(ray_residue, "capture_snapshot", _snapshot)

    with pytest.raises(ray_residue.ResidueError, match="baseline"):
        ray_residue.assert_clean(
            baseline,
            tmp_path / "repository" / "artifacts" / "ray-tmp",
            tmp_path / "repository",
        )


@pytest.mark.parametrize(
    ("field", "records", "message"),
    (
        (
            "processes",
            [
                {"pid": 42, "name": "raylet", "command_sha256": "a" * 64},
                {"pid": 42, "name": "gcs_server", "command_sha256": "b" * 64},
            ],
            "duplicate process identities",
        ),
        (
            "listeners",
            [
                {"pid": 42, "host": "127.0.0.1", "port": 6379},
                {"pid": 42, "host": "127.0.0.1", "port": 6379},
            ],
            "duplicate listener identities",
        ),
    ),
)
def test_validate_snapshot_rejects_duplicate_identities(
    field: str,
    records: list[dict[str, object]],
    message: str,
) -> None:
    snapshot = _snapshot()
    snapshot[field] = records

    with pytest.raises(ray_residue.ResidueError, match=message):
        ray_residue._validate_snapshot(snapshot, "test")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("complete", False, "incomplete"),
        (
            "processes",
            [{"pid": 42, "name": "raylet", "command_sha256": "a" * 64}],
            "clean fresh-runner",
        ),
        ("shared_memory", ["ray-session"], "clean fresh-runner"),
    ),
)
def test_snapshot_cli_fails_before_tests_for_invalid_or_nonempty_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = _snapshot()
    snapshot[field] = value
    monkeypatch.setattr(ray_residue, "capture_snapshot", lambda: snapshot)
    output = tmp_path / "baseline.json"

    assert ray_residue.main(["snapshot", "--output", str(output)]) == 2

    assert message in capsys.readouterr().err
    assert output.exists()
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_assert_clean_reports_active_additions_without_deleting_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    owned = root / "artifacts" / "sample" / "ray-tmp"
    owned.mkdir(parents=True)
    current = copy.deepcopy(_snapshot())
    current["processes"] = [
        {
            "pid": 42,
            "name": "raylet",
            "command_sha256": "a" * 64,
        }
    ]
    monkeypatch.setattr(ray_residue, "capture_snapshot", lambda: current)
    monkeypatch.setattr(
        ray_residue,
        "_owned_temp_entry_scan",
        lambda _path: (ray_residue.OWNED_TEMP_SCAN_LIMIT + 1, True),
    )

    report = ray_residue.assert_clean(_snapshot(), owned, root)

    assert report["valid"] is False
    assert report["errors"] == ["Ray processes remain after the canonical target"]
    assert report["additions"]["processes"] == [[42, "raylet", "a" * 64]]
    assert report["owned_temp"]["scan_truncated"] is True
    assert owned.exists()

    def _fail_scan(_path: Path) -> tuple[int, bool]:
        raise ray_residue.ResidueError("scanner unavailable")

    monkeypatch.setattr(ray_residue, "_owned_temp_entry_scan", _fail_scan)
    scan_error_report = ray_residue.assert_clean(_snapshot(), owned, root)

    assert scan_error_report["additions"]["processes"] == [[42, "raylet", "a" * 64]]
    assert scan_error_report["owned_temp"]["scan_error"] == "scanner unavailable"
    assert owned.exists()


@pytest.mark.parametrize(
    ("body_status", "residue_leak", "expected_status", "cleanup_status"),
    (
        (7, False, 7, 0),
        (0, True, 2, 2),
        (7, True, 2, 2),
    ),
)
def test_guarded_run_always_reports_cleanup_and_preserves_failure_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_status: int,
    residue_leak: bool,
    expected_status: int,
    cleanup_status: int,
) -> None:
    root = tmp_path / "repository"
    evidence = root / "artifacts" / "benchmark"
    owned = evidence / "ray-tmp"
    owned.mkdir(parents=True)
    (owned / "diagnostic.txt").write_text("bounded", encoding="utf-8")
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / ".gitignore").write_text("artifacts/benchmark/\n", encoding="utf-8")
    baseline = evidence / "ray-baseline.json"
    baseline.write_text(json.dumps(_snapshot()), encoding="utf-8")
    output = evidence / "ray-residue.json"
    current = _snapshot()
    if residue_leak:
        current["processes"] = [{"pid": 42, "name": "raylet", "command_sha256": "a" * 64}]
    capture_count = 0

    def _capture() -> dict[str, object]:
        nonlocal capture_count
        capture_count += 1
        return current

    monkeypatch.setattr(ray_residue, "capture_snapshot", _capture)
    monkeypatch.chdir(root)

    status = ray_residue.main(
        [
            "guard",
            "--baseline",
            str(baseline),
            "--owned-temp-dir",
            str(owned),
            "--output",
            str(output),
            "--",
            sys.executable,
            "-c",
            f"raise SystemExit({body_status})",
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert status == expected_status
    assert capture_count == 1
    assert report["guard"] == {
        "body_returncode": body_status,
        "cleanup_returncode": cleanup_status,
    }
    assert report["valid"] is (not residue_leak)
    assert owned.exists() is residue_leak
    assert not (evidence / ray_residue.GUARD_SENTINEL).exists()


def test_assert_clean_cli_writes_bounded_report_when_scanner_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    owned = root / "artifacts" / "benchmark" / "ray-tmp"
    owned.mkdir(parents=True)
    baseline = root / "baseline.json"
    baseline.write_text(json.dumps(_snapshot()), encoding="utf-8")
    output = root / "ray-residue.json"

    def _fail_capture() -> dict[str, object]:
        raise ray_residue.ResidueError("scanner result exceeded its bound")

    monkeypatch.setattr(ray_residue, "capture_snapshot", _fail_capture)
    monkeypatch.chdir(root)

    status = ray_residue.main(
        [
            "assert-clean",
            "--baseline",
            str(baseline),
            "--owned-temp-dir",
            str(owned),
            "--output",
            str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert status == 2
    assert report["valid"] is False
    assert report["errors"] == ["cleanup evidence is invalid: scanner result exceeded its bound"]
    assert len(output.read_bytes()) < 2_000


def test_guard_rejects_repository_root_as_evidence_directory(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / ".gitignore").write_text("ray-residue.json\n", encoding="utf-8")

    with pytest.raises(ray_residue.ResidueError, match="repository root"):
        ray_residue.guarded_run(
            [sys.executable, "-c", "raise SystemExit(0)"],
            baseline_path=root / "ray-baseline.json",
            owned_temp=root / "ray-tmp",
            output=root / "ray-residue.json",
            root=root,
        )


def test_verify_guard_consumes_only_matching_ephemeral_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    evidence = root / "artifacts" / "benchmark"
    evidence.mkdir(parents=True)
    token = "a" * 64
    sentinel = evidence / ray_residue.GUARD_SENTINEL
    ray_residue._write_json(
        sentinel,
        {
            "schema_version": 1,
            "token_sha256": "0" * 64,
        },
    )
    monkeypatch.setenv(ray_residue.GUARD_TOKEN_ENV, token)

    with pytest.raises(ray_residue.ResidueError, match="wrong cleanup guard"):
        ray_residue.verify_guard(evidence, root)
    assert sentinel.exists()

    ray_residue._write_json(
        sentinel,
        {
            "schema_version": 1,
            "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        },
    )
    ray_residue.verify_guard(evidence, root)

    assert not sentinel.exists()
