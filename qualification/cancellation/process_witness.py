"""Read only the owned Linux driver's bounded process identity; never signal it."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_BYTES = 4096


def bounded_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Witness must be a regular file")
        value = os.read(descriptor, MAX_BYTES + 1)
        if len(value) > MAX_BYTES:
            raise ValueError("Witness exceeds its byte budget")
        return value
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    uid: int
    pid_namespace: str


def read_process(pid, *, proc=Path("/proc")):
    if type(pid) is not int or not 0 < pid <= 4194304:
        raise ValueError("Invalid owned process identifier")
    root = proc / str(pid)
    value = bounded_bytes(root / "stat").decode("ascii")
    prefix, separator, tail = value.rpartition(") ")
    fields = tail.split()
    if not separator or not prefix.startswith(f"{pid} (") or len(fields) < 20:
        raise ValueError("Invalid process stat")
    state, start = fields[0], fields[19]
    if state not in set("RSDZTtXxKWPIN") or not start.isdecimal() or int(start) <= 0:
        raise ValueError("Invalid process state or start identity")
    uid_lines = [
        line.split()[1:]
        for line in bounded_bytes(root / "status").decode("ascii").splitlines()
        if line.startswith("Uid:")
    ]
    if (
        len(uid_lines) != 1
        or len(uid_lines[0]) != 4
        or any(not v.isdecimal() for v in uid_lines[0])
    ):
        raise ValueError("Invalid process user identity")
    uids = list(map(int, uid_lines[0]))
    if len(set(uids)) != 1:
        raise ValueError("Process user identities differ")
    namespace = os.readlink(root / "ns/pid")
    if not re.fullmatch(r"pid:\[[0-9]+\]", namespace):
        raise ValueError("Invalid process namespace")
    return ProcessIdentity(pid, int(start), uids[0], namespace), state


def write_start(path, *, nonce, case, execution):
    identity, _ = read_process(os.getpid())
    value = {"nonce": nonce, "case": case, "execution": execution, "process": asdict(identity)}
    pending = path.with_suffix(".pending")
    with pending.open("x") as stream:
        json.dump(value, stream, sort_keys=True)
    try:
        # Link only a complete marker, atomically and without replacing an earlier one.
        os.link(pending, path)
    finally:
        pending.unlink()


def bind_driver(path, *, nonce, case, execution, proc=Path("/proc"), observer_pid=None):
    value = json.loads(bounded_bytes(path))
    if (
        not isinstance(value, dict)
        or set(value) != {"nonce", "case", "execution", "process"}
        or value["nonce"] != nonce
        or value["case"] != case
        or value["execution"] != execution
        or not isinstance(value["process"], dict)
        or set(value["process"]) != {"pid", "start_ticks", "uid", "pid_namespace"}
    ):
        raise ValueError("Driver witness does not match the owned execution")
    identity = value["execution"]
    if (
        not isinstance(identity, list)
        or len(identity) != 4
        or any(type(identity[index]) is not int or identity[index] <= 0 for index in (0, 2, 3))
        or not isinstance(identity[1], str)
        or not 0 < len(identity[1]) <= 255
        or case not in {"control", "cancel"}
        or not re.fullmatch("[0-9a-f]{32}", nonce)
    ):
        raise ValueError("Invalid durable driver identity")
    record = value["process"]
    if any(type(record[key]) is not int for key in ("pid", "start_ticks", "uid")):
        raise ValueError("Driver identity must contain integer counters")
    expected = ProcessIdentity(**record)
    actual, state = read_process(expected.pid, proc=proc)
    observer, _ = read_process(os.getpid() if observer_pid is None else observer_pid, proc=proc)
    if (
        actual != expected
        or actual.uid != observer.uid
        or actual.pid_namespace != observer.pid_namespace
    ):
        raise ValueError("Driver is not in the owned process context")
    if actual.pid == observer.pid or state in {"Z", "X", "x"}:
        raise ValueError("Driver did not start independently")
    return actual


def driver_exited(identity, *, proc=Path("/proc")):
    try:
        current, state = read_process(identity.pid, proc=proc)
    except FileNotFoundError:
        return True
    if current.pid_namespace != identity.pid_namespace:
        raise ValueError("Process namespace changed during observation")
    if current.start_ticks != identity.start_ticks:
        return True  # PID reuse cannot identify the original process as alive.
    if current.uid != identity.uid:
        raise ValueError("Original process user identity changed")
    return state in {"Z", "X", "x"}
