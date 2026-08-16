"""Contracts for daemonless local validation resource coordination."""

from __future__ import annotations

import ctypes
import errno
import inspect
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import BinaryIO, cast

import psutil
import pytest

from scripts import local_resource_coordinator as coordinator


class _CustomSignalError(RuntimeError):
    pass


def _invoke_installed_sigint_handler() -> None:
    """Synchronously exercise the installed Python handler across host OSes."""

    installed = signal.getsignal(signal.SIGINT)
    assert callable(installed)
    handler = cast(Callable[[int, FrameType | None], object], installed)
    handler(signal.SIGINT, None)


def _pid_is_absent_or_zombie(pid: int) -> bool:
    if coordinator._pid_presence(pid) == "absent":
        return True
    if os.name != "posix":
        return False
    if sys.platform.startswith("linux"):
        process_stat = coordinator._linux_proc_stat(pid)
        return process_stat is not None and process_stat.state == "Z"
    if sys.platform == "darwin":
        try:
            return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            return False
    return False


STATUS_KEYS = {
    "schema_version",
    "observed_at",
    "state",
    "safe_action",
    "termination_authority",
    "resources",
    "active",
    "queue",
    "queue_total",
    "orphaned",
    "last_completed",
    "local_liveness",
    "state_registry",
    "kubernetes_mirror",
    "deployed_stack",
    "diagnostics",
}
ACTIVE_KEYS = {
    "run_id",
    "profile",
    "resources",
    "phase",
    "queue_position",
    "owner",
    "source",
    "intent",
    "handoff",
    "acquired_at",
    "heartbeat_at",
    "expiry_at",
    "selected_count",
    "child",
    "outcome",
    "postcondition",
    "liveness",
    "legacy",
}
QUEUE_KEYS = {
    "request_id",
    "ticket",
    "profile",
    "resources",
    "phase",
    "queue_position",
    "owner",
    "source",
    "intent",
    "handoff",
    "requested_at",
    "liveness",
}


def _private_state_dir(tmp_path: Path) -> Path:
    parent = tmp_path / "private-state-parent"
    if os.name == "nt":
        assert coordinator._WINDOWS_CURRENT_USER_SID is not None
        coordinator._windows_create_private_directory(
            parent,
            sid=coordinator._WINDOWS_CURRENT_USER_SID,
        )
    else:
        parent.mkdir(mode=0o700)
    return parent / "state"


def _use_isolated_coordinator_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    state_dir = _private_state_dir(tmp_path)
    lock_path = tmp_path / "legacy-owner.lock"
    monkeypatch.setattr(coordinator, "DEFAULT_LOCAL_RESOURCE_STATE_DIR", state_dir)
    monkeypatch.setattr(coordinator, "DEFAULT_REAL_RAY_LOCK_PATH", lock_path)
    for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return state_dir, lock_path


def _launch_owned_for_test(
    *,
    lease: coordinator.LocalResourceLease,
    command: list[str],
    rootpath: Path,
) -> coordinator._OwnedLocalCommand:
    """Exercise the private launcher while restoring its lifecycle handler."""

    sigint_guard = coordinator._MainThreadSigintGuard()
    sigint_guard.install()
    try:
        return coordinator._launch_owned_command(
            lease=lease,
            command=command,
            rootpath=rootpath,
            sigint_guard=sigint_guard,
        )
    finally:
        sigint_guard.enter_suppress_mode()
        sigint_guard.restore_and_replay()


def _active_record(*, pid: int, process_birth: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "a" * 32,
        "ticket": 1,
        "profile": "ci-final",
        "resources": ["host-heavy"],
        "phase": "tests",
        "owner": {
            "owner": "owner",
            "session": "session",
            "agent": "agent",
            "model": "model",
            "host_id": "sha256:" + ("b" * 16),
            "pid": pid,
            "process_birth": process_birth,
        },
        "source": {
            "worktree": "worktree",
            "branch": "branch",
            "commit": "c" * 40,
            "source_tree": "d" * 40,
            "dirty": False,
        },
        "rootpath_sha256": "f" * 64,
        "intent": "test",
        "handoff": None,
        "acquired_at": "2026-08-15T00:00:00+00:00",
        "heartbeat_at": "2026-08-15T00:00:00+00:00",
        "expiry_at": None,
        "selected_count": None,
        "child": None,
        "outcome": None,
        "postcondition": None,
        "capability_sha256": "e" * 64,
    }


def _queue_item(
    tmp_path: Path,
    *,
    request_id: str = "b" * 32,
    ticket: int = 1,
) -> dict[str, object]:
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    return coordinator._new_queue_item(
        request_id=request_id,
        ticket=ticket,
        profile="ci-final",
        phase="waiting",
        owner=coordinator.LocalResourceOwner(
            owner="owner",
            session="session",
            agent="agent",
            model="model",
            pid=os.getpid(),
            process_birth=birth,
        ),
        source=coordinator.LocalResourceSource(worktree=str(tmp_path)),
        intent="test",
        handoff="handoff",
    )


def test_phase_one_profiles_share_one_fixed_host_heavy_resource() -> None:
    assert dict(coordinator.LOCAL_RESOURCE_PROFILES) == {
        "ci-final": ("host-heavy",),
        "kuberay-final": ("host-heavy",),
        "real-ray": ("host-heavy",),
    }

    with pytest.raises(TypeError):
        coordinator.LOCAL_RESOURCE_PROFILES["unexpected"] = ("other",)  # type: ignore[index]

    assert str(coordinator.LocalResourceBusyError()) == (
        "local resources remain busy after the bounded wait"
    )


def test_exported_acquisition_and_cli_surfaces_are_stable() -> None:
    acquire = inspect.signature(coordinator.acquire_local_resources)
    assert tuple(acquire.parameters) == (
        "profile",
        "phase",
        "rootpath",
        "selected_count",
        "timeout_seconds",
        "progress_interval_seconds",
        "progress",
        "on_acquired",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in acquire.parameters.values()
    )
    assert acquire.parameters["timeout_seconds"].default == 14_400
    assert acquire.parameters["progress_interval_seconds"].default == 30
    inherited = inspect.signature(coordinator.require_inherited_local_resources)
    assert tuple(inherited.parameters) == ("profile", "rootpath")
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in inherited.parameters.values()
    )

    parsed = coordinator._parser().parse_args(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "tests",
            "--root",
            ".",
            "--timeout",
            "60",
            "--",
            "python",
            "-V",
        ]
    )
    assert parsed.command == "run"
    assert parsed.run_command == ["--", "python", "-V"]


def test_legacy_and_coordinator_active_records_have_one_exact_key_schema() -> None:
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    coordinator_active = coordinator._public_active_record(
        _active_record(pid=os.getpid(), process_birth=birth)
    )
    legacy_active = coordinator._legacy_active_status(
        {"pid": os.getpid(), "hostname": "host", "rootpath": "worktree"}
    )

    assert set(coordinator_active) == ACTIVE_KEYS
    assert set(legacy_active) == ACTIVE_KEYS


def test_internal_inheritance_environment_has_one_exported_scrub_inventory() -> None:
    assert coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS == (
        "DJANGO_RAY_LOCAL_RUN_ID",
        "DJANGO_RAY_LOCAL_LEASE_TOKEN",
        "DJANGO_RAY_LOCAL_PROFILE",
        "DJANGO_RAY_LOCAL_STATE_DIR",
    )
    assert set(coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS) < set(
        coordinator.LOCAL_RESOURCE_ENV_KEYS
    )


def test_owner_and_source_diagnostics_are_allowlisted_printable_and_bounded() -> None:
    owner = coordinator.LocalResourceOwner(
        owner="owner\n\u202e" + ("é" * 10_000),
        session="session",
        agent="agent",
        model="model",
        host_id="host",
        pid=True,
        process_birth="birth",
    )
    source = coordinator.LocalResourceSource(
        worktree="worktree\r" + ("x" * 10_000),
        branch="branch",
        commit="commit",
        source_tree="tree",
    )

    assert set(owner.as_dict()) == {
        "owner",
        "session",
        "agent",
        "model",
        "host_id",
        "pid",
        "process_birth",
    }
    assert owner.owner is not None
    assert "\n" not in owner.owner
    assert "\u202e" not in owner.owner
    assert "é" not in owner.owner
    assert owner.owner.endswith("...")
    assert len(json.dumps(owner.owner, ensure_ascii=True).encode("ascii")) <= 512
    assert owner.pid is None
    assert set(source.as_dict()) == {"worktree", "branch", "commit", "source_tree", "dirty"}
    assert source.dirty is None
    assert source.worktree is not None
    assert "\r" not in source.worktree
    assert source.worktree.endswith("...")


def test_private_state_directory_is_created_once_with_private_posix_mode(
    tmp_path: Path,
) -> None:
    state_dir = _private_state_dir(tmp_path)

    coordinator._ensure_private_state_dir(state_dir)
    coordinator._ensure_private_state_dir(state_dir)

    assert coordinator._validate_private_state_dir(state_dir, allow_absent=False) is True
    if os.name == "posix":
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    else:
        assert coordinator._WINDOWS_CURRENT_USER_SID is not None
        assert coordinator._WINDOWS_CURRENT_USER_SID in str(
            coordinator.DEFAULT_LOCAL_RESOURCE_STATE_DIR
        )
        assert coordinator._windows_acl_is_safe(
            state_dir,
            expected_sid=coordinator._WINDOWS_CURRENT_USER_SID,
            require_private=True,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX per-user state regression")
def test_posix_default_state_parent_is_stable_account_home(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    home_dir = tmp_path / "home"
    home_dir.mkdir(mode=0o700)

    assert (
        coordinator._default_local_resource_state_parent(
            environment={"XDG_RUNTIME_DIR": str(runtime_dir)},
            home=home_dir,
        )
        == home_dir
    )
    assert (
        coordinator._default_local_resource_state_parent(
            environment={},
            home=home_dir,
        )
        == home_dir
    )
    assert (
        coordinator._default_local_resource_state_parent(
            environment={"XDG_RUNTIME_DIR": ""},
            home=home_dir,
        )
        == home_dir
    )
    assert (
        coordinator._default_local_resource_state_parent(
            environment={"XDG_RUNTIME_DIR": "relative/runtime"},
            home=home_dir,
        )
        == home_dir
    )
    assert (
        coordinator._default_local_resource_state_parent(
            environment={"TMPDIR": str(tmp_path / "different-temp")},
            home=home_dir,
        )
        == home_dir
    )

    state_dir = home_dir / "django-ray-local-resources"
    coordinator._ensure_private_state_dir(state_dir)
    assert coordinator._validate_private_state_dir(state_dir, allow_absent=False) is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX per-user state regression")
def test_posix_default_authority_domain_is_identical_across_optional_environment() -> None:
    command = (
        "from scripts import local_resource_coordinator as c; "
        "print(c.DEFAULT_LOCAL_RESOURCE_STATE_DIR)"
    )
    clean_environment = os.environ.copy()
    clean_environment.pop("XDG_RUNTIME_DIR", None)
    clean_environment.pop("TMPDIR", None)
    varied_environment = clean_environment | {
        "XDG_RUNTIME_DIR": "/run/user/123456",
        "TMPDIR": "/var/tmp/different",
    }

    paths = [
        subprocess.run(
            [sys.executable, "-c", command],
            cwd=Path(__file__).parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        for environment in (clean_environment, varied_environment)
    ]
    assert paths[0] == paths[1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX per-user state regression")
@pytest.mark.parametrize("use_runtime_dir", [True, False], ids=("xdg-runtime", "home"))
def test_posix_default_state_parent_rejects_writable_xdg_and_home_bases(
    tmp_path: Path,
    use_runtime_dir: bool,
) -> None:
    unsafe_base = tmp_path / ("runtime" if use_runtime_dir else "home")
    unsafe_base.mkdir(mode=0o700)
    unsafe_base.chmod(0o770)
    environment = {"XDG_RUNTIME_DIR": str(unsafe_base)} if use_runtime_dir else {}
    selected = coordinator._default_local_resource_state_parent(
        environment=environment,
        home=unsafe_base,
    )

    with pytest.raises(coordinator.LocalResourceStatePathError):
        coordinator._validate_private_state_dir(
            selected / "django-ray-local-resources",
            allow_absent=True,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX per-user state regression")
def test_posix_state_parent_rejects_a_replaceable_controlling_directory(
    tmp_path: Path,
) -> None:
    replaceable = tmp_path / "replaceable"
    replaceable.mkdir(mode=0o700)
    replaceable.chmod(0o777)
    account_home = replaceable / "account-home"
    account_home.mkdir(mode=0o700)

    with pytest.raises(coordinator.LocalResourceStatePathError):
        coordinator._validate_private_state_dir(
            account_home / "django-ray-local-resources",
            allow_absent=True,
        )


def test_private_state_directory_refuses_a_redirected_path(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    state_dir = _private_state_dir(tmp_path)
    try:
        state_dir.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this platform: {error}")

    with pytest.raises(coordinator.LocalResourceStatePathError, match="unsafe state path"):
        coordinator._validate_private_state_dir(state_dir, allow_absent=False)


def test_linux_birth_identity_binds_boot_and_start_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coordinator,
        "_linux_proc_stat",
        lambda _pid: coordinator._LinuxProcStat(
            parent_pid=41,
            process_group=42,
            session=42,
            start_ticks="123456",
            state="S",
        ),
    )
    monkeypatch.setattr(
        coordinator,
        "_linux_boot_identity",
        lambda: "12345678-1234-5678-9234-567812345678",
    )

    snapshot = coordinator._linux_process_snapshot(42)

    assert snapshot == coordinator._PosixProcessSnapshot(
        parent_pid=41,
        process_group=42,
        session=42,
        start_identity=("linux-boot:12345678-1234-5678-9234-567812345678:proc-start-ticks:123456"),
    )


@pytest.mark.parametrize("payload", ["", "not-a-uuid", "{12345678-1234-5678-9234-567812345678}"])
def test_linux_boot_identity_rejects_missing_or_noncanonical_values(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    monkeypatch.setattr(Path, "read_text", lambda _path, **_kwargs: payload)

    assert coordinator._linux_boot_identity() is None


def test_process_liveness_rejects_same_pid_and_ticks_from_another_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_birth = "linux-boot:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:proc-start-ticks:10"
    new_birth = "linux-boot:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb:proc-start-ticks:10"
    monkeypatch.setattr(coordinator, "_pid_presence", lambda _pid: "present")
    monkeypatch.setattr(coordinator, "_process_birth", lambda _pid: new_birth)

    assert coordinator._process_liveness(42, old_birth) == "dead"


def test_linux_group_scan_stops_at_the_explicit_process_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yielded: list[int] = []

    class FakeEntry:
        def __init__(self, name: str) -> None:
            self.name = name

    class BoundedEntries:
        def __enter__(self) -> BoundedEntries:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> Iterator[FakeEntry]:
            for pid in (101, 102, 103, 104):
                yielded.append(pid)
                yield FakeEntry(str(pid))

    monkeypatch.setattr(coordinator, "_MAX_POSIX_GROUP_MEMBERS", 2)
    monkeypatch.setattr(coordinator.os, "scandir", lambda _path: BoundedEntries())
    monkeypatch.setattr(
        coordinator,
        "_linux_proc_stat",
        lambda _pid: coordinator._LinuxProcStat(1, 900, 900, "1", "S"),
    )

    assert coordinator._linux_posix_process_group_members(900) is None
    assert yielded == [101, 102, 103]


def test_linux_group_scan_skips_unrelated_zero_group_records_without_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_group = 900

    class FakeEntry:
        def __init__(self, pid: int) -> None:
            self.name = str(pid)

    class ProcEntries:
        def __enter__(self) -> ProcEntries:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> Iterator[FakeEntry]:
            return iter((FakeEntry(1), FakeEntry(process_group)))

    def proc_stat_payload(pid: int, *, group: int, session: int) -> str:
        middle_fields = " ".join("0" for _ in range(15))
        return f"{pid} (container process) S 0 {group} {session} {middle_fields} {pid * 10}\n"

    stat_payloads = {
        Path("/proc") / "1" / "stat": proc_stat_payload(1, group=0, session=0),
        Path("/proc") / str(process_group) / "stat": proc_stat_payload(
            process_group,
            group=process_group,
            session=process_group,
        ),
    }

    def read_proc_text(path: Path, *, encoding: str) -> str:
        assert encoding == "ascii"
        payload = stat_payloads.get(path)
        if payload is None:
            raise AssertionError(f"authority lookup unexpectedly read {path}")
        return payload

    monkeypatch.setattr(Path, "read_text", read_proc_text)
    monkeypatch.setattr(coordinator.os, "scandir", lambda _path: ProcEntries())
    monkeypatch.setattr(coordinator, "_pid_presence", lambda _pid: "present")

    assert coordinator._linux_proc_stat(1) == coordinator._LinuxProcStat(
        parent_pid=0,
        process_group=0,
        session=0,
        start_ticks="10",
        state="S",
    )
    assert coordinator._linux_process_snapshot(1) is None
    assert coordinator._linux_posix_process_group_members(process_group) == frozenset(
        {process_group}
    )


def test_linux_group_snapshot_excludes_only_nonleader_zombies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_group = 900
    boot_identity = "12345678-1234-5678-9234-567812345678"
    proc_pids = [process_group, 901, 902]

    class FakeEntry:
        def __init__(self, pid: int) -> None:
            self.name = str(pid)

    class ProcEntries:
        def __enter__(self) -> ProcEntries:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> Iterator[FakeEntry]:
            return iter(FakeEntry(pid) for pid in proc_pids)

    def proc_stat_payload(pid: int, *, parent_pid: int, state: str) -> str:
        middle_fields = " ".join("0" for _ in range(15))
        return (
            f"{pid} (contained worker) {state} {parent_pid} {process_group} "
            f"{process_group} {middle_fields} {pid * 10}\n"
        )

    stat_payloads = {
        Path("/proc") / str(process_group) / "stat": proc_stat_payload(
            process_group,
            parent_pid=1,
            state="Z",
        ),
        Path("/proc") / "901" / "stat": proc_stat_payload(
            901,
            parent_pid=1,
            state="Z",
        ),
        Path("/proc") / "902" / "stat": proc_stat_payload(
            902,
            parent_pid=1,
            state="S",
        ),
    }
    boot_path = Path("/proc") / "sys" / "kernel" / "random" / "boot_id"

    def read_proc_text(path: Path, *, encoding: str) -> str:
        assert encoding == "ascii"
        if path == boot_path:
            return f"{boot_identity}\n"
        payload = stat_payloads.get(path)
        if payload is None:
            raise FileNotFoundError(path)
        return payload

    def scan_proc(path: str) -> ProcEntries:
        assert path == "/proc"
        return ProcEntries()

    monkeypatch.setattr(Path, "read_text", read_proc_text)
    monkeypatch.setattr(coordinator.os, "scandir", scan_proc)
    monkeypatch.setattr(coordinator.sys, "platform", "linux")
    monkeypatch.setattr(coordinator, "_pid_presence", lambda _pid: "present")
    monkeypatch.setattr(coordinator, "_process_birth", coordinator._posix_process_birth)
    monkeypatch.setattr(coordinator, "_posix_process_group_presence", lambda _pgid: "present")

    expected_birth = f"linux-boot:{boot_identity}:proc-start-ticks:{process_group * 10}"
    assert coordinator._linux_proc_stat(process_group) == coordinator._LinuxProcStat(
        parent_pid=1,
        process_group=process_group,
        session=process_group,
        start_ticks=str(process_group * 10),
        state="Z",
    )
    assert coordinator._process_liveness(process_group, expected_birth) == "live"
    assert coordinator._linux_posix_process_group_members(process_group) == frozenset(
        {process_group, 902}
    )

    proc_pids.remove(902)
    assert (
        coordinator._posix_process_group_survivor_presence(
            process_group,
            leader_pid=process_group,
        )
        == "absent"
    )


def test_darwin_boot_identity_reads_canonical_boot_session_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"12345678-1234-5678-9234-567812345678\0"
    calls: list[bool] = []

    class FakeSysctlByName:
        argtypes: object = None
        restype: object = None

        def __call__(
            self,
            name: bytes,
            old_value: object,
            old_size: object,
            new_value: object,
            new_size: int,
        ) -> int:
            assert name == b"kern.bootsessionuuid"
            assert new_value is None
            assert new_size == 0
            size = ctypes.cast(old_size, ctypes.POINTER(ctypes.c_size_t)).contents
            calls.append(old_value is None)
            if old_value is None:
                size.value = len(payload)
            else:
                ctypes.memmove(old_value, payload, len(payload))
                size.value = len(payload)
            return 0

    class FakeLibc:
        sysctlbyname = FakeSysctlByName()

    monkeypatch.setattr(coordinator, "_load_darwin_libc", FakeLibc)

    assert coordinator._darwin_boot_identity() == "12345678-1234-5678-9234-567812345678"
    assert calls == [True, False]


def test_darwin_proc_identity_requires_exact_bsdinfo_size_and_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int, int]] = []

    class FakeProcPidInfo:
        argtypes: object = None
        restype: object = None

        def __call__(
            self,
            pid: int,
            flavor: int,
            argument: int,
            buffer: object,
            size: int,
        ) -> int:
            calls.append((pid, flavor, argument, size))
            assert size == ctypes.sizeof(coordinator._DarwinProcBsdInfo)
            info = ctypes.cast(
                buffer,
                ctypes.POINTER(coordinator._DarwinProcBsdInfo),
            ).contents
            info.pbi_pid = pid
            info.pbi_ppid = 901
            info.pbi_pgid = 900
            info.pbi_start_tvsec = 1_700_000_000
            info.pbi_start_tvusec = 123_456
            return size

    class FakeLibproc:
        proc_pidinfo = FakeProcPidInfo()

    monkeypatch.setattr(coordinator, "_load_darwin_libproc", FakeLibproc)

    assert coordinator._darwin_proc_identity(902) == coordinator._DarwinProcIdentity(
        parent_pid=901,
        process_group=900,
        start_seconds=1_700_000_000,
        start_microseconds=123_456,
    )
    assert calls == [(902, coordinator._DARWIN_PROC_PIDTBSDINFO, 0, 136)]


def test_darwin_proc_identity_rejects_a_partial_bsdinfo_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcPidInfo:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            size = int(arguments[-1])
            return size - 1

    class FakeLibproc:
        proc_pidinfo = FakeProcPidInfo()

    monkeypatch.setattr(coordinator, "_load_darwin_libproc", FakeLibproc)

    assert coordinator._darwin_proc_identity(902) is None


def test_darwin_snapshot_birth_and_ancestry_ignore_wall_clock_adjustments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = {
        900: coordinator._DarwinProcIdentity(1, 900, 1_700_000_000, 100),
        901: coordinator._DarwinProcIdentity(900, 900, 1_700_000_001, 200),
        902: coordinator._DarwinProcIdentity(901, 900, 1_700_000_002, 300),
    }
    wall_clock_reads: list[str] = []
    wall_clock_adjustment = 0.0

    class WallClockProcess:
        def __init__(self, _pid: int) -> None:
            wall_clock_reads.append("process")

        def create_time(self) -> float:
            wall_clock_reads.append("create-time")
            return 1_900_000_000.0 + wall_clock_adjustment

    monkeypatch.setattr(coordinator.sys, "platform", "darwin")
    monkeypatch.setattr(coordinator.os, "name", "posix")
    monkeypatch.setattr(coordinator.os, "getpgid", lambda _pid: 900, raising=False)
    monkeypatch.setattr(coordinator.os, "getsid", lambda _pid: 900, raising=False)
    monkeypatch.setattr(coordinator, "_darwin_proc_identity", identities.get)
    monkeypatch.setattr(
        coordinator,
        "_darwin_boot_identity",
        lambda: "12345678-1234-5678-9234-567812345678",
    )
    monkeypatch.setattr(psutil, "Process", WallClockProcess)
    monkeypatch.setattr(
        psutil,
        "boot_time",
        lambda: wall_clock_reads.append("boot-time") or 1_600_000_000.0 + wall_clock_adjustment,
    )
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "live")

    before_adjustment = coordinator._posix_process_snapshot(902)
    wall_clock_adjustment = 3_600.0
    after_adjustment = coordinator._posix_process_snapshot(902)

    expected = coordinator._PosixProcessSnapshot(
        parent_pid=901,
        process_group=900,
        session=900,
        start_identity=(
            "darwin-boot:12345678-1234-5678-9234-567812345678:process-start:1700000002:300"
        ),
    )
    assert before_adjustment == expected
    assert after_adjustment == expected
    assert coordinator._posix_parent_pid(902) == 901
    assert coordinator._process_is_descendant(902, 900, "darwin-exact-birth") == "yes"
    assert wall_clock_reads == []


def test_unsupported_posix_does_not_mint_wall_clock_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rootpath = Path.cwd()
    monkeypatch.setattr(coordinator.sys, "platform", "freebsd14")
    monkeypatch.setattr(coordinator.os, "name", "posix")

    assert coordinator._posix_process_snapshot(902) is None
    with pytest.raises(
        coordinator.LocalResourceStateError,
        match="supports only Windows, Linux, and macOS",
    ):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="unsupported",
            rootpath=rootpath,
            timeout_seconds=1,
            command=[sys.executable, "-c", "pass"],
        )


def test_darwin_group_membership_is_exact_session_bound_and_zombie_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = {
        pid: coordinator._DarwinProcIdentity(1, 900, 1_700_000_000 + pid, pid)
        for pid in (900, 901, 902)
    }

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def status(self) -> str:
            return psutil.STATUS_ZOMBIE if self.pid == 901 else psutil.STATUS_RUNNING

    monkeypatch.setattr(psutil, "pids", lambda: [0, 800, 900, 901, 902])
    monkeypatch.setattr(psutil, "Process", FakeProcess)
    monkeypatch.setattr(
        coordinator.os,
        "getpgid",
        lambda pid: 900 if pid >= 900 else 800,
        raising=False,
    )
    monkeypatch.setattr(
        coordinator.os, "getsid", lambda pid: pid if pid == 800 else 900, raising=False
    )
    monkeypatch.setattr(coordinator, "_darwin_proc_identity", identities.get)

    assert coordinator._darwin_posix_process_group_members(900) == frozenset({900, 902})


@pytest.mark.skipif(os.name != "nt", reason="Windows process liveness regression")
def test_windows_liveness_probe_cannot_stop_an_owned_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        birth = coordinator._process_birth(child.pid)
        assert birth is not None
        assert child.poll() is None
        assert coordinator._pid_presence(child.pid) == "present"
        assert coordinator._process_liveness(child.pid, birth) == "live"
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_windows_private_state_acl_rejects_a_foreign_allow_ace(tmp_path: Path) -> None:
    state_dir = _private_state_dir(tmp_path)
    coordinator._ensure_private_state_dir(state_dir)
    result = subprocess.run(
        ["icacls", str(state_dir), "/grant", "*S-1-1-0:(RX)"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert result.returncode == 0

    with pytest.raises(coordinator.LocalResourceStatePathError):
        coordinator._validate_private_state_dir(state_dir, allow_absent=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows LocalAppData trust regression")
def test_windows_default_state_parent_is_sid_bound_and_write_safe() -> None:
    assert coordinator._WINDOWS_CURRENT_USER_SID is not None
    assert coordinator.DEFAULT_LOCAL_RESOURCE_STATE_DIR.parent == Path(os.environ["LOCALAPPDATA"])
    assert (
        coordinator._WINDOWS_CURRENT_USER_SID in coordinator.DEFAULT_LOCAL_RESOURCE_STATE_DIR.name
    )
    assert coordinator._windows_acl_is_safe(
        coordinator.DEFAULT_LOCAL_RESOURCE_STATE_DIR.parent,
        expected_sid=coordinator._WINDOWS_CURRENT_USER_SID,
        require_private=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows SID regression")
def test_windows_sid_discovery_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    monkeypatch.setattr(coordinator, "_WINDOWS_CURRENT_USER_SID", None)

    with pytest.raises(coordinator.LocalResourceStatePathError):
        coordinator._ensure_private_state_dir(state_dir)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory identity regression")
def test_windows_state_directory_identity_is_rechecked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    coordinator._ensure_private_state_dir(state_dir)
    original = state_dir.with_name("original-state")
    assert coordinator._WINDOWS_CURRENT_USER_SID is not None

    def replace_during_acl_check(
        path: Path,
        *,
        expected_sid: str,
        require_private: bool,
    ) -> bool:
        assert expected_sid == coordinator._WINDOWS_CURRENT_USER_SID
        if not require_private:
            return True
        assert path == state_dir
        state_dir.rename(original)
        coordinator._windows_create_private_directory(state_dir, sid=expected_sid)
        return True

    monkeypatch.setattr(coordinator, "_windows_acl_is_safe", replace_during_acl_check)

    with pytest.raises(coordinator.LocalResourceStatePathError):
        coordinator._validate_private_state_dir(state_dir, allow_absent=False)


def test_pid_validation_is_bounded_and_liveness_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert coordinator._safe_pid(True) is None
    assert coordinator._safe_pid(0) is None
    assert coordinator._safe_pid(coordinator._MAX_PID + 1) is None
    monkeypatch.setattr(coordinator, "_pid_presence", lambda _pid: "unknown")

    assert coordinator._process_liveness(123, "birth") == "unknown"


def test_idle_status_is_strictly_read_only(tmp_path: Path) -> None:
    lock_path = tmp_path / "legacy-owner.lock"
    state_dir = _private_state_dir(tmp_path)

    status = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )

    assert status == {
        "schema_version": 1,
        "observed_at": status["observed_at"],
        "state": "idle",
        "safe_action": "proceed",
        "termination_authority": "none",
        "resources": ["host-heavy"],
        "active": None,
        "queue": [],
        "queue_total": 0,
        "orphaned": [],
        "last_completed": None,
        "local_liveness": "idle",
        "kubernetes_mirror": {"state": "not-configured"},
        "deployed_stack": {
            "state": "not-observed",
            "provenance": "not-configured",
        },
        "diagnostics": [],
        "state_registry": "absent",
    }
    assert isinstance(status["observed_at"], str)
    assert set(status) == STATUS_KEYS
    assert lock_path.exists() is False
    assert state_dir.exists() is False


def test_status_does_not_create_a_missing_registry_mutex(tmp_path: Path) -> None:
    state_dir = _private_state_dir(tmp_path)
    coordinator._ensure_private_state_dir(state_dir)
    mutex_path = state_dir / coordinator.QUEUE_LOCK_FILE

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert status["state"] == "idle"
    assert status["state_registry"] == "present"
    assert mutex_path.exists() is False


def test_unlocked_stale_legacy_metadata_reports_idle_without_rewriting(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "legacy-owner.lock"
    original = b'\0{"pid":4242,"rootpath":"stale"}'
    lock_path.write_bytes(original)
    original_mtime = lock_path.stat().st_mtime_ns

    status = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=_private_state_dir(tmp_path),
    )

    assert status["state"] == "idle"
    assert status["safe_action"] == "proceed"
    assert lock_path.read_bytes() == original
    assert lock_path.stat().st_mtime_ns == original_mtime


def test_free_lock_with_unknown_root_liveness_blocks_as_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            _active_record(pid=os.getpid(), process_birth=birth),
        )
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "unknown")

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    assert status["local_liveness"] == "owner-liveness-unknown"


def test_free_lock_with_indeterminate_child_liveness_is_unknown_not_orphaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    active = _active_record(pid=41_001, process_birth="root-birth")
    active["child"] = {
        "pid": 41_002,
        "process_birth": "child-birth",
        "tree_kind": "windows-job" if os.name == "nt" else "posix-process-group",
    }
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            active,
        )

    def liveness(pid: object, _birth: object) -> str:
        return "dead" if pid == 41_001 else "unknown"

    monkeypatch.setattr(coordinator, "_process_liveness", liveness)

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    assert status["local_liveness"] == "child-liveness-unknown"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list)
    assert isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "active-child-liveness-unknown"


def test_indeterminate_waiter_liveness_makes_status_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    queue = {
        "schema_version": 1,
        "next_ticket": 2,
        "items": [
            coordinator._new_queue_item(
                request_id="a" * 32,
                ticket=1,
                profile="ci-final",
                phase="waiting",
                owner=coordinator.LocalResourceOwner(
                    pid=os.getpid(),
                    process_birth=birth,
                ),
                source=coordinator.LocalResourceSource(worktree=str(tmp_path)),
                intent="test",
                handoff=None,
            )
        ],
    }
    with coordinator._state_mutex(state_dir):
        coordinator._write_queue_state(state_dir, queue)
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "unknown")

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    assert status["local_liveness"] == "queue-liveness-unknown"


def test_status_waiters_have_explicit_one_based_positions_in_bounded_prefix(
    tmp_path: Path,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    items = [
        _queue_item(
            tmp_path,
            request_id=f"{ticket:032x}",
            ticket=ticket,
        )
        for ticket in range(1, 26)
    ]
    with coordinator._state_mutex(state_dir):
        coordinator._write_queue_state(
            state_dir,
            {"schema_version": 1, "next_ticket": 26, "items": items},
        )

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert status["state"] == "waiting"
    assert status["queue_total"] == 25
    queue = status["queue"]
    assert isinstance(queue, list)
    assert len(queue) == coordinator._MAX_STATUS_QUEUE_ITEMS == 20
    assert [item["queue_position"] for item in queue] == list(range(1, 21))
    assert all(set(item) == QUEUE_KEYS for item in queue)


def test_held_lock_identity_uses_the_platform_authority_boundary(tmp_path: Path) -> None:
    state_dir = _private_state_dir(tmp_path)
    lock_path = tmp_path / "legacy-owner.lock"
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            _active_record(pid=os.getpid(), process_birth="wrong-birth"),
        )
    ownership = coordinator.RealRayOwnershipLock(lock_path)
    ownership.acquire(
        {
            "pid": os.getpid(),
            "hostname": "legacy-host",
            "rootpath": "worktree",
            "selected_count": 1,
        }
    )
    try:
        status = coordinator.read_local_resource_status(
            lock_path=lock_path,
            state_dir=state_dir,
        )
    finally:
        ownership.release()

    assert set(status) == STATUS_KEYS
    if os.name == "posix":
        assert status["state"] == "legacy-held"
        assert status["safe_action"] == "wait"
        assert status["local_liveness"] == "os-lock-held"
        active = status["active"]
        assert isinstance(active, dict)
        assert active["legacy"] is True
        assert active["owner"]["pid"] == os.getpid()
    else:
        assert status["state"] == "unknown"
        diagnostics = status["diagnostics"]
        assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
        assert diagnostics[0]["code"] == "active-lock-identity-conflict"


def test_legacy_lock_maps_only_real_contention_to_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = coordinator.RealRayOwnershipLock(tmp_path / "legacy-owner.lock")

    def fail_lock(_handle: object) -> None:
        raise OSError(errno.EIO, "injected I/O failure")

    monkeypatch.setattr(coordinator, "_try_advisory_lock", fail_lock)

    with pytest.raises(
        coordinator.LocalResourceStateError,
        match="could not be acquired safely",
    ) as raised:
        lock.acquire({"pid": os.getpid()})

    assert not isinstance(raised.value, coordinator.RealRayOwnershipUnavailableError)
    assert lock.acquired is False


def test_composite_release_settles_authority_after_compatibility_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandle:
        def __init__(self, name: str, *, fail_close_once: bool = False) -> None:
            self.name = name
            self.closed = False
            self.close_calls = 0
            self.fail_close_once = fail_close_once

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_close_once:
                self.fail_close_once = False
                raise RuntimeError(f"{self.name} close failed")
            self.closed = True

    authority = FakeHandle("authority")
    compatibility = FakeHandle("compatibility", fail_close_once=True)
    unlocks: list[str] = []
    lock = coordinator.RealRayOwnershipLock(
        tmp_path / "authority.lock",
        compatibility_path=tmp_path / "legacy.lock",
    )
    lock._handle = cast(BinaryIO, authority)
    lock._compatibility_handle = cast(BinaryIO, compatibility)
    monkeypatch.setattr(
        coordinator,
        "_release_advisory_lock",
        lambda handle: unlocks.append(cast(FakeHandle, handle).name),
    )

    with pytest.raises(RuntimeError, match="compatibility close failed"):
        lock.release()

    assert unlocks == ["compatibility", "authority"]
    assert authority.closed is True
    assert lock._handle is None
    assert lock._compatibility_handle is compatibility
    assert lock.acquired is True

    lock.release()

    assert unlocks == ["compatibility", "authority"]
    assert compatibility.closed is True
    assert compatibility.close_calls == 2
    assert lock.acquired is False


def test_composite_release_does_not_retry_a_committed_close_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandle:
        def __init__(self, name: str, *, interrupt_after_close: bool = False) -> None:
            self.name = name
            self.closed = False
            self.close_calls = 0
            self.interrupt_after_close = interrupt_after_close

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True
            if self.interrupt_after_close:
                self.interrupt_after_close = False
                raise KeyboardInterrupt

    authority = FakeHandle("authority")
    compatibility = FakeHandle("compatibility", interrupt_after_close=True)
    unlocks: list[str] = []
    lock = coordinator.RealRayOwnershipLock(
        tmp_path / "authority.lock",
        compatibility_path=tmp_path / "legacy.lock",
    )
    lock._handle = cast(BinaryIO, authority)
    lock._compatibility_handle = cast(BinaryIO, compatibility)
    monkeypatch.setattr(
        coordinator,
        "_release_advisory_lock",
        lambda handle: unlocks.append(cast(FakeHandle, handle).name),
    )

    with pytest.raises(KeyboardInterrupt):
        lock.release()

    assert unlocks == ["compatibility", "authority"]
    assert compatibility.close_calls == 1
    assert authority.close_calls == 1
    assert lock.acquired is False
    lock.release()
    assert compatibility.close_calls == 1


def test_composite_release_defers_ctrl_c_until_both_handles_settle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandle:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False

        def close(self) -> None:
            self.closed = True

    authority = FakeHandle("authority")
    compatibility = FakeHandle("compatibility")
    lock = coordinator.RealRayOwnershipLock(
        tmp_path / "authority.lock",
        compatibility_path=tmp_path / "legacy.lock",
    )
    lock._handle = cast(BinaryIO, authority)
    lock._compatibility_handle = cast(BinaryIO, compatibility)
    unlocks: list[str] = []
    signal_injected = False

    def unlock(handle: BinaryIO) -> None:
        nonlocal signal_injected
        unlocks.append(cast(FakeHandle, handle).name)
        if not signal_injected:
            signal_injected = True
            _invoke_installed_sigint_handler()

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    monkeypatch.setattr(coordinator, "_release_advisory_lock", unlock)
    try:
        with pytest.raises(KeyboardInterrupt):
            lock.release()
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signal_injected is True
    assert unlocks == ["compatibility", "authority"]
    assert compatibility.closed is True
    assert authority.closed is True
    assert lock.acquired is False


def test_held_legacy_lock_reports_bounded_owner_and_wait_action(tmp_path: Path) -> None:
    lock_path = tmp_path / "legacy-owner.lock"
    ownership = coordinator.RealRayOwnershipLock(lock_path)
    ownership.acquire(
        {
            "pid": os.getpid(),
            "hostname": "legacy-host",
            "acquired_at": "2026-08-15T00:00:00+00:00",
            "rootpath": "legacy-worktree",
            "selected_count": 7,
        }
    )
    try:
        status = coordinator.read_local_resource_status(
            lock_path=lock_path,
            state_dir=_private_state_dir(tmp_path),
        )
    finally:
        ownership.release()

    assert status["state"] == "legacy-held"
    assert status["safe_action"] == "wait"
    assert status["local_liveness"] == "os-lock-held"
    active = status["active"]
    assert isinstance(active, dict)
    assert set(status) == STATUS_KEYS
    assert set(active) == ACTIVE_KEYS
    assert active == {
        "run_id": None,
        "profile": "real-ray",
        "resources": ["host-heavy"],
        "phase": "legacy-pytest",
        "queue_position": 0,
        "owner": {
            "owner": None,
            "session": None,
            "agent": None,
            "model": None,
            "host_id": "sha256:5bd960e40874e574",
            "pid": os.getpid(),
            "process_birth": None,
        },
        "source": {
            "worktree": "legacy-worktree",
            "branch": None,
            "commit": None,
            "source_tree": None,
            "dirty": None,
        },
        "intent": "legacy real_ray pytest ownership",
        "handoff": None,
        "acquired_at": "2026-08-15T00:00:00+00:00",
        "heartbeat_at": None,
        "expiry_at": None,
        "selected_count": 7,
        "child": None,
        "outcome": None,
        "postcondition": None,
        "liveness": "os-lock-held",
        "legacy": True,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX authority regression")
def test_status_uses_per_user_authority_with_or_without_an_active_record(
    tmp_path: Path,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    coordinator._ensure_private_state_dir(state_dir)
    legacy_path = tmp_path / "legacy-owner.lock"
    authority_path = coordinator._local_resource_authority_lock_path(
        state_dir=state_dir,
        legacy_lock_path=legacy_path,
    )
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    authority = coordinator.RealRayOwnershipLock(authority_path)
    authority.acquire({"pid": os.getpid(), "rootpath": str(tmp_path)})
    try:
        transitional = coordinator.read_local_resource_status(
            lock_path=legacy_path,
            state_dir=state_dir,
        )
        assert transitional["state"] == "legacy-held"
        assert transitional["safe_action"] == "wait"

        with coordinator._state_mutex(state_dir):
            coordinator._write_state_json(
                state_dir / coordinator.ACTIVE_STATE_FILE,
                _active_record(pid=os.getpid(), process_birth=birth),
            )
        active = coordinator.read_local_resource_status(
            lock_path=legacy_path,
            state_dir=state_dir,
        )
        assert active["state"] == "active"
        assert active["safe_action"] == "wait"

        mismatched_legacy = coordinator.RealRayOwnershipLock(legacy_path)
        mismatched_legacy.acquire({"pid": os.getpid() + 1, "rootpath": "older-worktree"})
        try:
            conflict = coordinator.read_local_resource_status(
                lock_path=legacy_path,
                state_dir=state_dir,
            )
        finally:
            mismatched_legacy.release()
        assert conflict["state"] == "unknown"
        assert conflict["safe_action"] == "investigate"
        diagnostics = conflict["diagnostics"]
        assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
        assert diagnostics[0]["code"] == "active-lock-identity-conflict"
    finally:
        authority.release()


@pytest.mark.skipif(os.name != "posix", reason="POSIX authority regression")
def test_status_never_substitutes_a_compatibility_lock_for_live_authority(tmp_path: Path) -> None:
    state_dir = _private_state_dir(tmp_path)
    legacy_path = tmp_path / "legacy-owner.lock"
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            _active_record(pid=os.getpid(), process_birth=birth),
        )
    legacy = coordinator.RealRayOwnershipLock(legacy_path)
    legacy.acquire({"pid": os.getpid(), "rootpath": str(tmp_path)})
    try:
        status = coordinator.read_local_resource_status(
            lock_path=legacy_path,
            state_dir=state_dir,
        )
    finally:
        legacy.release()

    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "active-owner-without-lock"


@pytest.mark.skipif(os.name != "posix", reason="POSIX orphan authority regression")
def test_held_compatibility_never_masks_a_proved_live_orphaned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    legacy_path = tmp_path / "legacy-owner.lock"
    active_record = _active_record(pid=41_001, process_birth="departed-root")
    active_record["child"] = {
        "pid": 41_002,
        "process_birth": "live-child",
        "tree_kind": "posix-process-group",
    }
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            active_record,
        )
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "dead")
    monkeypatch.setattr(
        coordinator,
        "_child_record_liveness",
        lambda _child, **_kwargs: "live",
    )
    legacy = coordinator.RealRayOwnershipLock(legacy_path)
    legacy.acquire({"pid": os.getpid(), "rootpath": str(tmp_path)})
    try:
        status = coordinator.read_local_resource_status(
            lock_path=legacy_path,
            state_dir=state_dir,
        )
    finally:
        legacy.release()

    assert status["state"] == "orphaned"
    assert status["safe_action"] == "investigate"
    assert status["local_liveness"] == "orphaned-child-live"
    orphaned = status["orphaned"]
    assert isinstance(orphaned, list) and len(orphaned) == 1
    orphan = orphaned[0]
    assert isinstance(orphan, dict)
    assert orphan["legacy"] is False
    child = orphan["child"]
    assert isinstance(child, dict)
    assert child["liveness"] == "live"


@pytest.mark.skipif(os.name != "posix", reason="POSIX foreign-user regression")
def test_foreign_inaccessible_tmp_legacy_and_old_state_do_not_block_user_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_parent = tmp_path / "safe-home"
    safe_parent.mkdir(mode=0o700)
    state_dir = safe_parent / "django-ray-local-resources"
    legacy_path = tmp_path / "django-ray-pytest-real-ray-owner.lock"
    legacy_path.write_bytes(b"foreign user compatibility inode")
    old_state_dir = tmp_path / f"django-ray-local-resources-{os.geteuid()}"
    old_state_dir.mkdir(mode=0o700)
    old_marker = old_state_dir / "foreign-squat"
    old_marker.write_text("untouched", encoding="ascii")
    monkeypatch.setattr(coordinator, "DEFAULT_LOCAL_RESOURCE_STATE_DIR", state_dir)
    monkeypatch.setattr(coordinator, "DEFAULT_REAL_RAY_LOCK_PATH", legacy_path)

    original_stat = coordinator.os.stat
    legacy_stat = original_stat(legacy_path, follow_symlinks=False)

    class _ForeignOwnerStat:
        st_uid = os.geteuid() + 1

        def __getattr__(self, name: str) -> object:
            return getattr(legacy_stat, name)

    def report_foreign_owner(path: object, *args: object, **kwargs: object) -> object:
        try:
            candidate = Path(cast(str | os.PathLike[str], path))
        except TypeError:
            return original_stat(path, *args, **kwargs)
        if candidate == legacy_path:
            return _ForeignOwnerStat()
        return original_stat(path, *args, **kwargs)

    legacy_opens = 0
    original_open = coordinator._open_lock_descriptor

    def deny_foreign_open(path: Path, *, create: bool = True) -> int:
        nonlocal legacy_opens
        if path == legacy_path:
            legacy_opens += 1
            raise PermissionError(errno.EACCES, "foreign 0600 inode", str(path))
        return original_open(path, create=create)

    monkeypatch.setattr(coordinator.os, "stat", report_foreign_owner)
    monkeypatch.setattr(coordinator, "_open_lock_descriptor", deny_foreign_open)

    initial = coordinator.read_local_resource_status(
        lock_path=legacy_path,
        state_dir=state_dir,
    )
    assert initial["state"] == "idle"
    assert initial["safe_action"] == "proceed"
    assert initial["diagnostics"] == [
        {
            "code": "foreign-legacy-lock-ignored",
            "message": (
                "the fixed legacy lock belongs to another OS user and is outside this "
                "coordination boundary"
            ),
        }
    ]
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="foreign-legacy",
        rootpath=tmp_path,
        timeout_seconds=0,
    )
    try:
        active = coordinator.read_local_resource_status(
            lock_path=legacy_path,
            state_dir=state_dir,
        )
        assert active["state"] == "active"
        assert active["safe_action"] == "wait"
        assert lease._ownership_lock is not None
        assert lease._ownership_lock.path == state_dir / coordinator.AUTHORITY_LOCK_FILE
        assert lease._ownership_lock._compatibility_handle is None
    finally:
        lease.release(outcome="passed", postcondition="foreign legacy ignored")

    assert (
        coordinator.read_local_resource_status(
            lock_path=legacy_path,
            state_dir=state_dir,
        )["state"]
        == "idle"
    )
    assert legacy_opens == 0
    assert old_marker.read_text(encoding="ascii") == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX legacy compatibility regression")
def test_same_user_legacy_client_and_per_user_authority_remain_mutually_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, legacy_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    ready_path = tmp_path / "legacy-ready"
    release_path = tmp_path / "release-legacy"
    holder_code = (
        "import os,sys,time; from pathlib import Path; "
        "from scripts.local_resource_coordinator import RealRayOwnershipLock; "
        "lock=RealRayOwnershipLock(Path(sys.argv[1])); "
        "lock.acquire({'pid':os.getpid(),'rootpath':'older-worktree'}); "
        "Path(sys.argv[2]).write_text('ready'); "
        "deadline=time.monotonic()+15; "
        'exec("while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:\\n'
        '    time.sleep(0.02)"); lock.release()'
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_code,
            str(legacy_path),
            str(ready_path),
            str(release_path),
        ],
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready_path.exists():
            stdout, stderr = holder.communicate(timeout=5)
            raise AssertionError(f"legacy holder failed: stdout={stdout!r}, stderr={stderr!r}")

        with pytest.raises(coordinator.LocalResourceBusyError):
            coordinator.acquire_local_resources(
                profile="ci-final",
                phase="new-client",
                rootpath=tmp_path,
                timeout_seconds=0,
            )
        authority_path = coordinator._local_resource_authority_lock_path(
            state_dir=state_dir,
            legacy_lock_path=legacy_path,
        )
        assert coordinator._probe_legacy_lock(authority_path)[0] == "free"

        release_path.write_text("release", encoding="ascii")
        holder.wait(timeout=10)
        assert holder.returncode == 0

        lease = coordinator.acquire_local_resources(
            profile="ci-final",
            phase="new-client",
            rootpath=tmp_path,
            timeout_seconds=1,
        )
        try:
            ownership = lease._ownership_lock
            assert ownership is not None
            assert ownership._compatibility_handle is not None
            contender_code = (
                "import os,sys; from pathlib import Path; "
                "from scripts.local_resource_coordinator import ("
                "RealRayOwnershipLock,RealRayOwnershipUnavailableError); "
                "lock=RealRayOwnershipLock(Path(sys.argv[1])); "
                'exec("try:\\n'
                "    lock.acquire({'pid':os.getpid()})\\n"
                "except RealRayOwnershipUnavailableError:\\n"
                "    raise SystemExit(0)\\n"
                "else:\\n"
                "    lock.release()\\n"
                '    raise SystemExit(1)")'
            )
            contender = subprocess.run(
                [sys.executable, "-c", contender_code, str(legacy_path)],
                cwd=Path(__file__).parents[2],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert contender.returncode == 0, contender.stderr
        finally:
            lease.release(outcome="passed", postcondition="same-user compatibility retained")
    finally:
        if holder.poll() is None:
            release_path.write_text("release", encoding="ascii")
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)


def test_unsafe_state_directory_fails_closed_without_touching_legacy_path(
    tmp_path: Path,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    state_dir.write_text("not a directory", encoding="utf-8")
    lock_path = tmp_path / "legacy-owner.lock"

    status = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )

    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    assert status["local_liveness"] == "state-path-unsafe"
    assert status["diagnostics"] == [
        {
            "code": "state-path-unsafe",
            "message": f"local resource coordination requires a private, stable state directory; refusing unsafe state path {state_dir}",
        }
    ]
    assert lock_path.exists() is False
    assert set(status) == STATUS_KEYS


def test_status_renderers_are_deterministic_and_bounded(tmp_path: Path) -> None:
    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=_private_state_dir(tmp_path),
    )

    rendered_json = coordinator.render_local_resource_status(status, output_format="json")
    assert json.loads(rendered_json) == status
    assert (
        rendered_json
        == json.dumps(
            status,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    assert coordinator.render_local_resource_status(status) == (
        "Local resources: idle\n"
        "Safe action: proceed\n"
        "Termination authority: none\n"
        "Kubernetes mirror: not-configured\n"
    )

    with pytest.raises(ValueError, match="must be 'text' or 'json'"):
        coordinator.render_local_resource_status(status, output_format="yaml")  # type: ignore[arg-type]


def test_human_status_names_active_queue_and_last_completion_owners() -> None:
    owner = {
        "owner": "alice",
        "session": "thread-417",
        "agent": "gate-agent",
        "model": "gpt-test",
        "host_id": "sha256:host",
        "pid": 417,
        "process_birth": "birth-417",
    }
    source = {
        "worktree": "D:/worktree",
        "branch": "feat/issue-417",
        "commit": "c" * 40,
        "source_tree": "d" * 40,
        "dirty": True,
    }
    identity = {
        "owner": owner,
        "source": source,
        "intent": "final validation",
        "handoff": "task-417",
    }
    status = coordinator._empty_status(
        state="active",
        safe_action="wait",
        local_liveness="os-lock-held",
    )
    status["active"] = {
        **identity,
        "profile": "ci-final",
        "resources": ["host-heavy"],
        "phase": "tests",
        "queue_position": 0,
        "child": {
            "pid": 418,
            "process_birth": "birth-418",
            "tree_kind": "windows-job",
            "liveness": "live",
        },
    }
    status["queue"] = [
        {
            **identity,
            "ticket": 9,
            "queue_position": 1,
            "profile": "kuberay-final",
            "phase": "images",
            "liveness": "live",
        }
    ]
    status["queue_total"] = 2
    status["last_completed"] = {
        **identity,
        "profile": "real-ray",
        "outcome": "passed",
        "completed_at": "2026-08-15T00:00:00+00:00",
        "postcondition": "owned tree absent",
    }

    rendered = coordinator.render_local_resource_status(status)

    assert coordinator.render_local_resource_status(status) == rendered
    lines = rendered.splitlines()
    assert lines[:8] == [
        "Local resources: active",
        "Safe action: wait",
        "Termination authority: none",
        "Profile: ci-final",
        "Resources: host-heavy",
        "Phase: tests",
        "Queue position: 0 (active)",
        "Child: pid=418, birth=birth-418, tree=windows-job, liveness=live",
    ]
    assert "Active owner: alice" in lines
    assert "Active session: thread-417" in lines
    assert "Active agent: gate-agent" in lines
    assert "Active model: gpt-test" in lines
    assert "Active intent: final validation" in lines
    assert "Active handoff: task-417" in lines
    assert "Queue position 1: ticket=9, profile=kuberay-final, phase=images, liveness=live" in lines
    assert "Queue 1 owner: alice" in lines
    assert "Queue 1 session: thread-417" in lines
    assert "Queue entries omitted: 1" in lines
    assert (
        "Last completion: profile=real-ray, outcome=passed, "
        "completed_at=2026-08-15T00:00:00+00:00" in lines
    )
    assert "Last postcondition: owned tree absent" in lines
    assert "Last owner: alice" in lines
    assert "Last source dirty: true" in lines
    assert lines[-1] == "Kubernetes mirror: not-configured"


def test_human_status_renders_bounded_orphan_investigation_context() -> None:
    status = coordinator._empty_status(
        state="orphaned",
        safe_action="investigate",
        local_liveness="orphaned-child-live",
    )
    status["orphaned"] = [
        {
            "profile": "kuberay-final",
            "resources": ["host-heavy"],
            "phase": "workload",
            "liveness": "orphaned-child-live",
            "owner": {
                "owner": "alice",
                "session": "thread-orphan",
                "agent": "gate-agent",
                "model": "gpt-test",
                "host_id": "sha256:host",
                "pid": 501,
                "process_birth": "birth-501",
            },
            "source": {
                "worktree": "D:/orphan-worktree",
                "branch": "feat/orphan",
                "commit": "e" * 40,
                "source_tree": "f" * 40,
                "dirty": False,
            },
            "intent": "final KubeRay validation",
            "handoff": "investigate-task",
            "child": {
                "pid": 502,
                "process_birth": "birth-502",
                "tree_kind": "windows-job",
                "liveness": "live",
            },
        }
    ]

    rendered = coordinator.render_local_resource_status(status)

    assert coordinator.render_local_resource_status(status) == rendered
    assert rendered.splitlines() == [
        "Local resources: orphaned",
        "Safe action: investigate",
        "Termination authority: none",
        "Orphan 1: profile=kuberay-final, resources=host-heavy, "
        "phase=workload, liveness=orphaned-child-live",
        "Orphan 1 child: pid=502, birth=birth-502, tree=windows-job, liveness=live",
        "Orphan 1 owner: alice",
        "Orphan 1 session: thread-orphan",
        "Orphan 1 agent: gate-agent",
        "Orphan 1 model: gpt-test",
        "Orphan 1 host: sha256:host",
        "Orphan 1 PID: 501",
        "Orphan 1 process birth: birth-501",
        "Orphan 1 intent: final KubeRay validation",
        "Orphan 1 handoff: investigate-task",
        "Orphan 1 worktree: D:/orphan-worktree",
        "Orphan 1 branch: feat/orphan",
        f"Orphan 1 commit: {'e' * 40}",
        f"Orphan 1 source tree: {'f' * 40}",
        "Orphan 1 source dirty: false",
        "Kubernetes mirror: not-configured",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"items":[],"items":[],"next_ticket":1,"schema_version":1}',
        b'{"items":[],"next_ticket":NaN,"schema_version":1}',
        b'{"items":[],"next_ticket":999999999999999999999,"schema_version":1}',
        b'{ "items": [], "next_ticket": 1, "schema_version": 1 }',
    ],
)
def test_status_maps_noncanonical_registry_json_to_schema_valid_unknown(
    tmp_path: Path,
    payload: bytes,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    with coordinator._state_mutex(state_dir):
        queue_path = state_dir / coordinator.QUEUE_STATE_FILE
        queue_path.write_bytes(payload)
        original_mtime = queue_path.stat().st_mtime_ns

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "state-registry-corrupt"
    assert queue_path.read_bytes() == payload
    assert queue_path.stat().st_mtime_ns == original_mtime


def test_status_maps_deeply_nested_registry_json_to_schema_valid_unknown(
    tmp_path: Path,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    payload = (b"[" * 20_000) + b"0" + (b"]" * 20_000)
    with coordinator._state_mutex(state_dir):
        queue_path = state_dir / coordinator.QUEUE_STATE_FILE
        queue_path.write_bytes(payload)

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "state-registry-corrupt"
    assert queue_path.read_bytes() == payload


@pytest.mark.parametrize("record_kind", ["queue", "active-owner", "active-child"])
@pytest.mark.parametrize(
    "invalid_birth",
    ["", "birth\nforged", "birth-\N{SNOWMAN}", "x" * 1_000],
)
def test_authority_process_birth_is_exact_or_status_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_kind: str,
    invalid_birth: str,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    current_birth = coordinator._process_birth(os.getpid())
    assert current_birth is not None
    with coordinator._state_mutex(state_dir):
        if record_kind == "queue":
            item = _queue_item(tmp_path)
            assert isinstance(item["owner"], dict)
            item["owner"]["process_birth"] = invalid_birth
            coordinator._write_queue_state(
                state_dir,
                {"schema_version": 1, "next_ticket": 2, "items": [item]},
            )
        else:
            active = _active_record(pid=os.getpid(), process_birth=current_birth)
            if record_kind == "active-owner":
                assert isinstance(active["owner"], dict)
                active["owner"]["process_birth"] = invalid_birth
            else:
                active["child"] = {
                    "pid": os.getpid(),
                    "process_birth": invalid_birth,
                    "tree_kind": ("windows-job" if os.name == "nt" else "posix-process-group"),
                }
            coordinator._write_state_json(
                state_dir / coordinator.ACTIVE_STATE_FILE,
                active,
            )

    def liveness_must_not_run(_pid: object, _birth: object) -> str:
        pytest.fail("malformed authority identity reached liveness classification")

    monkeypatch.setattr(coordinator, "_process_liveness", liveness_must_not_run)
    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "state-registry-corrupt"


@pytest.mark.parametrize(
    "corruption",
    ["duplicate-request", "duplicate-ticket", "nonmonotonic", "out-of-range"],
)
def test_status_rejects_corrupt_queue_sequence(
    tmp_path: Path,
    corruption: str,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    first = _queue_item(tmp_path, request_id="a" * 32, ticket=1)
    second = _queue_item(tmp_path, request_id="b" * 32, ticket=2)
    next_ticket = 3
    if corruption == "duplicate-request":
        second["request_id"] = first["request_id"]
    elif corruption == "duplicate-ticket":
        second["ticket"] = first["ticket"]
    elif corruption == "nonmonotonic":
        first["ticket"], second["ticket"] = 2, 1
    else:
        first["ticket"] = coordinator._MAX_TICKET + 1
        next_ticket = coordinator._MAX_TICKET + 1
    items = [first] if corruption == "out-of-range" else [first, second]
    with coordinator._state_mutex(state_dir):
        coordinator._write_queue_state(
            state_dir,
            {"schema_version": 1, "next_ticket": next_ticket, "items": items},
        )

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    assert status["safe_action"] == "investigate"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "state-registry-corrupt"


@pytest.mark.parametrize("selected_count", [-1, coordinator._MAX_SELECTED_COUNT + 1])
def test_status_rejects_out_of_range_active_selected_count(
    tmp_path: Path,
    selected_count: int,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    current_birth = coordinator._process_birth(os.getpid())
    assert current_birth is not None
    active = _active_record(pid=os.getpid(), process_birth=current_birth)
    active["selected_count"] = selected_count
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(state_dir / coordinator.ACTIVE_STATE_FILE, active)

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "state-registry-corrupt"


@pytest.mark.parametrize("tree_kind", [None, "direct", "wrong-platform-tree"])
def test_status_requires_platform_exact_child_tree_custody(
    tmp_path: Path,
    tree_kind: object,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    current_birth = coordinator._process_birth(os.getpid())
    assert current_birth is not None
    active = _active_record(pid=os.getpid(), process_birth=current_birth)
    active["child"] = {
        "pid": os.getpid(),
        "process_birth": current_birth,
        "tree_kind": tree_kind,
    }
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(state_dir / coordinator.ACTIVE_STATE_FILE, active)

    status = coordinator.read_local_resource_status(
        lock_path=tmp_path / "legacy-owner.lock",
        state_dir=state_dir,
    )

    assert set(status) == STATUS_KEYS
    assert status["state"] == "unknown"
    diagnostics = status["diagnostics"]
    assert isinstance(diagnostics, list) and isinstance(diagnostics[0], dict)
    assert diagnostics[0]["code"] == "state-registry-corrupt"


def test_state_removal_never_deletes_an_injected_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _private_state_dir(tmp_path)
    coordinator._ensure_private_state_dir(state_dir)
    state_file = state_dir / coordinator.ACTIVE_STATE_FILE
    original = b'{"original":true}'
    replacement = b'{"replacement":true}'
    state_file.write_bytes(original)
    displaced = state_dir / "displaced.json"

    if os.name == "nt":
        open_for_delete = coordinator._open_windows_state_delete_handle

        def inject_windows_replacement(path: Path) -> int:
            handle = open_for_delete(path)
            path.replace(displaced)
            path.write_bytes(replacement)
            return handle

        monkeypatch.setattr(
            coordinator,
            "_open_windows_state_delete_handle",
            inject_windows_replacement,
        )
        coordinator._remove_state_file(state_file)
    else:
        open_descriptor = coordinator._open_existing_state_descriptor

        def inject_posix_replacement(path: Path) -> int:
            descriptor = open_descriptor(path)
            path.replace(displaced)
            path.write_bytes(replacement)
            return descriptor

        monkeypatch.setattr(
            coordinator,
            "_open_existing_state_descriptor",
            inject_posix_replacement,
        )
        with pytest.raises(coordinator.LocalResourceStateError, match="changed before deletion"):
            coordinator._remove_state_file(state_file)

    assert state_file.read_bytes() == replacement


def test_state_read_open_defers_sigint_until_fd_has_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / coordinator.ACTIVE_STATE_FILE
    state_file.write_bytes(b'{"value":true}')
    open_descriptor = coordinator._open_existing_state_descriptor
    close_descriptor = os.close
    opened_descriptor: int | None = None
    close_calls = 0

    def open_then_interrupt(path: Path) -> int:
        nonlocal opened_descriptor
        opened_descriptor = open_descriptor(path)
        signal.raise_signal(signal.SIGINT)
        return opened_descriptor

    def observe_close(descriptor: int) -> None:
        nonlocal close_calls
        if descriptor == opened_descriptor:
            close_calls += 1
        close_descriptor(descriptor)

    monkeypatch.setattr(
        coordinator,
        "_open_existing_state_descriptor",
        open_then_interrupt,
    )
    monkeypatch.setattr(os, "close", observe_close)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator._read_state_json(state_file)
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert close_calls == 1
    assert opened_descriptor is not None
    with pytest.raises(OSError) as raised:
        os.fstat(opened_descriptor)
    assert raised.value.errno == errno.EBADF


def test_state_read_does_not_map_post_open_file_not_found_to_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / coordinator.ACTIVE_STATE_FILE
    state_file.write_bytes(b'{"value":true}')
    open_descriptor = coordinator._open_existing_state_descriptor
    close_descriptor = os.close
    opened_descriptor: int | None = None
    close_calls = 0

    def open_then_signal(path: Path) -> int:
        nonlocal opened_descriptor
        opened_descriptor = open_descriptor(path)
        signal.raise_signal(signal.SIGINT)
        return opened_descriptor

    def late_missing_handler(_signum: int, _frame: FrameType | None) -> None:
        raise FileNotFoundError("post-open injected absence")

    def observe_close(descriptor: int) -> None:
        nonlocal close_calls
        if descriptor == opened_descriptor:
            close_calls += 1
        close_descriptor(descriptor)

    monkeypatch.setattr(
        coordinator,
        "_open_existing_state_descriptor",
        open_then_signal,
    )
    monkeypatch.setattr(os, "close", observe_close)
    previous_handler = signal.signal(signal.SIGINT, late_missing_handler)
    try:
        with pytest.raises(FileNotFoundError, match="post-open injected absence"):
            coordinator._read_state_json(state_file)
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert close_calls == 1
    assert opened_descriptor is not None
    with pytest.raises(OSError) as raised:
        os.fstat(opened_descriptor)
    assert raised.value.errno == errno.EBADF


def test_state_read_fd_transfer_defers_sigint_until_file_object_owns_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / coordinator.ACTIVE_STATE_FILE
    state_file.write_bytes(b'{"value":true}')
    open_descriptor = coordinator._open_existing_state_descriptor
    fdopen = os.fdopen
    transferred_descriptor: int | None = None

    def record_open(path: Path) -> int:
        nonlocal transferred_descriptor
        transferred_descriptor = open_descriptor(path)
        return transferred_descriptor

    def transfer_then_interrupt(
        descriptor: int,
        mode: str,
        buffering: int,
    ) -> object:
        handle = fdopen(descriptor, mode, buffering)
        signal.raise_signal(signal.SIGINT)
        return handle

    monkeypatch.setattr(coordinator, "_open_existing_state_descriptor", record_open)
    monkeypatch.setattr(os, "fdopen", transfer_then_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator._read_state_json(state_file)
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert transferred_descriptor is not None
    with pytest.raises(OSError) as raised:
        os.fstat(transferred_descriptor)
    assert raised.value.errno == errno.EBADF


def test_state_temp_fd_close_is_consumed_before_committed_sigint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / coordinator.ACTIVE_STATE_FILE
    open_descriptor = os.open
    close_descriptor = os.close
    temporary_descriptor: int | None = None
    close_calls = 0

    def record_open(path: object, flags: int, mode: int = 0o777) -> int:
        nonlocal temporary_descriptor
        descriptor = open_descriptor(path, flags, mode)
        if Path(path) != state_file:
            temporary_descriptor = descriptor
        return descriptor

    def close_then_interrupt(descriptor: int) -> None:
        nonlocal close_calls
        if descriptor == temporary_descriptor:
            close_calls += 1
            close_descriptor(descriptor)
            signal.raise_signal(signal.SIGINT)
            return
        close_descriptor(descriptor)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", close_then_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator._write_state_json(state_file, {"value": True})
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert close_calls == 1
    assert temporary_descriptor is not None
    with pytest.raises(OSError) as raised:
        os.fstat(temporary_descriptor)
    assert raised.value.errno == errno.EBADF
    assert state_file.exists() is False
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("boundary", ["open", "close"])
def test_state_validation_fd_is_closed_once_across_sigint_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    state_file = tmp_path / coordinator.ACTIVE_STATE_FILE
    open_descriptor = coordinator._open_existing_state_descriptor
    close_descriptor = os.close
    validation_descriptor: int | None = None
    close_calls = 0
    injected = False

    def open_then_maybe_interrupt(path: Path) -> int:
        nonlocal injected, validation_descriptor
        validation_descriptor = open_descriptor(path)
        if boundary == "open":
            injected = True
            signal.raise_signal(signal.SIGINT)
        return validation_descriptor

    def close_then_maybe_interrupt(descriptor: int) -> None:
        nonlocal close_calls, injected
        if descriptor == validation_descriptor:
            close_calls += 1
            close_descriptor(descriptor)
            if boundary == "close" and not injected:
                injected = True
                signal.raise_signal(signal.SIGINT)
            return
        close_descriptor(descriptor)

    monkeypatch.setattr(
        coordinator,
        "_open_existing_state_descriptor",
        open_then_maybe_interrupt,
    )
    monkeypatch.setattr(os, "close", close_then_maybe_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator._write_state_json(state_file, {"value": True})
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert injected is True
    assert close_calls == 1
    assert validation_descriptor is not None
    with pytest.raises(OSError) as raised:
        os.fstat(validation_descriptor)
    assert raised.value.errno == errno.EBADF
    assert state_file.read_bytes() == b'{"value":true}'


def test_git_observation_scrubs_routing_and_caps_dirty_output_at_one_byte(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_DIR", "hostile")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "hostile")
    monkeypatch.setenv(coordinator.LOCAL_RESOURCE_CAPABILITY_ENV, "secret")
    monkeypatch.setenv("DJANGO_RAY_MARKER", "preserved")

    class BoundedOutput:
        def __init__(self) -> None:
            self.read_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return b"x" * size

        def close(self) -> None:
            return None

    class FakeGitProcess:
        def __init__(self) -> None:
            self.stdout = BoundedOutput()
            self.returncode: int | None = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    dirty_process = FakeGitProcess()

    def popen_git(args: list[str], **kwargs: object) -> object:
        assert args[:4] == ["git", "-c", "core.fsmonitor=false", "status"]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "GIT_DIR" not in environment
        assert coordinator.LOCAL_RESOURCE_CAPABILITY_ENV not in environment
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["close_fds"] is True
        return dirty_process

    def run_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "GIT_DIR" not in environment
        assert "GIT_CONFIG_COUNT" not in environment
        assert "GIT_CONFIG_KEY_0" not in environment
        assert "GIT_CONFIG_VALUE_0" not in environment
        assert coordinator.LOCAL_RESOURCE_CAPABILITY_ENV not in environment
        assert environment["DJANGO_RAY_MARKER"] == "preserved"
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["stderr"] is subprocess.DEVNULL
        output = kwargs["stdout"]
        output.write(b"x" * 1_000_000)  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(coordinator.subprocess, "run", run_git)
    monkeypatch.setattr(coordinator.subprocess, "Popen", popen_git)

    assert coordinator._git_dirty(tmp_path) is True
    assert dirty_process.terminated is True
    assert dirty_process.stdout.read_sizes == [coordinator._GIT_DIRTY_OUTPUT_CAP_BYTES] == [1]
    assert coordinator._git_value(tmp_path, "branch", "--show-current") is None


def test_root_acquisition_phase_update_and_release_are_coherent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)

    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="preflight",
        rootpath=tmp_path,
        selected_count=3,
        timeout_seconds=1,
    )
    capability = lease.inheritance_environment()[coordinator.LOCAL_RESOURCE_CAPABILITY_ENV]
    try:
        assert lease.inherited is False
        assert lease.profile == "ci-final"
        assert lease.termination_authority == "none"
        assert len(lease.run_id) == 32
        assert capability not in (state_dir / coordinator.ACTIVE_STATE_FILE).read_text(
            encoding="ascii"
        )
        status = coordinator.read_local_resource_status(
            lock_path=lock_path,
            state_dir=state_dir,
        )
        assert status["state"] == "active"
        assert status["termination_authority"] == "none"
        assert isinstance(status["active"], dict)
        assert status["active"]["phase"] == "preflight"

        lease.update_phase("tests")
        updated = coordinator.read_local_resource_status(
            lock_path=lock_path,
            state_dir=state_dir,
        )
        assert isinstance(updated["active"], dict)
        assert updated["active"]["phase"] == "tests"
    finally:
        lease.release(outcome="passed", postcondition="no owned child")
        lease.release(outcome="passed", postcondition="no owned child")

    assert lease.termination_authority == "none"
    completed = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )
    assert completed["state"] == "idle"
    assert completed["active"] is None
    assert isinstance(completed["last_completed"], dict)
    assert completed["last_completed"]["outcome"] == "passed"


def test_active_source_is_recaptured_once_at_fifo_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    queued_source = coordinator.LocalResourceSource(
        worktree=str(tmp_path),
        branch="queued-branch",
        commit="a" * 40,
        source_tree="b" * 40,
        dirty=False,
    )
    granted_source = coordinator.LocalResourceSource(
        worktree=str(tmp_path),
        branch="grant-branch",
        commit="c" * 40,
        source_tree="d" * 40,
        dirty=True,
    )
    observations = iter((queued_source, granted_source))
    calls = 0

    def observe_source(_rootpath: Path) -> coordinator.LocalResourceSource:
        nonlocal calls
        calls += 1
        return next(observations)

    monkeypatch.setattr(coordinator, "_source_identity", observe_source)

    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="grant",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    try:
        active = coordinator._read_active_record(state_dir)
        assert active is not None
        assert active["source"] == granted_source.as_dict()
        assert calls == 2
    finally:
        lease.release(outcome="passed", postcondition="grant source captured")


def test_fifo_grant_source_capture_does_not_hold_the_state_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    source_started = threading.Event()
    release_source = threading.Event()
    calls = 0
    leases: list[coordinator.LocalResourceLease] = []
    errors: list[BaseException] = []

    def slow_grant_source(rootpath: Path) -> coordinator.LocalResourceSource:
        nonlocal calls
        calls += 1
        if calls == 2:
            source_started.set()
            if not release_source.wait(timeout=5):
                raise AssertionError("test did not release grant source capture")
        return coordinator.LocalResourceSource(
            worktree=str(rootpath),
            branch=f"observation-{calls}",
            commit=str(calls) * 40,
            source_tree=str(calls + 1) * 40,
            dirty=False,
        )

    def acquire() -> None:
        try:
            leases.append(
                coordinator.acquire_local_resources(
                    profile="ci-final",
                    phase="slow-source",
                    rootpath=tmp_path,
                    timeout_seconds=2,
                )
            )
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(coordinator, "_source_identity", slow_grant_source)
    worker = threading.Thread(target=acquire, daemon=True)
    worker.start()
    assert source_started.wait(timeout=5)
    try:
        with coordinator._state_mutex(state_dir, timeout_seconds=0.5):
            queue = coordinator._read_queue_state(state_dir)
            assert len(queue["items"]) == 1
    finally:
        release_source.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    assert errors == []
    assert calls == 2
    assert len(leases) == 1
    leases[0].release(outcome="passed", postcondition="short mutex proved")


def test_fifo_grant_fails_closed_when_queued_source_changes_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    source_started = threading.Event()
    release_source = threading.Event()
    calls = 0
    errors: list[BaseException] = []

    def slow_grant_source(rootpath: Path) -> coordinator.LocalResourceSource:
        nonlocal calls
        calls += 1
        if calls == 2:
            source_started.set()
            if not release_source.wait(timeout=5):
                raise AssertionError("test did not release grant source capture")
        return coordinator.LocalResourceSource(worktree=str(rootpath), dirty=False)

    def acquire() -> None:
        try:
            coordinator.acquire_local_resources(
                profile="ci-final",
                phase="source-race",
                rootpath=tmp_path,
                timeout_seconds=2,
            )
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(coordinator, "_source_identity", slow_grant_source)
    worker = threading.Thread(target=acquire, daemon=True)
    worker.start()
    assert source_started.wait(timeout=5)
    try:
        with coordinator._state_mutex(state_dir):
            queue = coordinator._read_queue_state(state_dir)
            items = queue["items"]
            assert isinstance(items, list) and len(items) == 1
            item = items[0]
            assert isinstance(item, dict) and isinstance(item["source"], dict)
            item["source"]["branch"] = "concurrent-source-mutation"
            coordinator._write_queue_state(state_dir, queue)
    finally:
        release_source.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], coordinator.LocalResourceStateError)
    assert "queue head changed during grant source capture" in str(errors[0])
    assert coordinator._probe_legacy_lock(lock_path)[0] == "free"
    assert coordinator._read_active_record(state_dir) is None
    assert coordinator._read_queue_state(state_dir)["items"] == []


@pytest.mark.parametrize("failure_point", ["before", "after"])
def test_release_transition_never_publishes_the_same_run_as_active_and_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="release",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    transition = coordinator._transition_active_to_last_completed
    calls = 0

    def fail_once(
        transition_state_dir: Path,
        active: dict[str, object],
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_point == "after":
                transition(transition_state_dir, active)
            raise coordinator.LocalResourceStateError("injected completion transition failure")
        transition(transition_state_dir, active)

    monkeypatch.setattr(coordinator, "_transition_active_to_last_completed", fail_once)

    with pytest.raises(coordinator.LocalResourceStateError, match="injected"):
        lease.release(outcome="passed", postcondition="owned tree absent")

    interrupted = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )
    active = interrupted.get("active")
    completed = interrupted.get("last_completed")
    active_run = active.get("run_id") if isinstance(active, dict) else None
    completed_run = completed.get("run_id") if isinstance(completed, dict) else None
    assert not (active_run == lease.run_id and completed_run == lease.run_id)
    if failure_point == "before":
        assert active_run == lease.run_id
        assert completed is None
    else:
        assert active_run is None
        assert completed_run == lease.run_id

    lease.release(outcome="failed", postcondition="must preserve first transition")
    final = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert final["state"] == "idle"
    assert final["active"] is None
    assert isinstance(final["last_completed"], dict)
    assert final["last_completed"]["run_id"] == lease.run_id
    assert final["last_completed"]["outcome"] == "passed"
    assert final["last_completed"]["postcondition"] == "owned tree absent"
    expected_calls = 2 if failure_point == "before" else 1
    assert calls == expected_calls


def test_interrupted_partial_os_release_stays_interrupted_after_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="release-interruption",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    ownership = lease._ownership_lock
    assert ownership is not None
    release = ownership.release
    calls = 0

    def interrupt_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        release()

    monkeypatch.setattr(ownership, "release", interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        lease.release(outcome="passed", postcondition="owned tree absent")

    assert lease._released is False
    assert ownership.acquired is True
    partial = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )
    assert partial["state"] == "legacy-held"
    assert isinstance(partial["last_completed"], dict)
    assert partial["last_completed"]["outcome"] == "interrupted"
    assert partial["last_completed"]["postcondition"] == (
        "OS lane release interrupted before settlement"
    )

    lease.release(outcome="passed", postcondition="must not replace interruption")

    final = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )
    assert final["state"] == "idle"
    assert isinstance(final["last_completed"], dict)
    assert final["last_completed"]["outcome"] == "interrupted"
    assert final["last_completed"]["postcondition"] == (
        "OS lane release interrupted before settlement"
    )
    assert calls == 2


def test_partial_inheritance_fails_before_any_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(coordinator.LOCAL_RESOURCE_RUN_ID_ENV, "a" * 32)

    with pytest.raises(
        coordinator.LocalResourceInheritanceError,
        match="requires all four capability variables",
    ):
        coordinator.acquire_local_resources(
            profile="not-a-profile",
            phase="invalid\nphase",
            rootpath=tmp_path / "missing-root",
            timeout_seconds=0,
        )

    assert state_dir.exists() is False
    assert lock_path.exists() is False


def test_invalid_run_command_fails_before_any_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)

    with pytest.raises(coordinator.LocalResourceStateError, match="command is empty or invalid"):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="tests",
            rootpath=tmp_path,
            timeout_seconds=1,
            command=[],
        )

    assert state_dir.exists() is False
    assert lock_path.exists() is False


def test_complete_capability_can_borrow_a_resource_subset_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    root = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="tests",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    active_path = state_dir / coordinator.ACTIVE_STATE_FILE
    try:
        for key, value in root.inheritance_environment().items():
            monkeypatch.setenv(key, value)
        with coordinator._state_mutex(state_dir):
            active = coordinator._read_active_record(state_dir)
            assert active is not None
            source = active["source"]
            assert isinstance(source, dict)
            source["worktree"] = "bounded-display-path..."
            coordinator._write_state_json(active_path, active)
        before = active_path.read_bytes(), active_path.stat().st_mtime_ns

        borrowed = coordinator.acquire_local_resources(
            profile="real-ray",
            phase="pytest",
            rootpath=tmp_path,
            timeout_seconds=0,
        )

        assert borrowed.inherited is True
        assert borrowed.run_id == root.run_id
        assert borrowed.profile == "real-ray"
        assert borrowed.termination_authority == "none"
        borrowed.update_phase("ignored-by-borrower")
        borrowed.release()
        assert (active_path.read_bytes(), active_path.stat().st_mtime_ns) == before
    finally:
        for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        root.release(outcome="passed", postcondition="borrow validated")


@pytest.mark.skipif(os.name != "posix", reason="POSIX authority regression")
def test_inheritance_requires_per_user_authority_not_the_legacy_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, legacy_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    root = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="root",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    inheritance = root.inheritance_environment()
    with coordinator._state_mutex(state_dir):
        active = coordinator._read_active_record(state_dir)
        assert active is not None
    root.release(outcome="passed", postcondition="prepare authority-negative test")
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            active,
        )

    legacy = coordinator.RealRayOwnershipLock(legacy_path)
    legacy.acquire({"pid": os.getpid(), "rootpath": str(tmp_path)})
    try:
        for key, value in inheritance.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(
            coordinator.LocalResourceInheritanceError,
            match="authority is not actively held",
        ):
            coordinator.require_inherited_local_resources(
                profile="real-ray",
                rootpath=tmp_path,
            )
    finally:
        legacy.release()
        for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        with coordinator._state_mutex(state_dir):
            coordinator._remove_state_file(state_dir / coordinator.ACTIVE_STATE_FILE)


def test_same_process_inheritance_preserves_unicode_state_path_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    unicode_state_dir = base_state_dir.with_name(
        "state-\N{SNOWMAN}-caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    )
    monkeypatch.setattr(
        coordinator,
        "DEFAULT_LOCAL_RESOURCE_STATE_DIR",
        unicode_state_dir,
    )
    root = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="unicode",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    try:
        for key, value in root.inheritance_environment().items():
            monkeypatch.setenv(key, value)
        borrowed = coordinator.require_inherited_local_resources(
            profile="real-ray",
            rootpath=tmp_path,
        )
        assert borrowed.inherited is True
        assert borrowed.run_id == root.run_id
        assert Path(os.environ[coordinator.LOCAL_RESOURCE_STATE_DIR_ENV]) == unicode_state_dir
        borrowed.release()
    finally:
        for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        root.release(outcome="passed", postcondition="unicode inheritance validated")


@pytest.mark.parametrize(
    "raw_path",
    ["state\nforged", "x" * (coordinator._MAX_AUTHORITY_PATH_CHARACTERS + 1)],
    ids=("control", "oversized"),
)
def test_inherited_state_path_rejects_controls_and_unbounded_text(
    raw_path: str,
) -> None:
    with pytest.raises(coordinator.LocalResourceInheritanceError, match="path is invalid"):
        coordinator._validated_authority_path_text(raw_path)


def test_wrong_inherited_token_fails_closed_without_reacquiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    root = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="tests",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    try:
        for key, value in root.inheritance_environment().items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv(coordinator.LOCAL_RESOURCE_CAPABILITY_ENV, "f" * 64)
        active_before = (state_dir / coordinator.ACTIVE_STATE_FILE).read_bytes()

        with pytest.raises(
            coordinator.LocalResourceInheritanceError,
            match="does not match the active owner",
        ):
            coordinator.acquire_local_resources(
                profile="ci-final",
                phase="tests",
                rootpath=tmp_path,
                timeout_seconds=0,
            )

        assert (state_dir / coordinator.ACTIVE_STATE_FILE).read_bytes() == active_before
    finally:
        for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        root.release(outcome="passed", postcondition="bad token rejected")


def test_bounded_timeout_removes_its_live_waiting_ticket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    legacy = coordinator.RealRayOwnershipLock(lock_path)
    legacy.acquire(
        {
            "pid": os.getpid(),
            "hostname": "legacy-owner",
            "rootpath": str(tmp_path),
            "selected_count": 1,
        }
    )
    try:
        with pytest.raises(
            coordinator.LocalResourceBusyError,
            match="remain busy after the bounded wait",
        ):
            coordinator.acquire_local_resources(
                profile="ci-final",
                phase="waiting",
                rootpath=tmp_path,
                timeout_seconds=0,
                progress_interval_seconds=0.01,
            )
        with coordinator._state_mutex(state_dir):
            queue_state = coordinator._read_queue_state(state_dir)
        assert queue_state["items"] == []
    finally:
        legacy.release()


def test_promotion_prunes_only_a_proved_stale_active_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    departed = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    departed_birth = coordinator._process_birth(departed.pid)
    assert departed_birth is not None
    departed.terminate()
    departed.wait(timeout=10)
    stale = _active_record(pid=departed.pid, process_birth=departed_birth)
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(state_dir / coordinator.ACTIVE_STATE_FILE, stale)

    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="replacement",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    try:
        assert lease.run_id != stale["run_id"]
        current = coordinator._read_active_record(state_dir)
        assert current is not None
        assert current["run_id"] == lease.run_id
    finally:
        lease.release(outcome="passed", postcondition="stale record replaced safely")


def test_proved_orphaned_child_blocks_acquisition_without_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    departed = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    departed_birth = coordinator._process_birth(departed.pid)
    assert departed_birth is not None
    departed.terminate()
    departed.wait(timeout=10)
    current_birth = coordinator._process_birth(os.getpid())
    assert current_birth is not None
    orphan = _active_record(pid=departed.pid, process_birth=departed_birth)
    orphan["child"] = {
        "pid": os.getpid(),
        "process_birth": current_birth,
        "tree_kind": "windows-job" if os.name == "nt" else "posix-process-group",
    }
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(state_dir / coordinator.ACTIVE_STATE_FILE, orphan)
    original = (state_dir / coordinator.ACTIVE_STATE_FILE).read_bytes()

    with pytest.raises(coordinator.LocalResourceStateError, match="orphaned"):
        coordinator.acquire_local_resources(
            profile="ci-final",
            phase="blocked",
            rootpath=tmp_path,
            timeout_seconds=0,
        )

    assert (state_dir / coordinator.ACTIVE_STATE_FILE).read_bytes() == original


def test_live_child_prevents_release_until_exact_absence_is_proved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="child",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    owned = _launch_owned_for_test(
        lease=lease,
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        rootpath=Path.cwd(),
    )
    try:
        with pytest.raises(coordinator.LocalResourceStateError, match="must be cleared"):
            lease.release()
        assert lease.termination_authority == "owned-child-tree"
    finally:
        coordinator._terminate_owned_tree(lease, owned)
        coordinator._close_owned_tree_boundary(owned)
    with pytest.raises(coordinator.LocalResourceStateError, match="must be cleared"):
        lease.release()
    lease.clear_child()
    assert lease.termination_authority == "none"
    lease.release(outcome="passed", postcondition="exact child absent")
    assert lease.termination_authority == "none"


def test_posix_zombie_only_child_status_clear_and_release_use_exact_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid = 42_420
    child = {
        "pid": child_pid,
        "process_birth": "exact-reaped-birth",
        "tree_kind": "posix-process-group",
    }
    active = _active_record(pid=os.getpid(), process_birth="exact-owner-birth")
    active["child"] = child
    events: list[str] = []
    signals: list[int] = []

    class FakeOwnership:
        acquired = True

        def release(self) -> None:
            events.append("unlock")
            self.acquired = False

    class FakeMutex:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    ownership = FakeOwnership()
    lease = coordinator.LocalResourceLease(
        run_id=cast(str, active["run_id"]),
        profile="ci-final",
        authority_profile="ci-final",
        state_dir=tmp_path,
        capability_token="test-capability",
        inherited=False,
        ownership_lock=ownership,  # type: ignore[arg-type]
    )
    lease._child_recorded = True

    def write_state(path: Path, value: dict[str, object]) -> None:
        assert value is active
        events.append(f"write:{path.name}")

    monkeypatch.setattr(coordinator.os, "name", "posix")
    monkeypatch.setattr(coordinator, "_state_mutex", lambda _state_dir: FakeMutex())
    monkeypatch.setattr(lease, "_verified_active_record", lambda: active)
    monkeypatch.setattr(coordinator, "_read_active_record", lambda _state_dir: active)
    monkeypatch.setattr(coordinator, "_write_state_json", write_state)
    monkeypatch.setattr(
        coordinator,
        "_transition_active_to_last_completed",
        lambda _state_dir, value: events.append("transition") if value is active else None,
    )
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "dead")
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_presence",
        lambda _pgid: events.append("raw-present") or "present",
    )
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_members",
        lambda _pgid: events.append("empty-snapshot") or frozenset(),
    )
    monkeypatch.setattr(
        coordinator,
        "_signal_exact_posix_process_group",
        lambda pid, _birth, _signal: signals.append(pid),
    )

    public = coordinator._public_active_record(active)
    assert isinstance(public["child"], dict)
    assert public["child"]["liveness"] == "dead"

    lease.clear_child()
    assert active["child"] is None
    assert lease.termination_authority == "none"
    lease.release(outcome="passed", postcondition="zombie-only child settled")

    assert lease._released is True
    assert ownership.acquired is False
    assert signals == []
    assert events.count("raw-present") == 2
    assert events.count("empty-snapshot") == 4
    assert "transition" in events
    assert events[-1] == "unlock"


def test_posix_child_record_liveness_keeps_members_and_unknown_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid = 42_421
    child = {
        "pid": child_pid,
        "process_birth": "exact-reaped-birth",
        "tree_kind": "posix-process-group",
    }
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "dead")
    monkeypatch.setattr(coordinator, "_posix_process_group_presence", lambda _pgid: "present")
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_members",
        lambda _pgid: frozenset({child_pid}),
    )
    assert coordinator._child_record_liveness(child) == "live"

    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_members",
        lambda _pgid: frozenset({child_pid + 1}),
    )
    assert coordinator._child_record_liveness(child) == "live"

    snapshots = iter((frozenset(), None))
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_members",
        lambda _pgid: next(snapshots),
    )
    assert coordinator._child_record_liveness(child) == "unknown"

    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "unknown")
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_members",
        lambda _pgid: pytest.fail("unknown direct identity must not scan numeric group members"),
    )
    assert coordinator._child_record_liveness(child) == "unknown"

    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "live")
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_presence",
        lambda _pgid: "absent",
    )
    assert coordinator._child_record_liveness(child, owner_lock_held=False) == "unknown"


@pytest.mark.skipif(os.name != "posix", reason="POSIX orphaned zombie-leader regression")
def test_zombie_leader_settles_only_after_owner_authority_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    root_pid = 42_450
    leader_pid = 42_451
    active = _active_record(pid=root_pid, process_birth="departed-root")
    active["child"] = {
        "pid": leader_pid,
        "process_birth": "exact-zombie-leader",
        "tree_kind": "posix-process-group",
    }
    with coordinator._state_mutex(state_dir):
        coordinator._write_state_json(
            state_dir / coordinator.ACTIVE_STATE_FILE,
            active,
        )

    def process_liveness(pid: object, _birth: object) -> str:
        return "dead" if pid == root_pid else "live"

    scans = 0

    def executable_members(
        pgid: int,
        *,
        retain_zombie_leader: bool = True,
    ) -> frozenset[int]:
        nonlocal scans
        assert pgid == leader_pid
        assert retain_zombie_leader is False
        scans += 1
        return frozenset()

    monkeypatch.setattr(coordinator, "_process_liveness", process_liveness)
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_presence",
        lambda _pgid: "present",
    )
    monkeypatch.setattr(coordinator, "_posix_process_group_members", executable_members)

    child = cast(dict[str, object], active["child"])
    assert coordinator._child_record_liveness(child) == "live"
    assert (
        coordinator._validate_registry_authority(
            active,
            legacy_state="free",
            legacy_metadata={},
        )
        == "stale"
    )
    status = coordinator.read_local_resource_status(
        lock_path=lock_path,
        state_dir=state_dir,
    )
    assert status["state"] == "idle"
    assert status["safe_action"] == "proceed"
    assert scans == 4


def test_bare_contained_python_uses_the_callers_path_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "contained-python"
    expected = shutil.which("python", path=os.environ.get("PATH"))
    assert expected is not None
    monkeypatch.setenv("DJANGO_RAY_TEST_CONTAINED_PYTHON", str(marker))

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="bare-python",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[
            "python",
            "-c",
            (
                "import os,sys; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_CONTAINED_PYTHON'])"
                ".write_text(sys.executable, encoding='utf-8')"
            ),
        ],
    )

    assert exit_code == 0
    assert os.path.samefile(marker.read_text(encoding="utf-8"), expected)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "passed"


def test_explicit_contained_executable_path_bypasses_path_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_resolution(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("explicit executable path must be preserved")

    monkeypatch.setattr(coordinator.shutil, "which", unexpected_resolution)
    command = [sys.executable, "-c", "pass"]

    assert (
        coordinator._resolved_contained_command(
            command,
            environment={"PATH": "unused"},
        )
        == command
    )


def test_missing_bare_contained_executable_fails_closed_and_releases_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(coordinator.shutil, "which", lambda *_args, **_kwargs: None)

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "missing-executable",
            "--root",
            str(tmp_path),
            "--timeout",
            "1",
            "--",
            "missing-executable",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "contained command executable was not found on the captured PATH" in captured.err
    assert "Traceback" not in captured.err
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


def test_record_child_rejects_missing_tree_custody_without_granting_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="child",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    try:
        with pytest.raises(coordinator.LocalResourceStateError, match="tree custody"):
            lease.record_child(
                os.getpid(),
                birth,
                tree_kind=None,  # type: ignore[arg-type]
            )
        assert lease.termination_authority == "none"
        active = coordinator._read_active_record(state_dir)
        assert active is not None
        assert active["child"] is None
    finally:
        lease.release(outcome="passed", postcondition="no child custody granted")


@pytest.mark.parametrize("leader_liveness", ["dead", "unknown"])
def test_posix_group_signal_refuses_unverifiable_or_reused_leader(
    monkeypatch: pytest.MonkeyPatch,
    leader_liveness: str,
) -> None:
    signal_number = 9
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        coordinator,
        "_process_liveness",
        lambda _pid, _birth: leader_liveness,
    )
    monkeypatch.setattr(
        coordinator.os,
        "killpg",
        lambda pid, signal_number: signals.append((pid, signal_number)),
        raising=False,
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="leader identity"):
        coordinator._signal_exact_posix_process_group(
            42_424,
            "proc-start-ticks:original",
            signal_number,
        )

    assert signals == []


def test_posix_group_signal_refuses_leader_exit_during_authority_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_number = 9
    liveness = iter(("live", "dead"))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        coordinator,
        "_process_liveness",
        lambda _pid, _birth: next(liveness),
    )
    monkeypatch.setattr(coordinator.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(coordinator.os, "getsid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        coordinator.os,
        "killpg",
        lambda pid, signal_number: signals.append((pid, signal_number)),
        raising=False,
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="changed before signalling"):
        coordinator._signal_exact_posix_process_group(
            42_425,
            "proc-start-ticks:original",
            signal_number,
        )

    assert signals == []


def test_posix_group_signal_refuses_changed_session_or_group_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_number = 9
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "live")
    monkeypatch.setattr(coordinator.os, "getpgid", lambda pid: pid + 1, raising=False)
    monkeypatch.setattr(coordinator.os, "getsid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        coordinator.os,
        "killpg",
        lambda pid, signal_number: signals.append((pid, signal_number)),
        raising=False,
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="custody no longer matches"):
        coordinator._signal_exact_posix_process_group(
            42_426,
            "proc-start-ticks:original",
            signal_number,
        )

    assert signals == []


def test_posix_group_signal_preserves_exact_live_leader_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_number = 9
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "live")
    monkeypatch.setattr(coordinator.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(coordinator.os, "getsid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        coordinator.os,
        "killpg",
        lambda pid, signal_number: signals.append((pid, signal_number)),
        raising=False,
    )

    coordinator._signal_exact_posix_process_group(
        42_427,
        "proc-start-ticks:original",
        signal_number,
    )

    assert signals == [(42_427, signal_number)]


def test_finish_owned_posix_command_settles_zombies_after_leader_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    signals: list[int] = []

    class FakeLease:
        def clear_child(self) -> None:
            events.append("clear")

        def release(self) -> None:
            events.append("release")

    class FakeProcess:
        pid = 42_428
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None
            events.append("reap")
            self.returncode = 0
            return self.returncode

    process = FakeProcess()
    owned = coordinator._OwnedLocalCommand(
        process=process,  # type: ignore[arg-type]
        process_birth="exact-birth",
        tree_kind="posix-process-group",
        posix_exit_observed=True,
    )
    lease = FakeLease()

    def group_presence(_pgid: object) -> str:
        events.append("raw:reaped" if process.returncode is not None else "raw:held")
        # An adopted zombie keeps the raw numeric process group observable.
        return "present"

    def group_members(_pgid: int) -> frozenset[int]:
        phase = "reaped" if process.returncode is not None else "held"
        events.append(f"snapshot:{phase}")
        # Zombie-aware snapshots retain only the exact leader before wait;
        # after wait, the adopted non-leader zombie is not executable.
        return frozenset() if process.returncode is not None else frozenset({process.pid})

    def leader_liveness(_pid: int, _birth: str) -> str:
        assert process.returncode is None
        events.append("leader-live")
        return "live"

    monkeypatch.setattr(coordinator, "_PROCESS_TREE_SHUTDOWN_SECONDS", 0.0)
    monkeypatch.setattr(coordinator, "_posix_process_group_presence", group_presence)
    monkeypatch.setattr(coordinator, "_posix_process_group_members", group_members)
    monkeypatch.setattr(coordinator, "_process_liveness", leader_liveness)
    monkeypatch.setattr(
        coordinator,
        "_signal_exact_posix_process_group",
        lambda pid, _birth, _signal: signals.append(pid),
    )
    monkeypatch.setattr(
        coordinator,
        "_close_owned_tree_boundary",
        lambda _owned: events.append("close"),
    )

    result = coordinator._finish_owned_command(
        lease=lease,  # type: ignore[arg-type]
        owned=owned,
    )
    lease.release()

    assert result == (0, False)
    assert signals == []
    assert events == [
        "leader-live",
        "raw:held",
        "snapshot:held",
        "snapshot:held",
        "reap",
        "raw:reaped",
        "snapshot:reaped",
        "snapshot:reaped",
        "clear",
        "close",
        "release",
    ]


def test_reaped_posix_leader_settlement_keeps_ambiguous_snapshot_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator, "_posix_process_group_presence", lambda _pgid: "present")
    monkeypatch.setattr(
        coordinator,
        "_posix_process_group_members",
        lambda _pgid: frozenset({42_429}),
    )

    assert (
        coordinator._posix_process_group_survivor_presence(
            42_429,
            leader_pid=42_429,
            leader_reaped=True,
        )
        == "present"
    )

    snapshots = iter((frozenset(), None))
    monkeypatch.setattr(coordinator, "_posix_process_group_members", lambda _pgid: next(snapshots))

    assert (
        coordinator._posix_process_group_survivor_presence(
            42_429,
            leader_pid=42_429,
            leader_reaped=True,
        )
        == "unknown"
    )


def test_posix_termination_refuses_to_signal_after_leader_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class FakeLease:
        def _verify_recorded_child(self, **_identity: object) -> None:
            return None

    class FakeProcess:
        pid = 42_429
        returncode = 0

    owned = coordinator._OwnedLocalCommand(
        process=FakeProcess(),  # type: ignore[arg-type]
        process_birth="exact-birth",
        tree_kind="posix-process-group",
        posix_exit_observed=True,
    )
    monkeypatch.setattr(
        coordinator,
        "_signal_exact_posix_process_group",
        lambda pid, _birth, _signal: signals.append(pid),
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="already reaped"):
        coordinator._terminate_owned_tree(
            FakeLease(),  # type: ignore[arg-type]
            owned,
        )

    assert signals == []


def test_posix_forced_cleanup_settles_descendants_before_reaping_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    signal_number = 9
    monkeypatch.setattr(coordinator.signal, "SIGKILL", signal_number, raising=False)

    class FakeLease:
        def _verify_recorded_child(self, **_identity: object) -> None:
            events.append("verify")

    class FakeProcess:
        pid = 42_430
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == coordinator._PROCESS_TREE_SHUTDOWN_SECONDS
            events.append("reap")
            self.returncode = -signal_number
            return self.returncode

    process = FakeProcess()
    owned = coordinator._OwnedLocalCommand(
        process=process,  # type: ignore[arg-type]
        process_birth="exact-birth",
        tree_kind="posix-process-group",
        posix_exit_observed=True,
    )

    def signal_group(_pid: object, _birth: object, _signal_number: int) -> None:
        assert process.returncode is None
        events.append("signal")

    def tree_absence(
        _owned: coordinator._OwnedLocalCommand,
        *,
        timeout_seconds: float,
    ) -> str:
        assert timeout_seconds == coordinator._PROCESS_TREE_SHUTDOWN_SECONDS
        events.append("settle" if process.returncode is None else "final-absence")
        return "absent"

    monkeypatch.setattr(coordinator, "_signal_exact_posix_process_group", signal_group)
    monkeypatch.setattr(coordinator, "_wait_for_owned_tree_absence", tree_absence)

    coordinator._terminate_owned_tree(FakeLease(), owned)  # type: ignore[arg-type]

    assert events == ["verify", "signal", "settle", "reap", "final-absence"]


def test_posix_error_cleanup_reaps_observed_leader_before_clearing_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeLease:
        def clear_child(self) -> None:
            events.append("clear")

    class FakeProcess:
        pid = 42_434
        stdin = None
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == coordinator._PROCESS_TREE_SHUTDOWN_SECONDS
            events.append("reap")
            self.returncode = 0
            return 0

    process = FakeProcess()
    owned = coordinator._OwnedLocalCommand(
        process=process,  # type: ignore[arg-type]
        process_birth="exact-birth",
        tree_kind="posix-process-group",
        posix_exit_observed=True,
    )

    def tree_absence(
        _owned: coordinator._OwnedLocalCommand,
        *,
        timeout_seconds: float,
    ) -> str:
        events.append(f"absence:{timeout_seconds:g}")
        return "absent"

    monkeypatch.setattr(coordinator, "_wait_for_owned_tree_absence", tree_absence)
    monkeypatch.setattr(coordinator, "_close_owned_tree_boundary", lambda _owned: None)

    coordinator._cleanup_owned_command_after_error(
        lease=FakeLease(),  # type: ignore[arg-type]
        owned=owned,
    )

    assert events == [
        "absence:0",
        "reap",
        f"absence:{coordinator._PROCESS_TREE_SHUTDOWN_SECONDS:g}",
        "clear",
    ]


def test_linux_exit_observation_requests_exact_wait_without_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    class WaitResult:
        si_pid = 42_431

    def waitid(id_type: int, pid: int, options: int) -> WaitResult:
        calls.append((id_type, pid, options))
        return WaitResult()

    monkeypatch.setattr(coordinator.os, "waitid", waitid, raising=False)
    monkeypatch.setattr(coordinator.os, "P_PID", 1, raising=False)
    monkeypatch.setattr(coordinator.os, "WEXITED", 4, raising=False)
    monkeypatch.setattr(coordinator.os, "WNOWAIT", 0x01000000, raising=False)

    coordinator._linux_wait_for_process_exit_without_reaping(42_431)

    assert calls == [(1, 42_431, 4 | 0x01000000)]


def test_darwin_exit_observation_waits_for_exact_zombie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter((psutil.STATUS_RUNNING, psutil.STATUS_ZOMBIE))
    liveness: list[tuple[int, str]] = []

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            assert pid == 42_432

        def status(self) -> str:
            return next(statuses)

    def exact_liveness(pid: object, birth: object) -> str:
        liveness.append((int(pid), str(birth)))
        return "live"

    monkeypatch.setattr(psutil, "Process", FakeProcess)
    monkeypatch.setattr(coordinator, "_process_liveness", exact_liveness)
    monkeypatch.setattr(coordinator.time, "sleep", lambda _seconds: None)

    coordinator._darwin_wait_for_process_exit_without_reaping(42_432, "exact-birth")

    assert liveness == [(42_432, "exact-birth")] * 3


def test_recorded_posix_tree_cleanup_does_not_signal_after_leader_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_number = 9
    signals: list[tuple[int, int]] = []
    verified: list[dict[str, object]] = []

    class FakeLease:
        def _verify_recorded_child(self, **identity: object) -> None:
            verified.append(identity)

    class FakeProcess:
        pid = 42_428

        def wait(self, timeout: float | None = None) -> int:
            raise AssertionError(
                f"reused leader must not be waited after signal refusal: {timeout}"
            )

    monkeypatch.setattr(coordinator.signal, "SIGKILL", signal_number, raising=False)
    monkeypatch.setattr(coordinator, "_process_liveness", lambda _pid, _birth: "dead")
    monkeypatch.setattr(
        coordinator.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
        raising=False,
    )
    owned = coordinator._OwnedLocalCommand(
        process=FakeProcess(),  # type: ignore[arg-type]
        process_birth="proc-start-ticks:original",
        tree_kind="posix-process-group",
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="leader identity"):
        coordinator._terminate_owned_tree(FakeLease(), owned)  # type: ignore[arg-type]

    assert verified == [
        {
            "pid": 42_428,
            "process_birth": "proc-start-ticks:original",
            "tree_kind": "posix-process-group",
        }
    ]
    assert signals == []


def test_unrecorded_posix_cleanup_kills_and_reaps_only_the_exact_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_number = 9
    signals: list[tuple[int, int]] = []
    direct_kills: list[int] = []

    class FakeStream:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        pid = 42_429
        stdin = FakeStream()
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired(cmd="contained-shim", timeout=timeout)
            return -signal_number

        def kill(self) -> None:
            self.killed = True
            direct_kills.append(self.pid)

    monkeypatch.setattr(
        coordinator.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
        raising=False,
    )

    coordinator._abort_unrecorded_launch(
        FakeProcess(),  # type: ignore[arg-type]
        tree_kind="posix-process-group",
        windows_job_handle=None,
    )

    assert FakeProcess.stdin.closed is True
    assert signals == []
    assert direct_kills == [42_429]


def test_unrecorded_posix_cleanup_blocks_for_reap_when_direct_kill_fails() -> None:
    waits: list[float | None] = []

    class FakeStream:
        def close(self) -> None:
            return None

    class FakeProcess:
        pid = 42_433
        stdin = FakeStream()

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="contained-shim", timeout=timeout)
            return coordinator._RUNNER_INTERNAL_FAILURE

        def kill(self) -> None:
            raise PermissionError(errno.EPERM, "injected direct kill failure")

    coordinator._abort_unrecorded_launch(
        FakeProcess(),  # type: ignore[arg-type]
        tree_kind="posix-process-group",
        windows_job_handle=None,
    )

    assert len(waits) == 2
    initial_timeout = waits[0]
    assert initial_timeout is not None
    assert 0 < initial_timeout <= coordinator._PROCESS_TREE_SETTLE_SECONDS
    assert waits[1] is None


@pytest.mark.parametrize("interrupt_phase", ["stream-close", "initial-wait", "direct-kill"])
def test_unrecorded_posix_cleanup_defers_interrupt_until_exact_child_is_reaped(
    interrupt_phase: str,
) -> None:
    events: list[str] = []
    interrupted = False

    def interrupt_once(phase: str) -> None:
        nonlocal interrupted
        if interrupt_phase == phase and not interrupted:
            interrupted = True
            events.append(f"interrupt:{phase}")
            raise KeyboardInterrupt

    class FakeStream:
        def close(self) -> None:
            events.append("close")
            interrupt_once("stream-close")

    class FakeProcess:
        pid = 42_435
        stdin = FakeStream()
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                events.append("settle")
                interrupt_once("initial-wait")
                raise subprocess.TimeoutExpired(cmd="contained-shim", timeout=timeout)
            events.append("reap")
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            events.append("kill")
            interrupt_once("direct-kill")

    process = FakeProcess()
    with pytest.raises(KeyboardInterrupt):
        coordinator._abort_unrecorded_launch(
            process,  # type: ignore[arg-type]
            tree_kind="posix-process-group",
            windows_job_handle=None,
        )

    assert interrupted is True
    assert events[-1] == "reap"
    assert process.returncode == -9


@pytest.mark.parametrize(
    "raised_error",
    [SystemExit(17), _CustomSignalError("custom signal cleanup")],
)
def test_unrecorded_cleanup_restarts_after_an_interrupted_state_transition(
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
) -> None:
    calls = 0

    class FakeProcess:
        stdin = None
        returncode: int | None = None

    def complete_after_transition(state: coordinator._UnrecordedCleanupState) -> None:
        nonlocal calls
        calls += 1
        state.process.returncode = -9
        if calls == 1:
            # This is deliberately outside a process method: the interruption
            # lands after the cleanup state changed but before the action returns.
            raise raised_error

    monkeypatch.setattr(
        coordinator,
        "_complete_unrecorded_posix_cleanup",
        complete_after_transition,
    )

    process = FakeProcess()
    with pytest.raises(type(raised_error)) as raised:
        coordinator._abort_unrecorded_launch(
            process,  # type: ignore[arg-type]
            tree_kind="posix-process-group",
            windows_job_handle=None,
        )

    assert str(raised.value) == str(raised_error)
    assert calls == 2
    assert process.returncode == -9


@pytest.mark.parametrize(
    "interrupt_phase",
    [
        "stream-close",
        "initial-wait",
        "job-terminate",
        "leader-reap",
        "job-query",
        "handle-close",
    ],
)
def test_unrecorded_windows_cleanup_defers_interrupt_until_job_release(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_phase: str,
) -> None:
    events: list[str] = []
    interrupted = False
    terminated = False
    handle_closed = False
    handle_close_calls = 0

    def interrupt_once(phase: str) -> None:
        nonlocal interrupted
        if interrupt_phase == phase and not interrupted:
            interrupted = True
            events.append(f"interrupt:{phase}")
            raise KeyboardInterrupt

    class FakeStream:
        def close(self) -> None:
            events.append("stream-close")
            interrupt_once("stream-close")

    class FakeProcess:
        pid = 42_436
        stdin = FakeStream()
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                events.append("initial-wait")
                interrupt_once("initial-wait")
                raise subprocess.TimeoutExpired(cmd="contained-shim", timeout=timeout)
            events.append("leader-reap")
            interrupt_once("leader-reap")
            self.returncode = coordinator._RUNNER_INTERNAL_FAILURE
            return self.returncode

    def terminate_job(handle: int) -> None:
        nonlocal terminated
        assert handle == 700
        events.append("job-terminate")
        interrupt_once("job-terminate")
        terminated = True

    def active_processes(handle: int) -> int:
        assert handle == 700
        events.append("job-query")
        interrupt_once("job-query")
        return 0 if terminated else 1

    def close_job(handle: int) -> None:
        nonlocal handle_close_calls, handle_closed
        assert handle == 700
        handle_close_calls += 1
        events.append("handle-close")
        handle_closed = True
        # Model an async exception after CloseHandle committed but before its
        # wrapper returned.  Retrying this handle would be unsafe.
        interrupt_once("handle-close")

    monkeypatch.setattr(coordinator, "_terminate_windows_local_job", terminate_job)
    monkeypatch.setattr(coordinator, "_windows_job_active_processes", active_processes)
    monkeypatch.setattr(coordinator, "_close_windows_local_job", close_job)

    process = FakeProcess()
    with pytest.raises(KeyboardInterrupt):
        coordinator._abort_unrecorded_launch(
            process,  # type: ignore[arg-type]
            tree_kind="windows-job",
            windows_job_handle=700,
        )

    assert interrupted is True
    assert terminated is True
    assert process.returncode == coordinator._RUNNER_INTERNAL_FAILURE
    assert handle_closed is True
    assert handle_close_calls == 1
    if interrupt_phase == "handle-close":
        assert events[-2:] == ["handle-close", "interrupt:handle-close"]
    else:
        assert events[-1] == "handle-close"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job descendant custody")
def test_dead_job_leader_cannot_clear_or_release_while_grandchild_is_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    grandchild_path = tmp_path / "job-grandchild.pid"
    monkeypatch.setenv("DJANGO_RAY_TEST_GRANDCHILD", str(grandchild_path))
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="job-descendant",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    owned = _launch_owned_for_test(
        lease=lease,
        rootpath=Path.cwd(),
        command=[
            sys.executable,
            "-c",
            (
                "import os,subprocess,sys; from pathlib import Path; "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(30)'], close_fds=True); "
                "Path(os.environ['DJANGO_RAY_TEST_GRANDCHILD']).write_text(str(child.pid))"
            ),
        ],
    )
    grandchild_pid: int | None = None
    try:
        assert owned.process.wait(timeout=10) == 0
        deadline = time.monotonic() + 5
        while not grandchild_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        grandchild_pid = int(grandchild_path.read_text(encoding="ascii"))
        assert coordinator._pid_presence(owned.process.pid) == "absent"
        assert coordinator._pid_presence(grandchild_pid) == "present"
        retained_handle = lease._windows_child_job_handle
        assert retained_handle is not None
        assert coordinator._windows_job_active_processes(retained_handle) >= 1

        with pytest.raises(coordinator.LocalResourceStateError, match="active members"):
            lease.clear_child()
        with pytest.raises(coordinator.LocalResourceStateError, match="must be cleared"):
            lease.release()
        active = coordinator._read_active_record(state_dir)
        assert active is not None and isinstance(active["child"], dict)
    finally:
        coordinator._terminate_owned_tree(lease, owned)
        lease.clear_child()
        coordinator._close_owned_tree_boundary(owned)
        lease.release(outcome="passed", postcondition="Job descendants absent")

    assert grandchild_pid is not None
    assert coordinator._pid_presence(grandchild_pid) == "absent"
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job authority handle")
def test_job_duplicate_failure_never_releases_go_or_persists_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "duplicate-failure-marker"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))

    def fail_duplicate(_handle: int) -> int:
        raise coordinator.LocalResourceStateError("injected Job duplication failure")

    monkeypatch.setattr(coordinator, "_duplicate_windows_local_job", fail_duplicate)

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "duplicate-failure",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
            ),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert marker.exists() is False
    assert "injected Job duplication failure" in captured.err
    assert "Traceback" not in captured.err
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["active"] is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job authority handle")
def test_committed_retained_job_close_state_error_is_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="job-close",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    owned = _launch_owned_for_test(
        lease=lease,
        rootpath=Path.cwd(),
        command=[sys.executable, "-c", "pass"],
    )
    assert owned.process.wait(timeout=10) == 0
    retained_handle = lease._windows_child_job_handle
    assert retained_handle is not None
    import ctypes
    from ctypes import wintypes

    get_handle_information = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).GetHandleInformation
    get_handle_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_handle_information.restype = wintypes.BOOL
    handle_flags = wintypes.DWORD()
    assert get_handle_information(wintypes.HANDLE(retained_handle), ctypes.byref(handle_flags))
    assert handle_flags.value & 0x00000001 == 0  # HANDLE_FLAG_INHERIT
    close_job = coordinator._close_windows_local_job
    retained_close_calls = 0

    def close_then_raise_state_error(handle: int) -> None:
        nonlocal retained_close_calls
        if handle == retained_handle:
            retained_close_calls += 1
            close_job(handle)
            raise coordinator.LocalResourceStateError(
                "async state error after retained Job close committed"
            )
        close_job(handle)

    monkeypatch.setattr(
        coordinator,
        "_close_windows_local_job",
        close_then_raise_state_error,
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="close committed"):
        lease.clear_child()
    assert retained_close_calls == 1
    assert lease._windows_child_job_handle is None
    assert lease.termination_authority == "none"
    active = coordinator._read_active_record(state_dir)
    assert active is not None and active["child"] is None

    lease.clear_child()
    assert retained_close_calls == 1
    assert lease._windows_child_job_handle is None
    assert lease.termination_authority == "none"
    coordinator._close_owned_tree_boundary(owned)
    lease.release(outcome="passed", postcondition="retained Job handle closed")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job authority handle")
def test_committed_retained_job_close_is_consumed_before_async_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="committed-retained-job-close",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    owned = _launch_owned_for_test(
        lease=lease,
        rootpath=Path.cwd(),
        command=[sys.executable, "-c", "pass"],
    )
    assert owned.process.wait(timeout=10) == 0
    retained_handle = lease._windows_child_job_handle
    assert retained_handle is not None
    close_job = coordinator._close_windows_local_job
    retained_close_calls = 0

    def close_then_interrupt(handle: int) -> None:
        nonlocal retained_close_calls
        if handle == retained_handle:
            retained_close_calls += 1
            close_job(handle)
            raise _CustomSignalError("async signal after retained Job close committed")
        close_job(handle)

    monkeypatch.setattr(
        coordinator,
        "_close_windows_local_job",
        close_then_interrupt,
    )

    with pytest.raises(_CustomSignalError, match="retained Job close committed"):
        lease.clear_child()

    assert retained_close_calls == 1
    assert lease._windows_child_job_handle is None
    assert lease.termination_authority == "none"
    active = coordinator._read_active_record(state_dir)
    assert active is not None and active["child"] is None

    # The ambiguous numeric handle was consumed before CloseHandle.  Retrying
    # cleanup must not query or close a potentially recycled unrelated handle.
    lease.clear_child()
    assert retained_close_calls == 1
    coordinator._close_owned_tree_boundary(owned)
    lease.release(outcome="passed", postcondition="committed retained close not retried")


def test_committed_owned_job_close_state_error_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pass

    owned = coordinator._OwnedLocalCommand(
        process=FakeProcess(),  # type: ignore[arg-type]
        process_birth="fake-birth",
        tree_kind="windows-job",
        windows_job_handle=700,
    )
    close_calls: list[int] = []

    def close_then_interrupt(handle: int) -> None:
        close_calls.append(handle)
        raise coordinator.LocalResourceStateError(
            "async state error after original Job close committed"
        )

    monkeypatch.setattr(coordinator, "_close_windows_local_job", close_then_interrupt)

    with pytest.raises(coordinator.LocalResourceStateError, match="close committed"):
        coordinator._close_owned_tree_boundary(owned)

    assert close_calls == [700]
    assert owned.windows_job_handle is None
    coordinator._close_owned_tree_boundary(owned)
    assert close_calls == [700]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job duplicate handle")
def test_record_child_retains_duplicate_when_commit_reconciliation_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, _lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="record-child-duplicate-close",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    birth = coordinator._process_birth(os.getpid())
    assert birth is not None
    close_calls: list[int] = []
    verified_active_record = coordinator.LocalResourceLease._verified_active_record

    monkeypatch.setattr(coordinator, "_duplicate_windows_local_job", lambda _handle: 700)
    monkeypatch.setattr(coordinator, "_windows_pid_is_in_job", lambda _pid, _handle: True)

    def fail_record_after_duplicate(
        _lease: coordinator.LocalResourceLease,
    ) -> dict[str, object]:
        raise coordinator.LocalResourceStateError("injected durable record failure")

    def observe_close(handle: int) -> None:
        close_calls.append(handle)

    monkeypatch.setattr(
        coordinator.LocalResourceLease,
        "_verified_active_record",
        fail_record_after_duplicate,
    )
    monkeypatch.setattr(
        coordinator,
        "_close_windows_local_job",
        observe_close,
    )

    with pytest.raises(coordinator.LocalResourceStateError, match="durable record failure"):
        lease.record_child(
            os.getpid(),
            birth,
            tree_kind="windows-job",
            windows_job_handle=600,
        )

    assert close_calls == []
    assert lease._windows_child_job_handle == 700
    assert lease.termination_authority == "owned-child-tree"
    active = coordinator._read_active_record(state_dir)
    assert active is not None and active["child"] is None

    monkeypatch.setattr(
        coordinator.LocalResourceLease,
        "_verified_active_record",
        verified_active_record,
    )
    monkeypatch.setattr(coordinator, "_windows_job_active_processes", lambda _handle: 0)
    lease.clear_child()
    assert close_calls == [700]
    assert lease._windows_child_job_handle is None
    assert lease.termination_authority == "none"
    lease.release(outcome="passed", postcondition="duplicate handle consumed")


def test_contained_runner_records_custody_before_releasing_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "command-started"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    original_record_child = coordinator.LocalResourceLease.record_child
    observations: list[str] = []

    def record_before_go(
        lease: coordinator.LocalResourceLease,
        pid: int,
        process_birth: str,
        *,
        tree_kind: str,
        windows_job_handle: int | None = None,
    ) -> None:
        assert marker.exists() is False
        original_record_child(
            lease,
            pid,
            process_birth,
            tree_kind=tree_kind,  # type: ignore[arg-type]
            windows_job_handle=windows_job_handle,
        )
        assert marker.exists() is False
        observations.append(tree_kind or "direct")

    monkeypatch.setattr(coordinator.LocalResourceLease, "record_child", record_before_go)

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="command",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('started')"
            ),
        ],
    )

    assert exit_code == 0
    assert marker.read_text(encoding="utf-8") == "started"
    assert observations == ["windows-job" if os.name == "nt" else "posix-process-group"]
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["postcondition"] == "owned process tree absent"


def test_launch_shim_disables_startup_hooks_before_pre_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    hook_dir = tmp_path / "startup-hook"
    hook_dir.mkdir()
    startup_marker = tmp_path / "startup-hook-ran"
    descendant_pid_path = tmp_path / "startup-hook-descendant-pid"
    monkeypatch.setenv("PYTHONPATH", str(hook_dir))
    monkeypatch.setenv("DJANGO_RAY_TEST_STARTUP_MARKER", str(startup_marker))
    monkeypatch.setenv("DJANGO_RAY_TEST_DESCENDANT_PID", str(descendant_pid_path))
    descendant_code = (
        "import os, time; from pathlib import Path; "
        "Path(os.environ['DJANGO_RAY_TEST_DESCENDANT_PID']).write_text("
        "str(os.getpid()), encoding='ascii'); time.sleep(30)"
    )
    (hook_dir / "sitecustomize.py").write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['DJANGO_RAY_TEST_STARTUP_MARKER']).write_text("
        "'started', encoding='ascii')\n"
        f"subprocess.Popen([sys.executable, '-I', '-S', '-c', {descendant_code!r}], "
        "close_fds=True)\n",
        encoding="utf-8",
    )
    wait_for_birth = coordinator._wait_for_process_birth
    observations: list[tuple[bool, bool]] = []

    def fail_before_record(process: subprocess.Popen[bytes]) -> str:
        args = process.args
        assert isinstance(args, list)
        assert args[1:3] == ["-I", "-S"]
        wait_for_birth(process)
        deadline = time.monotonic() + 1
        while (
            not startup_marker.exists()
            and not descendant_pid_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        observations.append((startup_marker.exists(), descendant_pid_path.exists()))
        raise coordinator.LocalResourceStateError("injected failure before child recording")

    monkeypatch.setattr(coordinator, "_wait_for_process_birth", fail_before_record)
    with pytest.raises(coordinator.LocalResourceStateError, match="before child recording"):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="isolated-shim-startup",
            rootpath=Path.cwd(),
            timeout_seconds=1,
            command=[sys.executable, "-c", "pass"],
        )

    assert observations == [(False, False)]
    assert startup_marker.exists() is False
    assert descendant_pid_path.exists() is False
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["termination_authority"] == "none"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX unreaped leader regression")
def test_posix_no_descendant_command_does_not_misclassify_leader_as_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(coordinator, "_PROCESS_TREE_NATURAL_EXIT_SECONDS", 0.05)

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="leader-only",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[sys.executable, "-c", "pass"],
    )

    assert exit_code == 0
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "passed"
    assert status["last_completed"]["postcondition"] == "owned process tree absent"


def test_contained_runner_cleanup_windows_are_bounded_and_distinct() -> None:
    assert coordinator._PROCESS_TREE_SETTLE_SECONDS == 2.0
    assert coordinator._PROCESS_TREE_NATURAL_EXIT_SECONDS == 30.0
    assert coordinator._PROCESS_TREE_SHUTDOWN_SECONDS == 10.0


def test_contained_runner_allows_delayed_descendant_to_exit_naturally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    descendant_pid_path = tmp_path / "delayed-descendant.pid"
    completed_path = tmp_path / "delayed-descendant-completed"
    monkeypatch.setenv("DJANGO_RAY_TEST_DESCENDANT_PID", str(descendant_pid_path))
    monkeypatch.setenv("DJANGO_RAY_TEST_DESCENDANT_COMPLETED", str(completed_path))
    monkeypatch.setattr(coordinator, "_PROCESS_TREE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(coordinator, "_PROCESS_TREE_NATURAL_EXIT_SECONDS", 3.0)
    terminate = coordinator._terminate_owned_tree
    terminated: list[int] = []

    def observe_termination(
        lease: coordinator.LocalResourceLease,
        owned: coordinator._OwnedLocalCommand,
    ) -> None:
        terminated.append(owned.process.pid)
        terminate(lease, owned)

    monkeypatch.setattr(coordinator, "_terminate_owned_tree", observe_termination)
    descendant = (
        "import os,time; from pathlib import Path; "
        "Path(os.environ['DJANGO_RAY_TEST_DESCENDANT_PID']).write_text(str(os.getpid())); "
        "time.sleep(0.25); "
        "Path(os.environ['DJANGO_RAY_TEST_DESCENDANT_COMPLETED']).write_text('done')"
    )

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="natural-descendant-exit",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[
            sys.executable,
            "-c",
            (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}], close_fds=True)"
            ),
        ],
    )

    descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
    assert exit_code == 0
    assert completed_path.read_text(encoding="utf-8") == "done"
    assert terminated == []
    assert _pid_is_absent_or_zombie(descendant_pid)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "passed"
    assert status["last_completed"]["postcondition"] == "owned process tree absent"


def test_contained_runner_fails_closed_after_terminating_durable_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    descendant_pid_path = tmp_path / "durable-descendant.pid"
    monkeypatch.setenv("DJANGO_RAY_TEST_DESCENDANT_PID", str(descendant_pid_path))
    monkeypatch.setattr(coordinator, "_PROCESS_TREE_NATURAL_EXIT_SECONDS", 0.05)
    terminate = coordinator._terminate_owned_tree
    terminated: list[int] = []

    def observe_termination(
        lease: coordinator.LocalResourceLease,
        owned: coordinator._OwnedLocalCommand,
    ) -> None:
        terminated.append(owned.process.pid)
        terminate(lease, owned)

    monkeypatch.setattr(coordinator, "_terminate_owned_tree", observe_termination)

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="durable-descendant",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[
            sys.executable,
            "-c",
            (
                "import os,subprocess,sys; from pathlib import Path; "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(30)'], close_fds=True); "
                "Path(os.environ['DJANGO_RAY_TEST_DESCENDANT_PID']).write_text(str(child.pid))"
            ),
        ],
    )

    descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
    assert exit_code == coordinator._RUNNER_INTERNAL_FAILURE
    assert len(terminated) == 1
    deadline = time.monotonic() + 10
    while not _pid_is_absent_or_zombie(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _pid_is_absent_or_zombie(descendant_pid)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"
    assert status["last_completed"]["postcondition"] == (
        "post-launcher descendants terminated; owned process tree absent"
    )


def test_go_barrier_never_launches_command_when_durable_recording_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "must-not-start"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))

    def reject_recording(
        _lease: coordinator.LocalResourceLease,
        _pid: int,
        _process_birth: str,
        *,
        tree_kind: str,
        windows_job_handle: int | None = None,
    ) -> None:
        del tree_kind, windows_job_handle
        raise coordinator.LocalResourceStateError("injected durable record failure")

    monkeypatch.setattr(coordinator.LocalResourceLease, "record_child", reject_recording)

    with pytest.raises(
        coordinator.LocalResourceStateError, match="injected durable record failure"
    ):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="command",
            rootpath=Path.cwd(),
            timeout_seconds=1,
            command=[
                sys.executable,
                "-c",
                (
                    "import os; from pathlib import Path; "
                    "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
                ),
            ],
        )

    assert marker.exists() is False
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"


@pytest.mark.skipif(os.name != "posix", reason="POSIX stopped pre-GO shim regression")
def test_stopped_pre_go_shim_is_killed_and_reaped_before_lane_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "must-not-start-after-stop"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    monkeypatch.setattr(coordinator, "_PROCESS_TREE_SETTLE_SECONDS", 0.05)
    shim_pids: list[int] = []

    def stop_and_reject_recording(
        _lease: coordinator.LocalResourceLease,
        pid: int,
        _process_birth: str,
        *,
        tree_kind: str,
        windows_job_handle: int | None = None,
    ) -> None:
        del tree_kind, windows_job_handle
        shim_pids.append(pid)
        os.kill(pid, signal.SIGSTOP)
        raise coordinator.LocalResourceStateError("injected stopped durable record failure")

    monkeypatch.setattr(coordinator.LocalResourceLease, "record_child", stop_and_reject_recording)

    with pytest.raises(
        coordinator.LocalResourceStateError,
        match="injected stopped durable record failure",
    ):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="stopped-pre-go",
            rootpath=Path.cwd(),
            timeout_seconds=1,
            command=[
                sys.executable,
                "-c",
                (
                    "import os; from pathlib import Path; "
                    "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
                ),
            ],
        )

    assert len(shim_pids) == 1
    assert coordinator._pid_presence(shim_pids[0]) == "absent"
    assert marker.exists() is False
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


@pytest.mark.parametrize(
    "completion_error",
    [KeyboardInterrupt(), _CustomSignalError("custom completion signal")],
)
def test_main_interrupt_during_popen_return_cannot_lose_child_custody(
    monkeypatch: pytest.MonkeyPatch,
    completion_error: BaseException,
) -> None:
    factory_has_child = threading.Event()
    permit_factory_return = threading.Event()
    interrupted = False
    record_calls: list[int] = []

    class FakeStream:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        pid = 42_440
        stdin = FakeStream()
        returncode: int | None = None
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                if timeout is not None:
                    raise subprocess.TimeoutExpired("contained-shim", timeout)
                raise AssertionError("the retained pre-GO child must be killed before reap")
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.killed = True

    class FakeLease:
        def inheritance_environment(self) -> dict[str, str]:
            return {}

        def record_child(self, pid: int, *_args: object, **_kwargs: object) -> None:
            record_calls.append(pid)

    process = FakeProcess()

    def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        # Model the exact vulnerable interval: the OS child exists, while the
        # Popen factory has not returned it to the caller yet.
        factory_has_child.set()
        assert permit_factory_return.wait(timeout=2)
        return process

    def interrupt_completion_wait(state: coordinator._DeferredPopenState) -> None:
        nonlocal interrupted
        assert factory_has_child.wait(timeout=2)
        permit_factory_return.set()
        if not interrupted:
            interrupted = True
            raise completion_error
        assert state.complete.wait(timeout=2)

    monkeypatch.setattr(coordinator.subprocess, "Popen", create_process)
    monkeypatch.setattr(
        coordinator,
        "_wait_for_deferred_popen_completion",
        interrupt_completion_wait,
    )

    with pytest.raises(type(completion_error)):
        _launch_owned_for_test(
            lease=FakeLease(),  # type: ignore[arg-type]
            command=[sys.executable, "-c", "pass"],
            rootpath=Path.cwd(),
        )

    assert interrupted is True
    assert record_calls == []
    assert process.stdin.closed is True
    assert process.killed is True
    assert process.returncode == -9


def test_deferred_user_interrupt_remains_primary_when_popen_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_started = threading.Event()
    permit_failure = threading.Event()
    interrupted = False

    class FakeLease:
        def inheritance_environment(self) -> dict[str, str]:
            return {}

    def fail_process_creation(*_args: object, **_kwargs: object) -> object:
        factory_started.set()
        assert permit_failure.wait(timeout=2)
        raise FileNotFoundError("secondary process creation failure")

    def interrupt_completion_wait(state: coordinator._DeferredPopenState) -> None:
        nonlocal interrupted
        assert factory_started.wait(timeout=2)
        permit_failure.set()
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        assert state.complete.wait(timeout=2)

    monkeypatch.setattr(coordinator.subprocess, "Popen", fail_process_creation)
    monkeypatch.setattr(
        coordinator,
        "_wait_for_deferred_popen_completion",
        interrupt_completion_wait,
    )

    with pytest.raises(KeyboardInterrupt):
        _launch_owned_for_test(
            lease=FakeLease(),  # type: ignore[arg-type]
            command=[sys.executable, "-c", "pass"],
            rootpath=Path.cwd(),
        )

    assert interrupted is True


def test_worker_start_boundary_exception_cancels_before_popen_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_worker = coordinator._start_deferred_popen_worker
    observed_states: list[coordinator._DeferredPopenState] = []
    process_creations = 0

    class FakeLease:
        def inheritance_environment(self) -> dict[str, str]:
            return {}

    def fail_after_worker_ready(
        state: coordinator._DeferredPopenState,
        create_process: Callable[[], subprocess.Popen[bytes]],
    ) -> None:
        start_worker(state, create_process)
        observed_states.append(state)
        raise _CustomSignalError("custom start-boundary signal")

    def observe_process_creation(*_args: object, **_kwargs: object) -> object:
        nonlocal process_creations
        process_creations += 1
        raise AssertionError("cancelled launch must never enter Popen")

    monkeypatch.setattr(
        coordinator,
        "_start_deferred_popen_worker",
        fail_after_worker_ready,
    )
    monkeypatch.setattr(coordinator.subprocess, "Popen", observe_process_creation)

    with pytest.raises(_CustomSignalError, match="start-boundary"):
        _launch_owned_for_test(
            lease=FakeLease(),  # type: ignore[arg-type]
            command=[sys.executable, "-c", "pass"],
            rootpath=Path.cwd(),
        )

    assert len(observed_states) == 1
    state = observed_states[0]
    assert state.cancelled.is_set()
    assert state.permission.is_set()
    assert state.complete.wait(timeout=2)
    assert process_creations == 0


def test_committed_worker_cancellation_waits_for_retained_popen_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_creation_started = threading.Event()
    permit_process_return = threading.Event()
    wait_calls = 0

    class FakeProcess:
        pass

    process = FakeProcess()
    state = coordinator._DeferredPopenState(
        ready=threading.Event(),
        permission=threading.Event(),
        cancelled=threading.Event(),
        complete=threading.Event(),
    )

    def create_process() -> subprocess.Popen[bytes]:
        process_creation_started.set()
        assert permit_process_return.wait(timeout=2)
        return cast(subprocess.Popen[bytes], process)

    wait_for_completion = coordinator._wait_for_deferred_popen_completion

    def release_creation_then_wait(state: coordinator._DeferredPopenState) -> None:
        nonlocal wait_calls
        wait_calls += 1
        permit_process_return.set()
        wait_for_completion(state)

    coordinator._start_deferred_popen_worker(state, create_process)
    state.permission.set()
    assert process_creation_started.wait(timeout=2)
    monkeypatch.setattr(
        coordinator,
        "_wait_for_deferred_popen_completion",
        release_creation_then_wait,
    )

    coordinator._cancel_uncommitted_popen_worker(state)

    assert wait_calls == 1
    assert state.complete.is_set()
    assert state.process is process
    assert state.cancelled.is_set() is False


def test_sigint_in_successful_launch_finally_waits_for_caller_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    finally_needs_cleanup = coordinator._launch_finally_needs_cleanup
    wait_for_birth = coordinator._wait_for_process_birth
    injected = False
    launched_pids: list[int] = []

    def inject_at_successful_finally(
        *,
        launch_succeeded: bool,
        launch_error: BaseException | None,
    ) -> bool:
        nonlocal injected
        if launch_succeeded and launch_error is None and not injected:
            injected = True
            signal.raise_signal(signal.SIGINT)
        return finally_needs_cleanup(
            launch_succeeded=launch_succeeded,
            launch_error=launch_error,
        )

    def remember_process(process: subprocess.Popen[bytes]) -> str:
        launched_pids.append(process.pid)
        return wait_for_birth(process)

    monkeypatch.setattr(
        coordinator,
        "_launch_finally_needs_cleanup",
        inject_at_successful_finally,
    )
    monkeypatch.setattr(coordinator, "_wait_for_process_birth", remember_process)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.run_local_resource_command(
                profile="ci-final",
                phase="successful-finally-ctrl-c",
                rootpath=Path.cwd(),
                timeout_seconds=1,
                command=[sys.executable, "-c", "import time; time.sleep(10)"],
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert injected is True
    assert len(launched_pids) == 1
    assert coordinator._pid_presence(launched_pids[0]) == "absent"
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_sigint_from_success_to_cleanup_transition_is_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    enter_suppress = coordinator._MainThreadSigintGuard.enter_suppress_mode
    injected = False

    def interrupt_first_transition(guard: coordinator._MainThreadSigintGuard) -> None:
        nonlocal injected
        if not injected:
            injected = True
            guard.mode = "suppress"
            raise KeyboardInterrupt
        enter_suppress(guard)

    monkeypatch.setattr(
        coordinator._MainThreadSigintGuard,
        "enter_suppress_mode",
        interrupt_first_transition,
    )

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "success-cleanup-transition-ctrl-c",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )

    captured = capsys.readouterr()
    assert injected is True
    assert exit_code == 130
    assert captured.err == ""
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_sigint_after_acquire_return_uses_retained_lease_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    acquire = coordinator._acquire_local_resources
    retained_by_wrapper: list[coordinator.LocalResourceLease] = []
    marker = tmp_path / "must-not-launch-after-acquire-ctrl-c"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))

    def acquire_then_interrupt(
        *,
        profile: str,
        phase: str,
        rootpath: Path | str,
        selected_count: int | None = None,
        timeout_seconds: float = 14_400,
        progress_interval_seconds: float = 30,
        progress: Callable[[str], None] | None = None,
        retained: Callable[[coordinator.LocalResourceLease], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> coordinator.LocalResourceLease:
        lease = acquire(
            profile=profile,
            phase=phase,
            rootpath=rootpath,
            selected_count=selected_count,
            timeout_seconds=timeout_seconds,
            progress_interval_seconds=progress_interval_seconds,
            progress=progress,
            retained=retained,
            cancel_requested=cancel_requested,
        )
        retained_by_wrapper.append(lease)
        signal.raise_signal(signal.SIGINT)
        return lease

    monkeypatch.setattr(coordinator, "_acquire_local_resources", acquire_then_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        exit_code = coordinator.main(
            [
                "run",
                "--profile",
                "ci-final",
                "--phase",
                "acquire-return-ctrl-c",
                "--root",
                str(Path.cwd()),
                "--timeout",
                "1",
                "--",
                sys.executable,
                "-c",
                (
                    "import os; from pathlib import Path; "
                    "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
                ),
            ]
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err == ""
    assert marker.exists() is False
    assert len(retained_by_wrapper) == 1
    assert retained_by_wrapper[0]._released is True
    ownership = retained_by_wrapper[0]._ownership_lock
    assert ownership is None or ownership.acquired is False
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_uncertain_registration_commit_removes_exact_queue_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    register = coordinator._register_queue_request

    def register_then_raise(
        *,
        state_dir: Path,
        lock_path: Path,
        request_id: str,
        profile: str,
        phase: str,
        owner: coordinator.LocalResourceOwner,
        source: coordinator.LocalResourceSource,
        intent: str,
        handoff: str | None,
    ) -> int:
        register(
            state_dir=state_dir,
            lock_path=lock_path,
            request_id=request_id,
            profile=profile,
            phase=phase,
            owner=owner,
            source=source,
            intent=intent,
            handoff=handoff,
        )
        raise coordinator.LocalResourceStateError("injected failure after registration commit")

    monkeypatch.setattr(coordinator, "_register_queue_request", register_then_raise)

    with pytest.raises(
        coordinator.LocalResourceStateError,
        match="failure after registration commit",
    ):
        coordinator.acquire_local_resources(
            profile="ci-final",
            phase="uncertain-registration",
            rootpath=tmp_path,
            timeout_seconds=1,
        )

    queue_state = coordinator._read_queue_state(state_dir)
    assert queue_state["items"] == []
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["queue_total"] == 0


def test_uncertain_registration_cleanup_failure_remains_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    register = coordinator._register_queue_request

    def register_then_raise(
        *,
        state_dir: Path,
        lock_path: Path,
        request_id: str,
        profile: str,
        phase: str,
        owner: coordinator.LocalResourceOwner,
        source: coordinator.LocalResourceSource,
        intent: str,
        handoff: str | None,
    ) -> int:
        register(
            state_dir=state_dir,
            lock_path=lock_path,
            request_id=request_id,
            profile=profile,
            phase=phase,
            owner=owner,
            source=source,
            intent=intent,
            handoff=handoff,
        )
        raise coordinator.LocalResourceStateError("injected failure after registration commit")

    def fail_queue_removal(*, state_dir: Path, request_id: str) -> None:
        del state_dir, request_id
        raise coordinator.LocalResourceStateError("injected uncertain-registration cleanup failure")

    monkeypatch.setattr(coordinator, "_register_queue_request", register_then_raise)
    monkeypatch.setattr(coordinator, "_remove_queue_request", fail_queue_removal)

    with pytest.raises(
        coordinator.LocalResourceStateError,
        match="uncertain-registration cleanup failure",
    ) as raised:
        coordinator.acquire_local_resources(
            profile="ci-final",
            phase="uncertain-registration-cleanup",
            rootpath=tmp_path,
            timeout_seconds=1,
        )

    assert isinstance(raised.value.__context__, coordinator.LocalResourceStateError)
    assert "failure after registration commit" in str(raised.value.__context__)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["queue_total"] == 1
    queue = status["queue"]
    assert isinstance(queue, list) and len(queue) == 1
    assert queue[0]["phase"] == "uncertain-registration-cleanup"


def test_signal_after_promotion_return_releases_internally_retained_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    promote = coordinator._promote_queue_head
    injected = False

    def promote_then_interrupt(
        *,
        state_dir: Path,
        lock_path: Path,
        request_id: str,
        run_id: str,
        capability_token: str,
        profile: str,
        phase: str,
        owner: coordinator.LocalResourceOwner,
        rootpath: Path,
        intent: str,
        handoff: str | None,
        selected_count: int | None,
        retain_acquired: Callable[[coordinator.LocalResourceLease], None],
    ) -> tuple[coordinator.LocalResourceLease | None, int]:
        nonlocal injected
        result = promote(
            state_dir=state_dir,
            lock_path=lock_path,
            request_id=request_id,
            run_id=run_id,
            capability_token=capability_token,
            profile=profile,
            phase=phase,
            owner=owner,
            rootpath=rootpath,
            intent=intent,
            handoff=handoff,
            selected_count=selected_count,
            retain_acquired=retain_acquired,
        )
        if result[0] is not None and not injected:
            injected = True
            signal.raise_signal(signal.SIGINT)
        return result

    monkeypatch.setattr(coordinator, "_promote_queue_head", promote_then_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.acquire_local_resources(
                profile="ci-final",
                phase="promotion-return-ctrl-c",
                rootpath=tmp_path,
                timeout_seconds=1,
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert injected is True
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["queue_total"] == 0
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_public_acquisition_callback_failure_releases_retained_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    observed: list[coordinator.LocalResourceLease] = []

    def fail_callback(lease: coordinator.LocalResourceLease) -> None:
        observed.append(lease)
        raise _CustomSignalError("injected acquisition callback failure")

    with pytest.raises(_CustomSignalError, match="acquisition callback failure"):
        coordinator.acquire_local_resources(
            profile="ci-final",
            phase="callback-failure",
            rootpath=tmp_path,
            timeout_seconds=1,
            on_acquired=fail_callback,
        )

    assert len(observed) == 1
    assert observed[0]._released is True
    assert observed[0]._ownership_lock is None
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"


def test_active_publication_after_commit_fault_releases_exact_lease_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    write_state = coordinator._write_state_json
    injected = False
    retained: list[coordinator.LocalResourceLease] = []

    def write_then_raise(path: Path, value: dict[str, object]) -> None:
        nonlocal injected
        write_state(path, value)
        if (
            not injected
            and path.name == coordinator.ACTIVE_STATE_FILE
            and value.get("child") is None
            and value.get("outcome") is None
        ):
            injected = True
            raise coordinator.LocalResourceStateError(
                "injected fault after active publication commit"
            )

    monkeypatch.setattr(coordinator, "_write_state_json", write_then_raise)
    with pytest.raises(coordinator.LocalResourceStateError, match="publication commit"):
        coordinator.acquire_local_resources(
            profile="ci-final",
            phase="active-after-commit",
            rootpath=tmp_path,
            timeout_seconds=1,
            on_acquired=retained.append,
        )

    assert injected is True
    assert retained == []
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["queue_total"] == 0
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"
    assert status["last_completed"]["postcondition"] == (
        "acquisition aborted after active publication"
    )


def test_active_publication_after_commit_keyboard_interrupt_releases_exact_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    write_state = coordinator._write_state_json
    injected = False
    public_retained: list[coordinator.LocalResourceLease] = []

    def write_then_interrupt(path: Path, value: dict[str, object]) -> None:
        nonlocal injected
        write_state(path, value)
        if (
            not injected
            and path.name == coordinator.ACTIVE_STATE_FILE
            and value.get("child") is None
            and value.get("outcome") is None
        ):
            injected = True
            raise KeyboardInterrupt

    monkeypatch.setattr(coordinator, "_write_state_json", write_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        coordinator.acquire_local_resources(
            profile="ci-final",
            phase="active-after-commit-interrupt",
            rootpath=tmp_path,
            timeout_seconds=1,
            on_acquired=public_retained.append,
        )

    assert injected is True
    assert public_retained == []
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["queue_total"] == 0
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_child_record_after_commit_fault_cleans_exact_custody_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    write_state = coordinator._write_state_json
    injected = False

    def write_then_raise(path: Path, value: dict[str, object]) -> None:
        nonlocal injected
        write_state(path, value)
        if (
            not injected
            and path.name == coordinator.ACTIVE_STATE_FILE
            and isinstance(value.get("child"), dict)
        ):
            injected = True
            raise coordinator.LocalResourceStateError("injected fault after child recording commit")

    monkeypatch.setattr(coordinator, "_write_state_json", write_then_raise)
    with pytest.raises(coordinator.LocalResourceStateError, match="recording commit"):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="child-after-commit",
            rootpath=Path.cwd(),
            timeout_seconds=1,
            command=[sys.executable, "-c", "pass"],
        )

    assert injected is True
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["termination_authority"] == "none"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


def test_child_record_after_commit_keyboard_interrupt_cleans_exact_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    write_state = coordinator._write_state_json
    injected = False

    def write_then_interrupt(path: Path, value: dict[str, object]) -> None:
        nonlocal injected
        write_state(path, value)
        if (
            not injected
            and path.name == coordinator.ACTIVE_STATE_FILE
            and isinstance(value.get("child"), dict)
        ):
            injected = True
            raise KeyboardInterrupt

    monkeypatch.setattr(coordinator, "_write_state_json", write_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        coordinator.run_local_resource_command(
            profile="ci-final",
            phase="child-after-commit-interrupt",
            rootpath=Path.cwd(),
            timeout_seconds=1,
            command=[sys.executable, "-c", "pass"],
        )

    assert injected is True
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["termination_authority"] == "none"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


def test_queued_acquisition_observes_deferred_sigint_and_dequeues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    existing = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="existing-owner",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    progress_calls = 0

    def interrupt_queue(_message: str) -> None:
        nonlocal progress_calls
        progress_calls += 1
        signal.raise_signal(signal.SIGINT)

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.run_local_resource_command(
                profile="ci-final",
                phase="queued-ctrl-c",
                rootpath=Path.cwd(),
                timeout_seconds=10,
                command=[sys.executable, "-c", "pass"],
                progress=interrupt_queue,
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert time.monotonic() - started < 2
    assert progress_calls == 1
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "active"
    assert status["queue_total"] == 0
    existing.release(outcome="passed", postcondition="queued cancellation proved")


def test_queue_cleanup_failure_outweighs_deferred_sigint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    existing = coordinator.acquire_local_resources(
        profile="ci-final",
        phase="existing-owner",
        rootpath=tmp_path,
        timeout_seconds=1,
    )
    removal_attempted = False

    def fail_queue_removal(*, state_dir: Path, request_id: str) -> None:
        nonlocal removal_attempted
        del state_dir, request_id
        removal_attempted = True
        raise coordinator.LocalResourceStateError("injected queue-removal cleanup failure")

    def interrupt_queue(_message: str) -> None:
        signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(coordinator, "_remove_queue_request", fail_queue_removal)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(
            coordinator.LocalResourceStateError,
            match="queue-removal cleanup failure",
        ) as raised:
            coordinator.run_local_resource_command(
                profile="ci-final",
                phase="queued-cleanup-failure",
                rootpath=Path.cwd(),
                timeout_seconds=10,
                command=[sys.executable, "-c", "pass"],
                progress=interrupt_queue,
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert removal_attempted is True
    assert isinstance(raised.value.__context__, KeyboardInterrupt)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "active"
    assert status["queue_total"] == 1
    queue = status["queue"]
    assert isinstance(queue, list) and len(queue) == 1
    assert queue[0]["phase"] == "queued-cleanup-failure"
    existing.release(outcome="passed", postcondition="cleanup failure remained explicit")


def test_sigint_in_handled_exception_unwinds_before_would_block_sentinel() -> None:
    class WouldBlockSentinelError(RuntimeError):
        pass

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    guard = coordinator._MainThreadSigintGuard()
    guard.install()
    guard.enter_normal_mode_after_launch()
    try:
        with pytest.raises(KeyboardInterrupt):
            try:
                raise InterruptedError("ordinary retryable wait")
            except InterruptedError:
                signal.raise_signal(signal.SIGINT)
                raise WouldBlockSentinelError("wait loop incorrectly resumed") from None

        assert guard.mode == "suppress"
        assert guard.deferred_sigint is False
    finally:
        if guard.active:
            guard.deferred_sigint = False
            guard.restore_and_replay()
        signal.signal(signal.SIGINT, previous_handler)


def test_reentrant_repeated_ctrl_c_is_replayed_after_exact_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "must-not-run-after-repeated-ctrl-c"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    wait_for_completion = coordinator._wait_for_deferred_popen_completion
    capture_sigint = coordinator._MainThreadSigintGuard._capture
    captures = 0
    injected = False

    def reentrant_capture(
        deferrer: coordinator._MainThreadSigintGuard,
        signum: int,
        frame: FrameType | None,
    ) -> None:
        nonlocal captures
        captures += 1
        capture_sigint(deferrer, signum, frame)
        if captures == 1:
            # Deliver the second Ctrl-C before the first handler invocation has
            # returned.  Both must collapse into one safe replay request.
            signal.raise_signal(signal.SIGINT)

    def inject_during_completion(state: coordinator._DeferredPopenState) -> None:
        nonlocal injected
        if not injected:
            injected = True
            signal.raise_signal(signal.SIGINT)
        wait_for_completion(state)

    monkeypatch.setattr(
        coordinator._MainThreadSigintGuard,
        "_capture",
        reentrant_capture,
    )
    monkeypatch.setattr(
        coordinator,
        "_wait_for_deferred_popen_completion",
        inject_during_completion,
    )
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.run_local_resource_command(
                profile="ci-final",
                phase="repeated-ctrl-c",
                rootpath=Path.cwd(),
                timeout_seconds=1,
                command=[
                    sys.executable,
                    "-c",
                    (
                        "import os; from pathlib import Path; "
                        "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
                    ),
                ],
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert injected is True
    assert captures == 2
    assert marker.exists() is False
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


@pytest.mark.parametrize("custom_behavior", ["return", "raise"])
def test_custom_sigint_handler_is_restored_but_not_invoked_by_contained_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_behavior: str,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    child_pid_path = tmp_path / "custom-handler-child-pid"
    monkeypatch.setenv("DJANGO_RAY_TEST_CHILD_PID", str(child_pid_path))
    wait_for_owned_launcher_exit = coordinator._wait_for_owned_launcher_exit
    injected = False
    handler_calls: list[tuple[int, FrameType | None]] = []

    def custom_handler(signum: int, frame: FrameType | None) -> None:
        handler_calls.append((signum, frame))
        if custom_behavior == "raise":
            raise _CustomSignalError("custom SIGINT handler must not run")

    def inject_after_go(owned: coordinator._OwnedLocalCommand) -> None:
        nonlocal injected
        if injected:
            wait_for_owned_launcher_exit(owned)
            return
        deadline = time.monotonic() + 10
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        injected = True
        _invoke_installed_sigint_handler()
        raise AssertionError("normalized in-guard SIGINT must raise KeyboardInterrupt")

    monkeypatch.setattr(
        coordinator,
        "_wait_for_owned_launcher_exit",
        inject_after_go,
    )
    previous_handler = signal.signal(signal.SIGINT, custom_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.run_local_resource_command(
                profile="ci-final",
                phase="custom-sigint-handler",
                rootpath=Path.cwd(),
                timeout_seconds=1,
                command=[
                    sys.executable,
                    "-c",
                    (
                        "import os, time; from pathlib import Path; "
                        "Path(os.environ['DJANGO_RAY_TEST_CHILD_PID']).write_text("
                        "str(os.getpid())); time.sleep(10)"
                    ),
                ],
            )
        assert signal.getsignal(signal.SIGINT) is custom_handler
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert injected is True
    assert handler_calls == []
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    assert _pid_is_absent_or_zombie(child_pid)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_sigint_ignore_disposition_remains_ignored_inside_guard() -> None:
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    guard = coordinator._MainThreadSigintGuard()
    try:
        guard.install()
        guard.enter_normal_mode_after_launch()
        signal.raise_signal(signal.SIGINT)
        assert guard.deferred_sigint is False
        guard.enter_suppress_mode()
        guard.restore_and_replay()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        if guard.active:
            guard.deferred_sigint = False
            guard.restore_and_replay()
        signal.signal(signal.SIGINT, previous_handler)


def test_post_go_repeated_ctrl_c_is_deferred_through_recorded_tree_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    child_pid_path = tmp_path / "post-go-child-pid"
    monkeypatch.setenv("DJANGO_RAY_TEST_CHILD_PID", str(child_pid_path))
    wait_for_owned_launcher_exit = coordinator._wait_for_owned_launcher_exit
    terminate_owned_tree = coordinator._terminate_owned_tree
    capture_sigint = coordinator._MainThreadSigintGuard._capture
    post_go_injected = False
    cleanup_captures = 0
    cleanup_injections = 0
    transition_injected = False

    def count_cleanup_sigint(
        deferrer: coordinator._MainThreadSigintGuard,
        signum: int,
        frame: FrameType | None,
    ) -> None:
        nonlocal cleanup_captures, transition_injected
        cleanup_captures += 1
        try:
            capture_sigint(deferrer, signum, frame)
        except KeyboardInterrupt:
            if not transition_injected:
                transition_injected = True
                # The first handler has already committed suppress mode.  A
                # reentrant Ctrl-C must defer without replacing the unwind.
                _invoke_installed_sigint_handler()
            raise

    def interrupt_after_go(owned: coordinator._OwnedLocalCommand) -> None:
        nonlocal post_go_injected
        if post_go_injected:
            wait_for_owned_launcher_exit(owned)
            return
        deadline = time.monotonic() + 10
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        post_go_injected = True
        _invoke_installed_sigint_handler()
        raise AssertionError("the normal post-GO SIGINT handler must raise")

    def repeat_ctrl_c_before_real_cleanup(
        lease: coordinator.LocalResourceLease,
        owned: coordinator._OwnedLocalCommand,
    ) -> None:
        nonlocal cleanup_injections
        assert child_pid_path.exists()
        # Without the cleanup deferrer, the first signal consumes each of the
        # two bounded cleanup attempts before kill/reap can begin.  With it,
        # both requests collapse into a safe replay after exact absence.
        for _attempt in range(2):
            cleanup_injections += 1
            _invoke_installed_sigint_handler()
        terminate_owned_tree(lease, owned)

    monkeypatch.setattr(
        coordinator._MainThreadSigintGuard,
        "_capture",
        count_cleanup_sigint,
    )
    monkeypatch.setattr(
        coordinator,
        "_wait_for_owned_launcher_exit",
        interrupt_after_go,
    )
    monkeypatch.setattr(
        coordinator,
        "_terminate_owned_tree",
        repeat_ctrl_c_before_real_cleanup,
    )
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.run_local_resource_command(
                profile="ci-final",
                phase="post-go-repeated-ctrl-c",
                rootpath=Path.cwd(),
                timeout_seconds=1,
                command=[
                    sys.executable,
                    "-c",
                    (
                        "import os, time; from pathlib import Path; "
                        "Path(os.environ['DJANGO_RAY_TEST_CHILD_PID']).write_text("
                        "str(os.getpid())); time.sleep(10)"
                    ),
                ],
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    assert post_go_injected is True
    assert transition_injected is True
    assert cleanup_injections == 2
    assert cleanup_captures == 4
    assert _pid_is_absent_or_zombie(child_pid)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["termination_authority"] == "none"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "read-open",
        "read-transfer",
        "temporary-close",
        "validation-open",
        "validation-close",
    ],
)
def test_normal_clear_child_fd_sigint_returns_130_and_records_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    clear_child = coordinator.LocalResourceLease.clear_child
    write_state = coordinator._write_state_json
    open_descriptor = coordinator._open_existing_state_descriptor
    fdopen = os.fdopen
    close_descriptor = os.close
    in_clear_child = False
    in_target_write = False
    injected = False
    observed_descriptor: int | None = None
    observed_close_calls = 0

    def track_clear_child(lease: coordinator.LocalResourceLease) -> None:
        nonlocal in_clear_child
        in_clear_child = True
        try:
            clear_child(lease)
        finally:
            in_clear_child = False

    def track_target_write(path: Path, value: dict[str, object]) -> None:
        nonlocal in_target_write
        target = (
            in_clear_child
            and path.name == coordinator.ACTIVE_STATE_FILE
            and value.get("child") is None
        )
        if target:
            in_target_write = True
        try:
            write_state(path, value)
        finally:
            if target:
                in_target_write = False

    def interrupt_read_transfer(
        descriptor: int,
        mode: str = "r",
        buffering: int = -1,
    ) -> object:
        nonlocal injected, observed_descriptor
        handle = fdopen(descriptor, mode, buffering)
        if boundary == "read-transfer" and in_clear_child and mode == "rb" and not injected:
            observed_descriptor = descriptor
            injected = True
            signal.raise_signal(signal.SIGINT)
        return handle

    def interrupt_validation_open(path: Path) -> int:
        nonlocal injected, observed_descriptor
        descriptor = open_descriptor(path)
        if boundary == "read-open" and in_clear_child and not in_target_write and not injected:
            observed_descriptor = descriptor
            injected = True
            signal.raise_signal(signal.SIGINT)
        if in_target_write and boundary in {"validation-open", "validation-close"}:
            observed_descriptor = descriptor
            if boundary == "validation-open" and not injected:
                injected = True
                signal.raise_signal(signal.SIGINT)
        return descriptor

    def interrupt_committed_close(descriptor: int) -> None:
        nonlocal injected, observed_close_calls, observed_descriptor
        is_temporary_close = boundary == "temporary-close" and in_target_write and not injected
        is_validation_close = (
            boundary == "validation-close"
            and in_target_write
            and descriptor == observed_descriptor
            and not injected
        )
        is_observed_validation = (
            boundary == "validation-open" and in_target_write and descriptor == observed_descriptor
        )
        is_observed_read_open = (
            boundary == "read-open"
            and in_clear_child
            and descriptor == observed_descriptor
            and observed_close_calls == 0
        )
        if is_temporary_close:
            observed_descriptor = descriptor
        if (
            is_temporary_close
            or is_validation_close
            or is_observed_validation
            or is_observed_read_open
        ):
            observed_close_calls += 1
        close_descriptor(descriptor)
        if is_temporary_close or is_validation_close:
            injected = True
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(
        coordinator.LocalResourceLease,
        "clear_child",
        track_clear_child,
    )
    monkeypatch.setattr(coordinator, "_write_state_json", track_target_write)
    monkeypatch.setattr(os, "fdopen", interrupt_read_transfer)
    monkeypatch.setattr(
        coordinator,
        "_open_existing_state_descriptor",
        interrupt_validation_open,
    )
    monkeypatch.setattr(os, "close", interrupt_committed_close)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        exit_code = coordinator.main(
            [
                "run",
                "--profile",
                "ci-final",
                "--phase",
                f"{boundary}-ctrl-c",
                "--root",
                str(Path.cwd()),
                "--timeout",
                "1",
                "--",
                sys.executable,
                "-c",
                "pass",
            ]
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert exit_code == 130
    assert injected is True
    assert observed_descriptor is not None
    with pytest.raises(OSError) as raised:
        os.fstat(observed_descriptor)
    assert raised.value.errno == errno.EBADF
    if boundary in {
        "read-open",
        "temporary-close",
        "validation-open",
        "validation-close",
    }:
        assert observed_close_calls == 1
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert status["active"] is None
    assert status["termination_authority"] == "none"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


def test_cleanup_time_sigint_overrides_failed_outcome_and_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    child_pid_path = tmp_path / "cleanup-signal-child-pid"
    monkeypatch.setenv("DJANGO_RAY_TEST_CHILD_PID", str(child_pid_path))
    wait_for_owned_launcher_exit = coordinator._wait_for_owned_launcher_exit
    terminate_owned_tree = coordinator._terminate_owned_tree
    failure_injected = False
    cleanup_signal_injected = False

    def deterministic_error_after_go(owned: coordinator._OwnedLocalCommand) -> None:
        nonlocal failure_injected
        if failure_injected:
            wait_for_owned_launcher_exit(owned)
            return
        deadline = time.monotonic() + 10
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        failure_injected = True
        raise RuntimeError("deterministic post-GO failure")

    def interrupt_cleanup_then_terminate(
        lease: coordinator.LocalResourceLease,
        owned: coordinator._OwnedLocalCommand,
    ) -> None:
        nonlocal cleanup_signal_injected
        cleanup_signal_injected = True
        _invoke_installed_sigint_handler()
        terminate_owned_tree(lease, owned)

    monkeypatch.setattr(
        coordinator,
        "_wait_for_owned_launcher_exit",
        deterministic_error_after_go,
    )
    monkeypatch.setattr(
        coordinator,
        "_terminate_owned_tree",
        interrupt_cleanup_then_terminate,
    )
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        exit_code = coordinator.main(
            [
                "run",
                "--profile",
                "ci-final",
                "--phase",
                "cleanup-time-ctrl-c",
                "--root",
                str(Path.cwd()),
                "--timeout",
                "1",
                "--",
                sys.executable,
                "-c",
                (
                    "import os, time; from pathlib import Path; "
                    "Path(os.environ['DJANGO_RAY_TEST_CHILD_PID']).write_text("
                    "str(os.getpid())); time.sleep(10)"
                ),
            ]
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    captured = capsys.readouterr()
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    assert failure_injected is True
    assert cleanup_signal_injected is True
    assert exit_code == 130
    assert captured.err == ""
    assert _pid_is_absent_or_zombie(child_pid)
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"


def test_successful_run_inside_handled_exception_has_no_ambient_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)

    try:
        raise RuntimeError("ordinary handled outer exception")
    except RuntimeError:
        exit_code = coordinator.run_local_resource_command(
            profile="ci-final",
            phase="handled-outer-exception",
            rootpath=Path.cwd(),
            timeout_seconds=1,
            command=[sys.executable, "-c", "pass"],
        )

    assert exit_code == 0
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "passed"


@pytest.mark.parametrize(
    ("signal_boundary", "fail_first_finalization"),
    [
        ("lease-entry", False),
        ("ownership-release", False),
        ("ownership-release", True),
    ],
    ids=("lease-entry", "ownership-release", "ownership-release-rewrite-retry"),
)
def test_release_time_sigint_is_committed_before_waiters_can_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signal_boundary: str,
    fail_first_finalization: bool,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    lease_release = coordinator.LocalResourceLease.release
    ownership_release = coordinator.RealRayOwnershipLock.release
    write_state = coordinator._write_state_json
    injected = False
    finalization_attempts = 0

    def inject_once() -> None:
        nonlocal injected
        if not injected:
            injected = True
            signal.raise_signal(signal.SIGINT)

    if signal_boundary == "lease-entry":

        def release_with_signal(
            lease: coordinator.LocalResourceLease,
            *,
            outcome: str = "completed",
            postcondition: str | None = None,
            _completion_resolver: Callable[[bool], tuple[str, str | None]] | None = None,
        ) -> None:
            inject_once()
            lease_release(
                lease,
                outcome=outcome,
                postcondition=postcondition,
                _completion_resolver=_completion_resolver,
            )

        monkeypatch.setattr(coordinator.LocalResourceLease, "release", release_with_signal)
    else:

        def unlock_with_signal(lock: coordinator.RealRayOwnershipLock) -> None:
            inject_once()
            ownership_release(lock)

        monkeypatch.setattr(
            coordinator.RealRayOwnershipLock,
            "release",
            unlock_with_signal,
        )

    if fail_first_finalization:

        def fail_once_then_finalize(
            path: Path,
            value: dict[str, object],
        ) -> None:
            nonlocal finalization_attempts
            if (
                path.name == coordinator.LAST_COMPLETED_STATE_FILE
                and value.get("outcome") == "interrupted"
            ):
                finalization_attempts += 1
                if finalization_attempts == 1:
                    raise coordinator.LocalResourceStateError(
                        "injected transient completion finalization failure"
                    )
            write_state(path, value)

        monkeypatch.setattr(coordinator, "_write_state_json", fail_once_then_finalize)

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        exit_code = coordinator.main(
            [
                "run",
                "--profile",
                "ci-final",
                "--phase",
                f"{signal_boundary}-ctrl-c",
                "--root",
                str(Path.cwd()),
                "--timeout",
                "1",
                "--",
                sys.executable,
                "-c",
                "pass",
            ]
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    captured = capsys.readouterr()
    assert injected is True
    if fail_first_finalization:
        assert finalization_attempts == 2
    assert exit_code == 130
    assert captured.err == ""
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "interrupted"
    assert status["last_completed"]["postcondition"] == (
        "owned process tree absent before error propagation"
    )


def test_post_freeze_sigint_is_ignored_until_handler_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    write_state = coordinator._write_state_json
    injected = False

    def interrupt_final_write(path: Path, value: dict[str, object]) -> None:
        nonlocal injected
        if (
            not injected
            and path.name == coordinator.LAST_COMPLETED_STATE_FILE
            and value.get("outcome") == "passed"
        ):
            injected = True
            signal.raise_signal(signal.SIGINT)
        write_state(path, value)

    monkeypatch.setattr(coordinator, "_write_state_json", interrupt_final_write)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        exit_code = coordinator.main(
            [
                "run",
                "--profile",
                "ci-final",
                "--phase",
                "post-freeze-ctrl-c",
                "--root",
                str(Path.cwd()),
                "--timeout",
                "1",
                "--",
                sys.executable,
                "-c",
                "pass",
            ]
        )
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    captured = capsys.readouterr()
    assert injected is True
    assert exit_code == 0
    assert captured.err == ""
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "passed"


def test_run_cli_normalizes_process_creation_oserror_and_releases_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)

    def fail_process_creation(*_args: object, **_kwargs: object) -> object:
        raise OSError(errno.EIO, "unbounded-secret-process-error")

    monkeypatch.setattr(coordinator.subprocess, "Popen", fail_process_creation)

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "command",
            "--root",
            str(tmp_path),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "FAILED [local-resources]" in captured.err
    assert "could not be created safely" in captured.err
    assert "unbounded-secret-process-error" not in captured.err
    assert "Traceback" not in captured.err
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"


def test_go_pipe_oserror_is_bounded_and_cleanup_releases_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "go-oserror-marker"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    terminate = coordinator._terminate_owned_tree
    terminate_calls = 0

    def fail_go(_stream: object) -> None:
        raise BrokenPipeError("unbounded-secret-go-error")

    def fail_first_termination(
        lease: coordinator.LocalResourceLease,
        owned: coordinator._OwnedLocalCommand,
    ) -> None:
        nonlocal terminate_calls
        terminate_calls += 1
        if terminate_calls == 1:
            raise coordinator.LocalResourceStateError("injected first termination failure")
        terminate(lease, owned)

    monkeypatch.setattr(coordinator, "_release_launch_barrier", fail_go)
    monkeypatch.setattr(coordinator, "_terminate_owned_tree", fail_first_termination)

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "command",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
            ),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert terminate_calls == 2
    assert marker.exists() is False
    assert "contained command failed within its owned boundary" in captured.err
    assert "unbounded-secret-go-error" not in captured.err
    assert "Traceback" not in captured.err
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job retry ordering")
def test_resume_failure_retries_first_job_termination_and_never_releases_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "resume-failure-marker"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    terminate = coordinator._terminate_windows_local_job
    terminate_calls = 0

    def fail_resume(_process: subprocess.Popen[bytes]) -> None:
        raise BrokenPipeError("unbounded-secret-resume-error")

    def fail_first_termination(handle: int) -> None:
        nonlocal terminate_calls
        terminate_calls += 1
        if terminate_calls == 1:
            raise coordinator.LocalResourceStateError("injected first termination failure")
        terminate(handle)

    monkeypatch.setattr(coordinator, "_resume_windows_local_process", fail_resume)
    monkeypatch.setattr(
        coordinator,
        "_terminate_windows_local_job",
        fail_first_termination,
    )

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "command",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
            ),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert terminate_calls == 2
    assert marker.exists() is False
    assert "contained command failed within its owned boundary" in captured.err
    assert "unbounded-secret-resume-error" not in captured.err
    assert "Traceback" not in captured.err
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"
    assert isinstance(status["last_completed"], dict)
    assert status["last_completed"]["outcome"] == "failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job close ordering")
def test_go_failure_consumes_retained_close_error_before_original_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "go-failure-marker"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    close_job = coordinator._close_windows_local_job
    observed_handles: list[int] = []

    def fail_go(_stream: object) -> None:
        raise BrokenPipeError("unbounded-secret-go-error")

    def fail_first_close(handle: int) -> None:
        observed_handles.append(handle)
        if len(observed_handles) == 1:
            close_job(handle)
            raise coordinator.LocalResourceStateError(
                "async state error after retained close committed"
            )
        close_job(handle)

    monkeypatch.setattr(coordinator, "_release_launch_barrier", fail_go)
    monkeypatch.setattr(coordinator, "_close_windows_local_job", fail_first_close)

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "command",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('unsafe')"
            ),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert len(observed_handles) == 2
    assert observed_handles[1] != observed_handles[0]
    assert marker.exists() is False
    assert "contained command failed within its owned boundary" in captured.err
    assert "unbounded-secret-go-error" not in captured.err
    assert "Traceback" not in captured.err
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"


def test_launched_descendant_can_prove_inherited_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "inheritance-verified"
    monkeypatch.setenv("DJANGO_RAY_TEST_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    command = (
        "import os; from pathlib import Path; "
        "from scripts import local_resource_coordinator as c; "
        "c.DEFAULT_REAL_RAY_LOCK_PATH = Path(os.environ['DJANGO_RAY_TEST_LOCK_PATH']); "
        "lease = c.require_inherited_local_resources(profile='real-ray', rootpath=Path.cwd()); "
        "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text(str(lease.inherited))"
    )

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="command",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[sys.executable, "-c", command],
    )

    assert exit_code == 0
    assert marker.read_text(encoding="utf-8") == "True"
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"


def test_launched_descendant_inherits_unicode_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    state_dir = base_state_dir.with_name("state-\N{SNOWMAN}-\N{CJK UNIFIED IDEOGRAPH-5171}")
    monkeypatch.setattr(coordinator, "DEFAULT_LOCAL_RESOURCE_STATE_DIR", state_dir)
    marker = tmp_path / "unicode-inheritance-verified"
    monkeypatch.setenv("DJANGO_RAY_TEST_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))
    command = (
        "import os; from pathlib import Path; "
        "from scripts import local_resource_coordinator as c; "
        "c.DEFAULT_REAL_RAY_LOCK_PATH = Path(os.environ['DJANGO_RAY_TEST_LOCK_PATH']); "
        "lease = c.require_inherited_local_resources(profile='real-ray', rootpath=Path.cwd()); "
        "state = Path(os.environ[c.LOCAL_RESOURCE_STATE_DIR_ENV]); "
        "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text("
        "str(lease.inherited and state.name.startswith('state-')))"
    )

    exit_code = coordinator.run_local_resource_command(
        profile="ci-final",
        phase="unicode-command",
        rootpath=Path.cwd(),
        timeout_seconds=1,
        command=[sys.executable, "-c", command],
    )

    assert exit_code == 0
    assert marker.read_text(encoding="utf-8") == "True"
    assert state_dir.exists()
    status = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert status["state"] == "idle"


def test_root_process_exit_enforces_platform_tree_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    user_pid_path = tmp_path / "user-command.pid"
    worker_environment = os.environ.copy()
    worker_environment.update(
        {
            "TEST_STATE_DIR": str(state_dir),
            "TEST_LOCK_PATH": str(lock_path),
            "TEST_USER_PID_PATH": str(user_pid_path),
        }
    )
    worker_code = (
        "import os, sys; from pathlib import Path; "
        "from scripts import local_resource_coordinator as c; "
        "c.DEFAULT_LOCAL_RESOURCE_STATE_DIR = Path(os.environ['TEST_STATE_DIR']); "
        "c.DEFAULT_REAL_RAY_LOCK_PATH = Path(os.environ['TEST_LOCK_PATH']); "
        'command = "import os,time; from pathlib import Path; '
        "Path(os.environ['TEST_USER_PID_PATH']).write_text(str(os.getpid())); time.sleep(30)\"; "
        "raise SystemExit(c.run_local_resource_command(profile='ci-final', phase='crash-test', "
        "rootpath=Path.cwd(), timeout_seconds=5, command=[sys.executable, '-c', command]))"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        cwd=Path.cwd(),
        env=worker_environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        active: dict[str, object] | None = None
        while time.monotonic() < deadline:
            status = coordinator.read_local_resource_status(
                lock_path=lock_path,
                state_dir=state_dir,
            )
            if status.get("state") == "active" and user_pid_path.exists():
                candidate = status.get("active")
                if isinstance(candidate, dict):
                    active = candidate
                    break
            time.sleep(0.02)
        assert active is not None
        child = active.get("child")
        assert isinstance(child, dict)
        child_pid = child.get("pid")
        assert isinstance(child_pid, int)
        user_pid = int(user_pid_path.read_text(encoding="ascii"))
        assert coordinator._pid_presence(user_pid) == "present"

        worker.terminate()
        worker.wait(timeout=10)
        if os.name == "nt":
            deadline = time.monotonic() + 10
            while coordinator._pid_presence(user_pid) != "absent" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert coordinator._pid_presence(user_pid) == "absent"
        else:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                orphaned = coordinator.read_local_resource_status(
                    lock_path=lock_path,
                    state_dir=state_dir,
                )
                if orphaned.get("state") == "orphaned":
                    break
                time.sleep(0.02)
            assert orphaned["state"] == "orphaned"
            os.killpg(child_pid, signal.SIGKILL)
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=10)
        if os.name == "posix" and child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_direct_ctrl_c_keyboard_interrupt_cleans_owned_tree_and_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    launch = coordinator._launch_owned_command
    child_pid: int | None = None

    def interrupting_launch(
        *,
        lease: coordinator.LocalResourceLease,
        command: list[str],
        rootpath: Path,
        sigint_guard: coordinator._MainThreadSigintGuard,
    ) -> coordinator._OwnedLocalCommand:
        nonlocal child_pid
        owned = launch(
            lease=lease,
            command=command,
            rootpath=rootpath,
            sigint_guard=sigint_guard,
        )
        child_pid = owned.process.pid
        wait = owned.process.wait
        interrupted = False

        def ctrl_c_wait(timeout: float | None = None) -> int:
            nonlocal interrupted
            if timeout is None and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return wait(timeout=timeout)

        monkeypatch.setattr(owned.process, "wait", ctrl_c_wait)
        return owned

    monkeypatch.setattr(coordinator, "_launch_owned_command", interrupting_launch)

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "ctrl-c",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ]
    )

    assert exit_code == 130
    assert child_pid is not None
    assert coordinator._pid_presence(child_pid) == "absent"
    final = coordinator.read_local_resource_status(lock_path=lock_path, state_dir=state_dir)
    assert final["state"] == "idle"
    assert isinstance(final["last_completed"], dict)
    assert final["last_completed"]["outcome"] == "interrupted"


def test_registry_tickets_preserve_fifo_across_three_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    order_path = tmp_path / "acquisition-order.txt"
    release_path = tmp_path / "release-first"
    worker_code = (
        "import os, time; from pathlib import Path; "
        "from scripts import local_resource_coordinator as c; "
        "c.DEFAULT_LOCAL_RESOURCE_STATE_DIR = Path(os.environ['TEST_STATE_DIR']); "
        "c.DEFAULT_REAL_RAY_LOCK_PATH = Path(os.environ['TEST_LOCK_PATH']); "
        "label = os.environ['TEST_LABEL']; "
        "lease = c.acquire_local_resources(profile='ci-final', phase=label, "
        "rootpath=Path.cwd(), timeout_seconds=15, progress_interval_seconds=0.1); "
        "order = Path(os.environ['TEST_ORDER_PATH']); "
        "handle = order.open('a', encoding='ascii'); handle.write(label + '\\n'); "
        "handle.flush(); handle.close(); "
        "release = Path(os.environ['TEST_RELEASE_PATH']); "
        "deadline = time.monotonic() + 15; "
        "exec(\"while label == 'one' and not release.exists() and time.monotonic() < deadline:\\n"
        '    time.sleep(0.02)"); '
        "time.sleep(0.08); "
        "lease.release(outcome='passed', postcondition='worker complete')"
    )
    processes: list[subprocess.Popen[bytes]] = []

    def launch(label: str) -> subprocess.Popen[bytes]:
        environment = os.environ.copy()
        environment.update(
            {
                "TEST_STATE_DIR": str(state_dir),
                "TEST_LOCK_PATH": str(lock_path),
                "TEST_LABEL": label,
                "TEST_ORDER_PATH": str(order_path),
                "TEST_RELEASE_PATH": str(release_path),
                coordinator.LOCAL_RESOURCE_AGENT_ENV: label,
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            cwd=Path.cwd(),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        return process

    def await_status(predicate: Callable[[dict[str, object]], bool]) -> dict[str, object]:
        deadline = time.monotonic() + 10
        last: dict[str, object] = {}
        while time.monotonic() < deadline:
            last = coordinator.read_local_resource_status(
                lock_path=lock_path,
                state_dir=state_dir,
            )
            if predicate(last):
                return last
            time.sleep(0.02)
        pytest.fail(f"coordinator status did not reach expected FIFO state: {last}")

    def queue_phases(status: dict[str, object]) -> list[object]:
        queue = status.get("queue")
        if not isinstance(queue, list):
            return []
        return [item.get("phase") for item in queue if isinstance(item, dict)]

    try:
        first = launch("one")
        await_status(
            lambda status: (
                status.get("state") == "active"
                and isinstance(status.get("active"), dict)
                and status["active"].get("phase") == "one"
            )
        )
        second = launch("two")
        await_status(
            lambda status: status.get("queue_total") == 1 and queue_phases(status) == ["two"]
        )
        third = launch("three")
        queued = await_status(
            lambda status: (
                status.get("queue_total") == 2 and queue_phases(status) == ["two", "three"]
            )
        )
        assert queued["state"] == "active"
        release_path.write_text("go", encoding="ascii")
        for process in (first, second, third):
            _, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode("utf-8", errors="replace")[:2_000]
    finally:
        release_path.write_text("cleanup", encoding="ascii")
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    assert order_path.read_text(encoding="ascii").splitlines() == ["one", "two", "three"]


def test_run_cli_uses_the_contained_runner_without_exposing_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_isolated_coordinator_paths(tmp_path, monkeypatch)
    marker = tmp_path / "cli-run-marker"
    monkeypatch.setenv("DJANGO_RAY_TEST_MARKER", str(marker))

    exit_code = coordinator.main(
        [
            "run",
            "--profile",
            "ci-final",
            "--phase",
            "cli",
            "--root",
            str(Path.cwd()),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                "Path(os.environ['DJANGO_RAY_TEST_MARKER']).write_text('ok')"
            ),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert marker.read_text(encoding="utf-8") == "ok"
    assert "DJANGO_RAY_LOCAL_LEASE_TOKEN" not in captured.out + captured.err


def test_require_inherited_cli_is_read_only_and_fails_closed_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, lock_path = _use_isolated_coordinator_paths(tmp_path, monkeypatch)

    assert (
        coordinator.main(
            [
                "require-inherited",
                "--profile",
                "ci-final",
                "--root",
                str(tmp_path),
            ]
        )
        == 4
    )
    captured = capsys.readouterr()
    assert "FAILED [local-resources]" in captured.err
    assert state_dir.exists() is False
    assert lock_path.exists() is False


def test_cli_returns_distinct_unknown_status_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = coordinator._empty_status(
        state="unknown",
        safe_action="investigate",
        local_liveness="state-path-unsafe",
    )
    monkeypatch.setattr(coordinator, "read_local_resource_status", lambda: status)

    assert coordinator.main(["status", "--format", "json"]) == 3
    assert json.loads(capsys.readouterr().out) == status
