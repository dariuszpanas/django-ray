"""Resource accounting contracts with fake clocks, counters and descriptors only."""

import json
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.application import worker_readiness_diagnostics as readiness
from qualification.application import worker_resource_diagnostics as resources


def sample(start=0, end=1, cpu=0, counters=(0, 0, 0, 0), state=0):
    return [start, end, cpu, *counters, state]


def record():
    return {
        "schema_version": 1,
        "scope": "worker_readiness_resources",
        "coverage": "last_observed_prefix",
        "terminal_accounting": False,
        "phase": "django_setup",
        "baseline": [0, 1],
        "sample": sample(2, 3, 100, (200, 1, 1, 300)),
        "received_ms": [5, 5],
    }


@pytest.mark.parametrize("index", range(8))
@pytest.mark.parametrize("bad", [True, -1, 100_000_000, "private", {}, []])
def test_sample_rejects_wrong_domains(index, bad):
    value = sample()
    value[index] = bad
    assert not resources.valid_sample(value)


@pytest.mark.parametrize("state", range(1, 6))
def test_unavailable_counters_are_explicitly_null(state):
    assert resources.valid_sample(sample(cpu=None, counters=(None,) * 4, state=state))
    assert not resources.valid_sample(sample(state=state))
    assert not resources.valid_sample(sample(counters=(None,) * 4))


def test_stream_has_one_baseline_and_never_relabels_old_or_reset_values():
    first = sample()
    second = sample(2, 3, 50, (20, 1, 1, 10))
    unknown = sample(4, 5, None, (None,) * 4, resources.RESET)
    assert resources.valid_samples([first, second, unknown, None])
    assert resources.valid_samples([None] * 4)
    for values in (
        [],
        [first] * 5,
        [sample(cpu=1)],
        [sample(counters=(1, 0, 0, 0))],
        [sample(start=1)],
        [first, sample(0, 1)],
        [None, first],
        [first, second, sample(4, 5, 49)],
        [first, second, sample(4, 5, 60, (19, 1, 1, 10))],
        [first, unknown, sample(6, 7, 60)],
        [first, unknown, sample(6, 7, None, (None,) * 4, resources.UNAVAILABLE)],
    ):
        assert not resources.valid_samples(values)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", True),
        ("terminal_accounting", True),
        ("scope", "private"),
        ("coverage", "complete"),
        ("phase", {}),
        ("baseline", [1, 2]),
        ("baseline", [0, 4]),
        ("received_ms", [6, 5]),
        ("received_ms", [True, 5]),
        ("received_ms", [0, 10001]),
        ("sample", [0]),
    ],
)
def test_record_rejects_forged_scope_times_and_domains(key, value):
    payload = record()
    payload[key] = value
    assert not resources.valid_record(payload)


def test_first_checkpoint_is_a_zero_baseline_with_one_receive_time():
    payload = record()
    payload.update(phase="helper_entry", sample=sample())
    assert resources.valid_record(payload)
    for changes in (
        {"received_ms": [5, 6]},
        {"sample": sample(cpu=1)},
        {"sample": sample(counters=(1, 0, 0, 0))},
        {"sample": None},
    ):
        assert not resources.valid_record({**payload, **changes})
    assert resources.valid_record({**payload, "baseline": None, "sample": None})


def test_wire_and_sidecar_fit_existing_512_byte_bounds():
    maximum = sample(10000, 10000, resources.MAX_VALUE, (resources.MAX_VALUE,) * 4)
    frames = [readiness.progress_frame(phase, maximum) for phase in readiness.CHECKPOINTS]
    assert sum(map(len, frames)) + max(map(len, readiness.REPORTS)) <= 512
    value = record()
    value.update(phase="django_setup_complete", sample=maximum, received_ms=[10000, 10000])
    assert resources.valid_record(value)
    assert len((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()) <= 512


def sampler(clock, cpu, group=None):
    group = group or SimpleNamespace(sample=lambda: [0, 0, 0, 0, 0], close=Mock())
    return resources.Sampler(clock=iter(clock).__next__, cpu=iter(cpu).__next__, cgroup=group)


def test_sampler_reports_read_brackets_and_immutable_cpu_baseline():
    group = SimpleNamespace(
        sample=Mock(side_effect=[[0, 0, 0, 0, 0], [20, 1, 1, 10, 0]]), close=Mock()
    )
    collector = sampler([10_000_000, 11_000_000, 12_000_000, 14_000_000], [500_000, 550_000], group)
    assert collector.sample() == sample()
    assert collector.sample() == sample(2, 4, 50, (20, 1, 1, 10))
    collector.close()
    group.close.assert_called_once()


@pytest.mark.parametrize("bad", [True, -1, 0, (resources.MAX_VALUE + 1) * 1000 + 1000])
def test_cpu_failure_keeps_wall_and_cgroup_and_never_rebaselines(bad):
    collector = sampler(range(0, 6_000_000, 1_000_000), [1000, bad])
    assert collector.sample() == sample()
    assert collector.sample() == sample(2, 3, None)
    assert collector.sample() == sample(4, 5, None)
    assert collector.wall_failed is False


def test_invalid_cpu_baseline_is_unknown_without_retry():
    collector = sampler([0, 1_000_000, 2_000_000, 3_000_000], [False])
    assert collector.sample() == sample(cpu=None)
    assert collector.sample() == sample(2, 3, None)


@pytest.mark.parametrize("clock", [[True], [-1], [0, -1], [0, 10_001_000_000], [0, 1, 0]])
def test_wall_failure_is_permanent_and_never_rebases(clock):
    collector = sampler(clock, [0, 0])
    first = collector.sample()
    if first is not None:
        assert collector.sample() is None
    assert collector.sample() is None
    assert collector.wall_failed


def test_sampler_has_only_four_caller_owned_samples():
    collector = sampler(range(8), [0] * 4)
    assert all(collector.sample() is not None for _ in range(4))
    assert collector.sample() is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"usage_usec 01",
        b"usage_usec -1",
        b"usage_usec 1.5",
        b"usage_usec NaN",
        b"usage_usec  1",
        b"usage_usec 18446744073709551616",
        b"usage_usec 1\nusage_usec 2",
    ],
)
def test_counter_parser_rejects_malformed_values(raw):
    with pytest.raises((ValueError, KeyError)):
        resources._counter_values(raw)


class FakeCgroup:
    def __init__(self, monkeypatch):
        self.membership = b"0::/\n"
        self.mounts = b"1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        self.maximum = b"25000 100000\n"
        self.counters = [100, 10, 3, 20]
        self.extra_counters = b""
        self.info = {
            fd: SimpleNamespace(st_dev=1, st_ino=fd, st_mode=stat.S_IFREG) for fd in (10, 11, 12)
        }
        self.calls = []
        self.closed = []
        fake_os = SimpleNamespace(
            O_RDONLY=0,
            O_CLOEXEC=1,
            O_NOFOLLOW=2,
            O_DIRECTORY=4,
            open=self.open,
            fstat=lambda fd: self.info[fd],
            stat=self.stat,
            close=self.closed.append,
        )
        monkeypatch.setattr(resources, "os", fake_os)
        monkeypatch.setattr(resources, "_read_path", self.read_path)
        monkeypatch.setattr(resources, "_read_fd", self.read_fd)

    def open(self, path, flags, **kwargs):
        self.calls.append((path, flags, kwargs))
        return {resources.CGROUP: 10, "cpu.stat": 11, "cpu.max": 12}[path]

    def stat(self, path, **kwargs):
        return self.info[{resources.CGROUP: 10, "cpu.stat": 11, "cpu.max": 12}[path]]

    def read_path(self, path, limit):
        assert (path, limit) in {("/proc/self/cgroup", 4096), ("/proc/self/mountinfo", 65536)}
        return self.membership if path.endswith("cgroup") else self.mounts

    def read_fd(self, fd, limit):
        assert (fd, limit) in {(11, 4096), (12, 128)}
        if fd == 12:
            return self.maximum
        return (
            b"".join(
                f"{name} {value}\n".encode()
                for name, value in zip(resources.COUNTERS, self.counters, strict=True)
            )
            + self.extra_counters
        )


def test_cgroup_reads_only_fixed_files_and_keeps_one_baseline(monkeypatch):
    fixture = FakeCgroup(monkeypatch)
    group = resources._Cgroup()
    assert group.sample() == [0, 0, 0, 0, 0]
    fixture.counters = [130, 12, 4, 60]
    assert group.sample() == [30, 2, 1, 40, 0]
    assert fixture.calls == [
        (resources.CGROUP, 7, {}),
        ("cpu.stat", 3, {"dir_fd": 10}),
        ("cpu.max", 3, {"dir_fd": 10}),
    ]
    group.close()
    group.close()
    assert fixture.closed == [10, 11, 12]


@pytest.mark.parametrize(
    "change", ["membership", "mount", "overmount", "foreign_device", "maximum"]
)
def test_unsupported_scope_never_claims_container_counters(monkeypatch, change):
    fixture = FakeCgroup(monkeypatch)
    if change == "membership":
        fixture.membership = b"0::/other\n"
    elif change == "mount":
        fixture.mounts *= 2
    elif change == "overmount":
        fixture.mounts += b"2 1 0:1 / /sys/fs/cgroup/cpu.stat rw - cgroup2 cgroup rw\n"
    elif change == "foreign_device":
        fixture.info[11].st_dev = 2
    else:
        fixture.maximum = b"max 100000\n"
    group = resources._Cgroup()
    assert group.sample() == [None] * 4 + [resources.UNSUPPORTED]
    assert group.fds == []


@pytest.mark.parametrize(
    "change,state",
    [
        ("membership", resources.SCOPE_CHANGED),
        ("inode", resources.SCOPE_CHANGED),
        ("maximum", resources.SCOPE_CHANGED),
        ("reset", resources.RESET),
        ("overflow", resources.OVERFLOW),
        ("parse", resources.UNAVAILABLE),
    ],
)
def test_scope_loss_reset_and_overflow_latch_without_a_new_baseline(monkeypatch, change, state):
    fixture = FakeCgroup(monkeypatch)
    group = resources._Cgroup()
    assert group.sample() == [0, 0, 0, 0, 0]
    if change == "membership":
        fixture.membership = b"0::/other\n"
    elif change == "inode":
        fixture.info[11].st_ino = 21
    elif change == "maximum":
        fixture.maximum = b"50000 100000\n"
    elif change == "reset":
        fixture.counters[0] = 99
    elif change == "overflow":
        fixture.counters[0] += resources.MAX_VALUE + 1
    else:
        fixture.counters[0] = "private"
    assert group.sample() == [None] * 4 + [state]
    fixture.membership = b"0::/\n"
    fixture.info[11].st_ino = 11
    fixture.maximum = b"25000 100000\n"
    fixture.counters = [100, 10, 3, 20]
    assert group.sample() == [None] * 4 + [state]
    assert group.baseline == [100, 10, 3, 20]


def test_initial_counter_failure_cannot_later_become_a_zero_baseline(monkeypatch):
    fixture = FakeCgroup(monkeypatch)
    fixture.counters = ["private", 0, 0, 0]
    group = resources._Cgroup()
    assert group.sample() == [None] * 4 + [resources.UNAVAILABLE]
    fixture.counters = [100, 10, 3, 20]
    assert group.sample() == [None] * 4 + [resources.UNAVAILABLE]
    assert group.baseline is None


def test_counter_parser_allows_bounded_extra_kernel_counters():
    raw = b"usage_usec 10\nnr_periods 3\nnr_throttled 2\nthrottled_usec 4\nuser_usec 6\nsystem_usec 4\n"
    assert resources._counter_values(raw) == [10, 3, 2, 4]


def test_cgroup_dotted_kernel_counter_preserves_observation_and_baseline(monkeypatch):
    fixture = FakeCgroup(monkeypatch)
    # Linux v6.17 kernel/cgroup/rstat.c emits this field with CONFIG_SCHED_CORE.
    fixture.extra_counters = b"user_usec 60\nsystem_usec 40\ncore_sched.force_idle_usec 7\n"
    group = resources._Cgroup()
    assert group.sample() == [0, 0, 0, 0, resources.OBSERVED]
    fixture.counters = [130, 12, 4, 60]
    fixture.extra_counters = b"user_usec 80\nsystem_usec 50\ncore_sched.force_idle_usec 9\n"
    assert group.sample() == [30, 2, 1, 40, resources.OBSERVED]
    assert group.baseline == [100, 10, 3, 20]
    group.close()
    assert fixture.closed == [10, 11, 12]


@pytest.mark.parametrize(
    "extra",
    [
        b".force_idle_usec 1\n",
        b"core_sched. 1\n",
        b"core_sched..force_idle_usec 1\n",
        b"core_sched.force-idle_usec 1\n",
        b"core_sched.force_idle_usec 01\n",
        b"core_sched.force_idle_usec -1\n",
        b"core_sched.force_idle_usec 18446744073709551616\n",
        b"core_sched.force_idle_usec 1\ncore_sched.force_idle_usec 2\n",
    ],
)
def test_dotted_extra_counter_errors_latch_unavailable(monkeypatch, extra):
    fixture = FakeCgroup(monkeypatch)
    fixture.extra_counters = extra
    group = resources._Cgroup()
    assert group.sample() == [None] * 4 + [resources.UNAVAILABLE]
    fixture.extra_counters = b"core_sched.force_idle_usec 0\n"
    assert group.sample() == [None] * 4 + [resources.UNAVAILABLE]
    assert group.baseline is None
    group.close()


@pytest.mark.parametrize("oversized", [False, True])
def test_fixed_file_read_is_bounded_and_always_closes_its_descriptor(monkeypatch, oversized):
    events = []
    raw = b"x" * (5 if oversized else 4)
    monkeypatch.setattr(
        resources,
        "os",
        SimpleNamespace(
            O_RDONLY=0,
            O_CLOEXEC=1,
            O_NOFOLLOW=2,
            SEEK_SET=0,
            open=lambda path, flags: (events.append((path, flags)), 9)[1],
            lseek=lambda fd, offset, whence: events.append((fd, offset, whence)),
            read=lambda fd, size: (events.append((fd, size)), raw)[1],
            close=lambda fd: events.append(("close", fd)),
        ),
    )
    if oversized:
        with pytest.raises(ValueError, match="resource_bound"):
            resources._read_path("/proc/self/cgroup", 4)
    else:
        assert resources._read_path("/proc/self/cgroup", 4) == raw
    assert events == [("/proc/self/cgroup", 3), (9, 0, 0), (9, 5), ("close", 9)]


def test_cgroup_open_failure_closes_owned_descriptors_and_remains_unknown(monkeypatch):
    fixture = FakeCgroup(monkeypatch)
    original_open = fixture.open

    def fail(path, flags, **kwargs):
        if path == "cpu.max":
            raise OSError("private")
        return original_open(path, flags, **kwargs)

    monkeypatch.setattr(resources.os, "open", fail)
    group = resources._Cgroup()
    assert fixture.closed == [10, 11]
    assert group.sample() == [None] * 4 + [resources.UNAVAILABLE]
    assert group.baseline is None
