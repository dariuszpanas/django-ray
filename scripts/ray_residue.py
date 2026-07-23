"""Capture and enforce bounded local-Ray process, listener, and residue cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import psutil

SNAPSHOT_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 2
MAX_RECORDS = 500
OWNED_TEMP_SCAN_LIMIT = 10_000
RAY_PROCESS_NAMES = frozenset(
    {
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
    }
)
RAY_COMMAND_FRAGMENTS = (
    "ray/_private/workers/default_worker.py",
    "ray/_private/workers/setup_worker.py",
    "ray/_private/ray_process_reaper.py",
    "ray/autoscaler/_private/monitor.py",
    "ray/autoscaler/v2/monitor.py",
    "ray/dashboard/dashboard.py",
    "ray.dashboard",
    "ray.util.client.server",
    "dashboard_agent.py",
    "log_monitor.py",
    "runtime_env_agent.py",
)
SHARED_MEMORY_PREFIXES = ("plasma", "psm_", "ray", "sem.")
GUARD_TOKEN_ENV = "DJANGO_RAY_PHASED_GUARD_TOKEN"
GUARD_SENTINEL = ".ray-cleanup-guard.json"


class ResidueError(ValueError):
    """Raised when bounded Ray cleanup evidence is incomplete."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResidueError(f"cannot load Ray residue snapshot from {path}") from error
    if not isinstance(value, dict):
        raise ResidueError("Ray residue snapshot must be a JSON object")
    return cast(dict[str, Any], value)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, pending_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".pending", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
        Path(pending_name).replace(path)
    finally:
        Path(pending_name).unlink(missing_ok=True)


def _is_ray_process(name: str, command: str) -> bool:
    lowered_name = name.lower()
    normalized_name = lowered_name.removesuffix(".exe")
    lowered_command = command.lower().replace("\\", "/")
    return (
        normalized_name in RAY_PROCESS_NAMES
        or normalized_name.startswith("ray::")
        or any(fragment in lowered_command for fragment in RAY_COMMAND_FRAGMENTS)
    )


def _bounded_entries(path: Path, prefixes: tuple[str, ...] | None = None) -> list[str]:
    if not path.is_dir():
        return []
    entries = sorted(
        entry.name
        for entry in path.iterdir()
        if prefixes is None or entry.name.lower().startswith(prefixes)
    )
    if len(entries) > MAX_RECORDS:
        raise ResidueError(f"Ray residue snapshot exceeded {MAX_RECORDS} entries in {path}")
    return entries


def capture_snapshot() -> dict[str, Any]:
    """Capture a bounded read-only view of active Ray-owned state."""
    processes: list[dict[str, object]] = []
    listeners: list[dict[str, object]] = []
    errors: list[str] = []
    for process in psutil.process_iter(("pid", "name", "cmdline")):
        try:
            name = str(process.info.get("name") or "")
            command = " ".join(str(part) for part in process.info.get("cmdline") or ())
            if not _is_ray_process(name, command):
                continue
            processes.append(
                {
                    "pid": process.pid,
                    "name": name,
                    "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                }
            )
            for connection in process.net_connections(kind="inet"):
                if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                    continue
                listeners.append(
                    {
                        "pid": process.pid,
                        "host": connection.laddr.ip,
                        "port": connection.laddr.port,
                    }
                )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as error:
            errors.append(f"pid {process.pid}: {type(error).__name__}")
    if len(processes) > MAX_RECORDS or len(listeners) > MAX_RECORDS:
        raise ResidueError("Ray process or listener snapshot exceeded its bounded record limit")
    shared_memory = _bounded_entries(Path("/dev/shm"), SHARED_MEMORY_PREFIXES)
    global_temp = _bounded_entries(Path("/tmp/ray"))
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "complete": not errors,
        "errors": errors,
        "processes": sorted(processes, key=lambda record: int(record["pid"])),
        "listeners": sorted(
            listeners,
            key=lambda record: (int(record["pid"]), str(record["host"]), int(record["port"])),
        ),
        "shared_memory": shared_memory,
        "global_temp": global_temp,
    }


def _record_identities(
    records: object, fields: tuple[str, ...], label: str
) -> set[tuple[object, ...]]:
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ResidueError(f"Ray residue {label} must contain object records")
    return {
        tuple(record.get(field) for field in fields)
        for record in records
        if isinstance(record, dict)
    }


def _validate_string_entries(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(entry, str) or not entry for entry in value)
        or len(value) != len(set(value))
    ):
        raise ResidueError(f"Ray residue {label} must contain unique non-empty strings")
    return cast(list[str], value)


def _validate_snapshot(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "complete",
        "errors",
        "processes",
        "listeners",
        "shared_memory",
        "global_temp",
    }:
        raise ResidueError(f"Ray residue {label} has an incomplete schema")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
    ):
        raise ResidueError(f"Ray residue {label} has the wrong schema")
    if value.get("complete") is not True or value.get("errors") != []:
        raise ResidueError(f"Ray residue {label} is incomplete")
    processes = value.get("processes")
    if not isinstance(processes, list):
        raise ResidueError(f"Ray residue {label} processes must be a list")
    process_records = cast(list[object], processes)
    validated_processes: list[dict[str, object]] = []
    for record in process_records:
        if not isinstance(record, dict) or set(record) != {"pid", "name", "command_sha256"}:
            raise ResidueError(f"Ray residue {label} process entry is invalid")
        pid = record.get("pid")
        if (
            type(pid) is not int
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("command_sha256"), str)
            or len(record["command_sha256"]) != 64
        ):
            raise ResidueError(f"Ray residue {label} process entry is invalid")
        if pid < 1:
            raise ResidueError(f"Ray residue {label} process entry is invalid")
        validated_processes.append(cast(dict[str, object], record))
    listeners = value.get("listeners")
    if not isinstance(listeners, list):
        raise ResidueError(f"Ray residue {label} listeners must be a list")
    listener_records = cast(list[object], listeners)
    validated_listeners: list[dict[str, object]] = []
    for record in listener_records:
        if not isinstance(record, dict) or set(record) != {"pid", "host", "port"}:
            raise ResidueError(f"Ray residue {label} listener entry is invalid")
        pid = record.get("pid")
        port = record.get("port")
        if (
            type(pid) is not int
            or not isinstance(record.get("host"), str)
            or not record["host"]
            or type(port) is not int
        ):
            raise ResidueError(f"Ray residue {label} listener entry is invalid")
        if pid < 1 or not 0 < port < 65_536:
            raise ResidueError(f"Ray residue {label} listener entry is invalid")
        validated_listeners.append(cast(dict[str, object], record))
    process_pids = [record["pid"] for record in validated_processes]
    if len(process_pids) != len(set(process_pids)):
        raise ResidueError(f"Ray residue {label} contains duplicate process identities")
    listener_identities = [
        (record["pid"], record["host"], record["port"]) for record in validated_listeners
    ]
    if len(listener_identities) != len(set(listener_identities)):
        raise ResidueError(f"Ray residue {label} contains duplicate listener identities")
    _validate_string_entries(value.get("shared_memory"), f"{label} shared_memory")
    _validate_string_entries(value.get("global_temp"), f"{label} global_temp")
    return cast(dict[str, Any], value)


def _validate_fresh_baseline(value: object) -> dict[str, Any]:
    baseline = _validate_snapshot(value, "baseline")
    if any(baseline[field] for field in ("processes", "listeners", "shared_memory", "global_temp")):
        raise ResidueError("Ray residue baseline is not a clean fresh-runner state")
    return baseline


def _owned_temp_entry_scan(path: Path, *, limit: int = OWNED_TEMP_SCAN_LIMIT) -> tuple[int, bool]:
    """Return bounded entries observed and whether the exact count was truncated."""
    entries_observed = 0
    if not path.exists():
        return entries_observed, False
    directories = [path]
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_observed += 1
                    if entries_observed > limit:
                        return limit + 1, True
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(Path(entry.path))
        except OSError as error:
            raise ResidueError("owned Ray temporary directory could not be scanned") from error
    return entries_observed, False


def assert_clean(
    baseline: dict[str, Any], owned_temp: Path, root: Path | None = None
) -> dict[str, Any]:
    """Compare a fresh-runner baseline, remove only owned temp logs, and fail on residue."""
    baseline = _validate_fresh_baseline(baseline)
    repository = (root or Path.cwd()).resolve()
    resolved_owned = owned_temp.resolve()
    try:
        resolved_owned.relative_to(repository)
    except ValueError as error:
        raise ResidueError(
            "owned Ray temporary directory must stay inside the repository"
        ) from error
    if resolved_owned == repository or resolved_owned.name != "ray-tmp":
        raise ResidueError("owned Ray temporary directory must be a dedicated ray-tmp path")

    current = _validate_snapshot(capture_snapshot(), "post-run snapshot")
    process_fields = ("pid", "name", "command_sha256")
    listener_fields = ("pid", "host", "port")
    process_additions = sorted(
        _record_identities(current["processes"], process_fields, "processes")
        - _record_identities(baseline.get("processes"), process_fields, "baseline processes")
    )
    listener_additions = sorted(
        _record_identities(current["listeners"], listener_fields, "listeners")
        - _record_identities(baseline.get("listeners"), listener_fields, "baseline listeners")
    )
    shared_additions = sorted(
        set(current["shared_memory"]) - set(baseline.get("shared_memory", []))
    )
    global_temp_additions = sorted(
        set(current["global_temp"]) - set(baseline.get("global_temp", []))
    )
    errors: list[str] = []
    if process_additions:
        errors.append("Ray processes remain after the canonical target")
    if listener_additions:
        errors.append("Ray listeners remain after the canonical target")
    if shared_additions:
        errors.append("shared-memory objects remain after the canonical target")
    if global_temp_additions:
        errors.append("Ray created unowned entries under /tmp/ray")
    entries_observed: int | None
    scan_truncated: bool | None
    scan_error: str | None
    try:
        entries_observed, scan_truncated = _owned_temp_entry_scan(resolved_owned)
        scan_error = None
    except ResidueError as error:
        entries_observed = None
        scan_truncated = None
        scan_error = str(error)[:500]
    external_residue = bool(errors)
    removed_owned_temp = False
    if not external_residue and resolved_owned.exists():
        try:
            shutil.rmtree(resolved_owned)
        except OSError:
            errors.append("owned Ray temporary state could not be removed")
        else:
            removed_owned_temp = True
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "additions": {
            "processes": [list(identity) for identity in process_additions],
            "listeners": [list(identity) for identity in listener_additions],
            "shared_memory": shared_additions,
            "global_temp": global_temp_additions,
        },
        "owned_temp": {
            "entries_observed": entries_observed,
            "scan_limit": OWNED_TEMP_SCAN_LIMIT,
            "scan_truncated": scan_truncated,
            "scan_error": scan_error,
            "removed": removed_owned_temp,
            "exists_after": resolved_owned.exists(),
        },
    }


def _guard_failure_report(error: ResidueError, owned_temp: Path) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "valid": False,
        "errors": [f"cleanup evidence is invalid: {error}"[:500]],
        "additions": {
            "processes": [],
            "listeners": [],
            "shared_memory": [],
            "global_temp": [],
        },
        "owned_temp": {
            "entries_observed": None,
            "scan_limit": OWNED_TEMP_SCAN_LIMIT,
            "scan_truncated": None,
            "scan_error": None,
            "removed": False,
            "exists_after": owned_temp.exists(),
        },
    }


def verify_guard(output_directory: Path, root: Path | None = None) -> None:
    """Consume proof that the internal Make body is running under cleanup."""
    repository = (root or Path.cwd()).resolve()
    resolved_output = output_directory.resolve()
    try:
        resolved_output.relative_to(repository)
    except ValueError as error:
        raise ResidueError("guard evidence directory must stay inside the repository") from error
    if resolved_output == repository:
        raise ResidueError("guard evidence directory cannot be the repository root")
    token = os.environ.get(GUARD_TOKEN_ENV, "")
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise ResidueError("internal phased target requires the active cleanup guard")
    sentinel_path = resolved_output / GUARD_SENTINEL
    sentinel = _load_json(sentinel_path)
    if (
        set(sentinel) != {"schema_version", "token_sha256"}
        or type(sentinel.get("schema_version")) is not int
        or sentinel.get("schema_version") != 1
    ):
        raise ResidueError("internal phased cleanup guard proof is malformed")
    expected_digest = sentinel.get("token_sha256")
    actual_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    if not isinstance(expected_digest, str) or not secrets.compare_digest(
        actual_digest, expected_digest
    ):
        raise ResidueError("internal phased target has the wrong cleanup guard")
    sentinel_path.unlink()


def guarded_run(
    command: list[str],
    *,
    baseline_path: Path,
    owned_temp: Path,
    output: Path,
    root: Path | None = None,
) -> int:
    """Run a canonical Make body and always emit bounded cleanup evidence."""
    if not command:
        raise ResidueError("guarded Ray cleanup requires a command")
    repository = (root or Path.cwd()).resolve()
    resolved_owned = owned_temp.resolve()
    try:
        resolved_owned.relative_to(repository)
    except ValueError as error:
        raise ResidueError(
            "owned Ray temporary directory must stay inside the repository"
        ) from error
    if resolved_owned == repository or resolved_owned.name != "ray-tmp":
        raise ResidueError("owned Ray temporary directory must be a dedicated ray-tmp path")
    evidence_directory = resolved_owned.parent
    if evidence_directory == repository:
        raise ResidueError("guard evidence directory cannot be the repository root")
    if baseline_path.resolve() != evidence_directory / "ray-baseline.json":
        raise ResidueError("guard baseline must be the exact sibling ray-baseline.json path")
    if output.resolve() != evidence_directory / "ray-residue.json":
        raise ResidueError("guard output must be the exact sibling ray-residue.json path")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(output.resolve())],
        cwd=repository,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if ignored.returncode != 0:
        raise ResidueError("guard evidence directory must be ignored by Git")

    try:
        baseline = _validate_fresh_baseline(_load_json(baseline_path))
    except ResidueError as error:
        _write_json(output, _guard_failure_report(error, resolved_owned))
        raise

    token = secrets.token_hex(32)
    sentinel_path = evidence_directory / GUARD_SENTINEL
    _write_json(
        sentinel_path,
        {
            "schema_version": 1,
            "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        },
    )
    command_environment = os.environ.copy()
    command_environment[GUARD_TOKEN_ENV] = token
    primary_status = 0
    cleanup_status = 0
    try:
        try:
            primary_status = subprocess.run(
                command,
                check=False,
                close_fds=False,
                env=command_environment,
            ).returncode
        except OSError as error:
            print(f"Ray residue guard could not run canonical body: {error}", file=sys.stderr)
            primary_status = 2
    finally:
        sentinel_path.unlink(missing_ok=True)
        try:
            report = assert_clean(baseline, resolved_owned, repository)
        except ResidueError as error:
            report = _guard_failure_report(error, resolved_owned)
        if report["valid"] is not True:
            cleanup_status = 2
            print("Ray residue guard detected incomplete cleanup", file=sys.stderr)
        report["guard"] = {
            "body_returncode": primary_status,
            "cleanup_returncode": cleanup_status,
        }
        _write_json(output, report)
    return cleanup_status or primary_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="capture the fresh-runner baseline")
    snapshot.add_argument("--output", type=Path, required=True)
    clean = subparsers.add_parser("assert-clean", help="assert and record post-run cleanup")
    clean.add_argument("--baseline", type=Path, required=True)
    clean.add_argument("--owned-temp-dir", type=Path, required=True)
    clean.add_argument("--output", type=Path, required=True)
    guard = subparsers.add_parser(
        "guard", help="run a command and always assert post-run Ray cleanup"
    )
    guard.add_argument("--baseline", type=Path, required=True)
    guard.add_argument("--owned-temp-dir", type=Path, required=True)
    guard.add_argument("--output", type=Path, required=True)
    guard.add_argument("guard_command", nargs=argparse.REMAINDER)
    verify = subparsers.add_parser(
        "verify-guard", help="verify the internal phased Make body is cleanup-guarded"
    )
    verify.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "snapshot":
            snapshot = capture_snapshot()
            _write_json(arguments.output, snapshot)
            _validate_fresh_baseline(snapshot)
            return 0
        if arguments.command == "guard":
            command = arguments.guard_command
            if command[:1] == ["--"]:
                command = command[1:]
            return guarded_run(
                command,
                baseline_path=arguments.baseline,
                owned_temp=arguments.owned_temp_dir,
                output=arguments.output,
            )
        if arguments.command == "verify-guard":
            verify_guard(arguments.output_dir)
            return 0
        try:
            report = assert_clean(_load_json(arguments.baseline), arguments.owned_temp_dir)
        except ResidueError as error:
            report = _guard_failure_report(error, arguments.owned_temp_dir.resolve())
            _write_json(arguments.output, report)
            raise
        _write_json(arguments.output, report)
        return 0 if report["valid"] else 2
    except ResidueError as error:
        print(f"Ray residue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
