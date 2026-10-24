"""Synchronous prefix measurements; never account for an unobserved timeout tail."""

from __future__ import annotations

import json
import os
import re
import stat
import time

MAX_BYTES = 512
MAX_VALUE = 99_999_999
MAX_WALL_MS = 10_000
CGROUP = "/sys/fs/cgroup"
COUNTERS = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")
# These are availability categories, not explanations of the readiness failure.
OBSERVED, UNSUPPORTED, UNAVAILABLE, RESET, SCOPE_CHANGED, OVERFLOW = range(6)


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def valid_sample(value):
    if value is None:
        return True
    if type(value) is not list or len(value) != 8:
        return False
    start, end, cpu, *cgroup, state = value
    return (
        _integer(start, MAX_WALL_MS)
        and _integer(end, MAX_WALL_MS)
        and start <= end
        and (cpu is None or _integer(cpu, MAX_VALUE))
        and _integer(state, OVERFLOW)
        and (
            all(_integer(item, MAX_VALUE) for item in cgroup)
            if state == OBSERVED
            else all(item is None for item in cgroup)
        )
    )


def valid_samples(values):
    """A lost baseline cannot be replaced by a new zero or an earlier sample."""
    if not values or len(values) > 4 or not all(valid_sample(value) for value in values):
        return False
    first = values[0]
    if first is not None and (
        first[0] != 0
        or first[2] not in (None, 0)
        or (first[7] == OBSERVED and first[3:7] != [0, 0, 0, 0])
    ):
        return False
    for before, after in zip(values, values[1:], strict=False):
        if before is None:
            if after is not None:
                return False
        elif after is not None:
            if after[0] < before[1]:
                return False
            if before[2] is None:
                if after[2] is not None:
                    return False
            elif after[2] is not None and after[2] < before[2]:
                return False
            if before[7] != OBSERVED:
                if after[7] != before[7]:
                    return False
            elif after[7] == OBSERVED and any(
                current < previous
                for previous, current in zip(before[3:7], after[3:7], strict=True)
            ):
                return False
    return True


def valid_record(value):
    keys = {
        "schema_version",
        "scope",
        "coverage",
        "terminal_accounting",
        "phase",
        "baseline",
        "sample",
        "received_ms",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["scope"] != "worker_readiness_resources"
        or value["coverage"] != "last_observed_prefix"
        or value["terminal_accounting"] is not False
        or type(value["phase"]) is not str
        or value["phase"]
        not in {"helper_entry", "django_setup", "django_setup_complete", "worker_ready"}
        or not valid_sample(value["sample"])
    ):
        return False
    baseline, received, sample = value["baseline"], value["received_ms"], value["sample"]
    if (
        type(received) is not list
        or len(received) != 2
        or not all(_integer(item, MAX_WALL_MS) for item in received)
        or received[0] > received[1]
    ):
        return False
    if value["phase"] == "helper_entry" and (
        received[0] != received[1]
        or not valid_samples([sample])
        or (sample is None and baseline is not None)
    ):
        return False
    if baseline is None:
        if sample is not None:
            return False
    elif (
        type(baseline) is not list
        or len(baseline) != 2
        or not all(_integer(item, MAX_WALL_MS) for item in baseline)
        or baseline[0] != 0
        or (
            sample is not None
            and (
                baseline != sample[:2]
                if value["phase"] == "helper_entry"
                else baseline[1] > sample[0]
            )
        )
    ):
        return False
    return (
        len((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()) <= MAX_BYTES
    )


def _read_fd(fd, limit):
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, limit + 1)
    if len(raw) > limit:
        raise ValueError("resource_bound")
    return raw


def _read_path(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        return _read_fd(fd, limit)
    finally:
        os.close(fd)


def _identity(info):
    return info.st_dev, info.st_ino


def _counter_values(raw):
    rows = raw.splitlines()
    if not 0 < len(rows) <= 32:
        raise ValueError("resource_stat")
    values = {}
    for row in rows:
        fields = row.split(b" ")
        if (
            len(fields) != 2
            or re.fullmatch(rb"[a-z_]+(?:\.[a-z_]+)*", fields[0]) is None
            or re.fullmatch(rb"(?:0|[1-9][0-9]{0,19})", fields[1]) is None
            or fields[0] in values
        ):
            raise ValueError("resource_stat")
        number = int(fields[1])
        if number > 2**64 - 1:
            raise ValueError("resource_stat")
        values[fields[0]] = number
    return [values[key.encode()] for key in COUNTERS]


class _Cgroup:
    """Only the fixed, verified v2 namespace root; never discover other groups."""

    def __init__(self):
        self.fds = []
        self.state = OBSERVED
        self.baseline = self.previous = None
        try:
            if _read_path("/proc/self/cgroup", 4096) != b"0::/\n":
                raise ValueError("resource_scope")
            mounts = _read_path("/proc/self/mountinfo", 65536).splitlines()
            selected = []
            for line in mounts:
                fields, separator, filesystem = line.partition(b" - ")
                fields = fields.split()
                if len(fields) >= 6 and fields[4] in {
                    (CGROUP + "/cpu.stat").encode(),
                    (CGROUP + "/cpu.max").encode(),
                }:
                    raise ValueError("resource_scope")
                if len(fields) >= 6 and fields[4] == CGROUP.encode():
                    selected.append((fields[3], separator, filesystem.split()[0]))
            if selected != [(b"/", b" - ", b"cgroup2")]:
                raise ValueError("resource_scope")
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            self.directory = os.open(CGROUP, flags | os.O_DIRECTORY)
            self.fds.append(self.directory)
            self.directory_identity = _identity(os.fstat(self.directory))
            self.files = {}
            for name in ("cpu.stat", "cpu.max"):
                fd = os.open(name, flags, dir_fd=self.directory)
                self.fds.append(fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_dev != self.directory_identity[0]:
                    raise ValueError("resource_scope")
                self.files[name] = fd, _identity(info)
            self.maximum = _read_fd(self.files["cpu.max"][0], 128)
            quota = self.maximum.split()
            if (
                len(quota) != 2
                or any(re.fullmatch(rb"[1-9][0-9]{0,8}", item) is None for item in quota)
                or int(quota[0]) * 4 != int(quota[1])
            ):
                raise ValueError("resource_scope")
        except (ValueError, IndexError):
            self.state = UNSUPPORTED
            self.close()
        except OSError:
            self.state = UNAVAILABLE
            self.close()

    def _same_scope(self):
        return (
            _read_path("/proc/self/cgroup", 4096) == b"0::/\n"
            and _identity(os.stat(CGROUP, follow_symlinks=False)) == self.directory_identity
            and all(
                _identity(os.stat(name, dir_fd=self.directory, follow_symlinks=False)) == identity
                for name, (_, identity) in self.files.items()
            )
            and _read_fd(self.files["cpu.max"][0], 128) == self.maximum
        )

    def sample(self):
        if self.state == OBSERVED:
            try:
                current = _counter_values(_read_fd(self.files["cpu.stat"][0], 4096))
                if not self._same_scope():
                    self.state = SCOPE_CHANGED
                elif self.previous is not None and any(
                    item < previous for item, previous in zip(current, self.previous, strict=True)
                ):
                    self.state = RESET
                else:
                    if self.baseline is None:
                        self.baseline = current
                    delta = [
                        item - baseline
                        for item, baseline in zip(current, self.baseline, strict=True)
                    ]
                    self.previous = current
                    if all(_integer(item, MAX_VALUE) for item in delta):
                        return [*delta, OBSERVED]
                    self.state = OVERFLOW
            except (OSError, ValueError, KeyError):
                self.state = UNAVAILABLE
        return [None, None, None, None, self.state]

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()


class Sampler:
    """Four caller-owned samples; no asynchronous observer or clock reset."""

    def __init__(self, *, clock=None, cpu=None, cgroup=None):
        self.clock = clock or time.monotonic_ns
        self.cpu = cpu or time.process_time_ns
        self.cgroup = cgroup
        self.wall_baseline = self.cpu_baseline = None
        self.last_wall = self.last_cpu = None
        self.wall_failed = self.cpu_failed = self.cgroup_failed = False
        self.count = 0

    def sample(self):
        if self.count >= 4 or self.wall_failed:
            return None
        self.count += 1
        try:
            start = self.clock()
            if (
                type(start) is not int
                or start < 0
                or (self.last_wall is not None and start < self.last_wall)
            ):
                raise ValueError("resource_clock")
            if self.wall_baseline is None:
                self.wall_baseline = start
            cpu_value = None
            if not self.cpu_failed:
                try:
                    current = self.cpu()
                    if (
                        type(current) is not int
                        or current < 0
                        or (self.last_cpu is not None and current < self.last_cpu)
                    ):
                        raise ValueError("resource_clock")
                    if self.cpu_baseline is None:
                        self.cpu_baseline = current
                    cpu_value = (current - self.cpu_baseline) // 1000
                    if not _integer(cpu_value, MAX_VALUE):
                        raise ValueError("resource_clock")
                    self.last_cpu = current
                except (OSError, ValueError, OverflowError):
                    self.cpu_failed = True
                    cpu_value = None
            group = [None, None, None, None, UNAVAILABLE]
            if not self.cgroup_failed:
                try:
                    if self.cgroup is None:
                        self.cgroup = _Cgroup()
                    group = self.cgroup.sample()
                except (OSError, ValueError):
                    self.cgroup_failed = True
            end = self.clock()
            if type(end) is not int or end < start:
                raise ValueError("resource_clock")
            value = [
                (start - self.wall_baseline) // 1_000_000,
                (end - self.wall_baseline) // 1_000_000,
                cpu_value,
                *group,
            ]
            if not valid_sample(value):
                raise ValueError("resource_clock")
            self.last_wall = end
            return value
        except (OSError, ValueError, OverflowError):
            self.wall_failed = True
            return None

    def close(self):
        if self.cgroup is not None:
            self.cgroup.close()
