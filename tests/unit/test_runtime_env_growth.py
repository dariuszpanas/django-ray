"""Resource-free checks for the fixed RuntimeEnv measurement and refusal boundaries."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import benchmark_runtime_env_growth as host
from scripts.runtime_env_growth import driver, evidence, identity, report, supervisor


def fake_upload(monkeypatch, tmp_path):
    """Model the real mutation/cache seam, while running the actual observer/preparer."""
    package = types.ModuleType("ray._private.runtime_env.packaging")
    working = types.ModuleType("ray._private.runtime_env.working_dir")
    container = types.ModuleType("ray._private.runtime_env")
    container.packaging = package
    container.working_dir = working
    cache = set()
    inputs = []

    def create(source, target, **kwargs):
        with zipfile.ZipFile(target, "w") as archive:
            archive.write(Path(source) / "app_value.txt", "app_value.txt")

    def upload(uri, scratch, source, **kwargs):
        if uri in cache:
            return False
        target = Path(scratch) / "actual.zip"
        package.create_package(source, target)
        target.unlink()
        cache.add(uri)
        return True

    def prepare(spec, *, scratch_dir, include_gitignore):
        inputs.append(copy.deepcopy(spec))
        if spec["working_dir"].startswith("gcs://"):
            return spec
        uri = "gcs://_ray_pkg_" + evidence.fixture_identity(Path(spec["working_dir"])) + ".zip"
        working.upload_package_if_needed(uri, scratch_dir, spec["working_dir"])
        spec["working_dir"] = uri
        return spec

    package.create_package = create
    working.upload_package_if_needed = upload
    working.upload_working_dir_if_needed = prepare
    working.cache = cache
    monkeypatch.setitem(sys.modules, "ray._private.runtime_env", container)
    monkeypatch.setitem(sys.modules, "ray._private.runtime_env.packaging", package)
    monkeypatch.setitem(sys.modules, "ray._private.runtime_env.working_dir", working)
    source = tmp_path / "source"
    source.mkdir()
    (source / "app_value.txt").write_text("A")
    return source, inputs, working


def test_repackage_uses_fresh_source_and_cache_hit_has_no_emitted_archive(monkeypatch, tmp_path):
    source, inputs, _ = fake_upload(monkeypatch, tmp_path)
    first, second = [], []
    result = driver.prepare("raw", source, first.append)
    again = driver.prepare("raw", source, second.append)
    assert inputs == [{"working_dir": str(source), "excludes": evidence.EXCLUDES}] * 2
    assert result == again
    assert first[0]["operation"] == "uploaded"
    assert len(first[0]["archives"]) == 1
    assert first[0]["archives"][0]["entries"] == 1
    assert second[0]["operation"] == "cache_hit"
    assert second[0]["archives"] == []


def test_changed_source_changes_package_identity(monkeypatch, tmp_path):
    source, _, _ = fake_upload(monkeypatch, tmp_path)
    first = driver.prepare("raw", source, lambda _value: None)
    (source / "app_value.txt").write_text("B")
    second = driver.prepare("raw", source, lambda _value: None)
    assert first["working_dir"] != second["working_dir"]


def test_failed_upload_records_failure_and_restores_observers(monkeypatch, tmp_path):
    source, _, working = fake_upload(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("private diagnostic must not be exported")

    working.upload_package_if_needed = fail
    records = []
    with pytest.raises(RuntimeError):
        driver.prepare("raw", source, records.append)
    assert records == [{"archives": [], "operation": "failed"}]
    assert working.upload_package_if_needed is fail


@pytest.mark.parametrize(
    "key",
    [
        "RAY_ADDRESS",
        "RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES",
        "RAY_RUNTIME_ENV_IGNORE_GITIGNORE",
    ],
)
def test_inherited_packaging_override_refuses(monkeypatch, key):
    monkeypatch.setenv(key, "private value")
    with pytest.raises(evidence.GrowthError, match="^configuration$"):
        driver.frozen_settings(evidence.DEFAULT_EXCLUDES)


def test_frozen_settings_refuse_a_different_upstream_default(monkeypatch):
    for key in (
        "RAY_ADDRESS",
        "RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES",
        "RAY_RUNTIME_ENV_IGNORE_GITIGNORE",
    ):
        monkeypatch.delenv(key, raising=False)
    assert len(driver.frozen_settings(evidence.DEFAULT_EXCLUDES)) == 64
    with pytest.raises(evidence.GrowthError, match="^configuration$"):
        driver.frozen_settings(["changed"])


def test_inventory_roles_and_partial_entry_byte_evidence(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app").write_bytes(b"12345")
    extracted = tmp_path / "session_1/runtime_resources/working_dir_files/pkg"
    extracted.mkdir(parents=True)
    (extracted / "app").write_bytes(b"12345")
    complete = evidence.scan_tree(tmp_path)
    assert complete["status"] == "complete"
    assert complete["roles"]["source"]["bytes"] == 5
    assert complete["roles"]["ray_extracted"]["bytes"] == 5
    assert evidence.scan_tree(tmp_path, max_entries=1)["status"] == "entry_limit"
    partial = evidence.scan_tree(tmp_path, max_bytes=2)
    assert partial["status"] == "byte_limit"
    assert partial["bytes"] == 5
    assert evidence.scan_tree(tmp_path, seconds=0)["status"] == "scan_timeout"


def test_inventory_does_not_export_filename(tmp_path):
    (tmp_path / "token-private-customer-name").write_bytes(b"content")
    report = evidence.encode(evidence.scan_tree(tmp_path))
    assert b"private" not in report and b"content" not in report


def test_actual_archive_evidence_differs_from_uncompressed_size(tmp_path):
    target = tmp_path / "actual.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("private-path", b"a" * 10000)
    record = evidence.archive_record(target)
    assert record["uncompressed_bytes"] == 10000
    assert record["compressed_bytes"] == target.stat().st_size
    assert record["sha256"] == evidence.digest(target.read_bytes())
    assert "private" not in json.dumps(record)


def test_archive_uncompressed_budget_refuses_before_reading_members(tmp_path):
    target = tmp_path / "actual.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data", b"x" * (16 * evidence.MIB + 1))
    with pytest.raises(evidence.GrowthError, match="^archive_limit$"):
        evidence.archive_record(target)


def test_generated_source_remains_small_and_has_positive_negative_controls(tmp_path):
    a, b = evidence.make_fixture(tmp_path / "source")
    assert (a / "app_value.txt").read_text() == "A"
    assert (b / "app_value.txt").read_text() == "B"
    assert all((a / name / "excluded.bin").is_file() for name in evidence.CANARIES)
    assert (a / "included.txt").read_text() == "included"
    assert evidence.fixture_identity(a) != evidence.fixture_identity(b)
    assert evidence.scan_tree(tmp_path)["bytes"] < 32 * evidence.MIB


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -1,
        "secret-token",
        {"password": "x"},
        {"reason": ["secret"]},
        [0] * 33,
    ],
)
def test_event_sanitizer_rejects_nonfinite_or_unknown_data(value):
    with pytest.raises(evidence.GrowthError, match="^output_limit$"):
        evidence.validate_value(value)


def test_output_limit_cannot_be_silent_truncation():
    with pytest.raises(evidence.GrowthError, match="^output_limit$"):
        evidence.encode({"value": "x" * 100}, limit=10)


@pytest.mark.parametrize(
    "payload",
    [b'{"kind":"done","kind":"failure"}', b'{"value":NaN}', b'{"value":Infinity}', b"not-json"],
)
def test_decoder_refuses_duplicate_or_malformed_finite_evidence(payload):
    with pytest.raises(evidence.GrowthError, match="^report_invalid$"):
        evidence.decode(payload)


@pytest.mark.parametrize("field", ["session", "index"])
def test_phase_coordinates_require_integers_not_booleans(field):
    clock = supervisor.SessionClock(total_deadline=1000)
    clock.begin(0)
    event = {"kind": "phase", "session": 1, "index": 0, "phase": "startup"}
    event[field] = bool(event[field])
    with pytest.raises(evidence.GrowthError, match="^protocol$"):
        clock.accept(event, 1)


def test_session_deadline_cannot_be_extended_by_phases_or_replayed_events():
    clock = supervisor.SessionClock(total_deadline=1000)
    clock.begin(0)
    clock.accept({"kind": "phase", "session": 1, "index": 0, "phase": "startup"}, 1)
    assert clock.stage_deadline == 60
    with pytest.raises(evidence.GrowthError, match="^protocol$"):
        clock.accept({"kind": "phase", "session": 1, "index": 0, "phase": "startup"}, 2)
    assert clock.deadline == 180
    with pytest.raises(evidence.GrowthError, match="^deadline$"):
        clock.check(181)


def test_total_deadline_caps_new_session():
    clock = supervisor.SessionClock(total_deadline=10)
    clock.begin(9)
    assert clock.deadline == clock.stage_deadline == 10
    with pytest.raises(evidence.GrowthError, match="^deadline$"):
        clock.check(10)


@pytest.mark.parametrize("boundary", ["quiescence", "snapshot"])
def test_settling_cannot_renew_expired_session(monkeypatch, boundary):
    now = [9.5]
    clock = supervisor.SessionClock(total_deadline=10)
    clock.begin(0)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(supervisor.time, "sleep", lambda duration: None)

    def quiet(pid):
        if boundary == "quiescence":
            now[0] = 10.1
        return True

    def snapshot():
        if boundary == "snapshot":
            now[0] = 10.1
        return {}

    monkeypatch.setattr(supervisor, "quiescent", quiet)
    monkeypatch.setattr(supervisor, "boundary_snapshot", snapshot)
    with pytest.raises(evidence.GrowthError, match="^deadline$"):
        supervisor.settle_session(123, {}, clock)
    assert clock.session == 1 and clock.deadline == 10


def test_driver_exit_is_capped_and_rechecked_against_original_session(monkeypatch):
    now = [9.5]
    clock = supervisor.SessionClock(total_deadline=10)
    clock.begin(0)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now[0])

    def wait(*, timeout):
        assert timeout == 0.5
        now[0] = 10.1

    with pytest.raises(evidence.GrowthError, match="^deadline$"):
        supervisor.wait_driver_exit(SimpleNamespace(wait=wait), clock)


def test_owned_root_refuses_active_or_replaced_identity(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    owner = evidence.OwnedRoot.capture(root)
    assert owner.remove(quiescent=False) is False
    root.rename(tmp_path / "original")
    root.mkdir()
    (root / "foreign").write_text("keep")
    assert owner.remove(quiescent=True) is False
    assert (root / "foreign").read_text() == "keep"


def test_non_linux_rejection_precedes_host_or_container_work(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["benchmark_runtime_env_growth.py", "--container"])
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(host, "host", lambda: pytest.fail("no host action"))
    monkeypatch.setattr(supervisor, "run_container", lambda: pytest.fail("no container action"))
    with pytest.raises(SystemExit, match="requires Linux"):
        host.main()


def test_effective_admission_rejects_unlimited_cgroup(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: "max")
    with pytest.raises(evidence.GrowthError, match="^admission$"):
        evidence.admission()


def test_effective_admission_requires_tmpfs_capacity_and_inodes(monkeypatch):
    values = {
        "memory.max": str(evidence.MEMORY_BYTES),
        "pids.max": "256",
        "cpu.max": "200000 100000",
    }
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs: values[path.name])
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: io.BytesIO(b"1 2 0:1 / /scratch rw - tmpfs tmpfs rw\n"),
    )
    good = SimpleNamespace(f_blocks=evidence.SCRATCH_BYTES // 4096, f_frsize=4096, f_files=50000)
    shm = SimpleNamespace(f_blocks=evidence.SHM_BYTES // 4096, f_frsize=4096, f_files=50000)
    monkeypatch.setattr(
        evidence.os, "statvfs", lambda path: shm if path == "/dev/shm" else good, raising=False
    )
    assert evidence.admission()["pids"] == 256
    good.f_files = 50001
    with pytest.raises(evidence.GrowthError, match="^admission$"):
        evidence.admission()


def test_declared_complete_report_cannot_ignore_failed_cleanup():
    report = {
        "schema_version": 1,
        "status": "complete",
        "admission": {
            "memory_bytes": 1,
            "pids": 1,
            "cpu_quota": 1,
            "cpu_period": 1,
            "scratch_bytes": 1,
            "scratch_inodes": 1,
            "shm_bytes": 1,
        },
        "arms": [],
        "root_removed": False,
    }
    with pytest.raises(evidence.GrowthError, match="^report_invalid$"):
        host.validate_report(report)
    report["status"] = "failed"
    assert host.validate_report(report) == report


def test_workflow_has_exact_head_and_no_arbitrary_dispatch_inputs():
    import yaml

    workflow = yaml.safe_load((host.ROOT / ".github/workflows/runtime-env-growth.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers and triggers["workflow_dispatch"] is None
    assert workflow["permissions"] == {"contents": "read"}
    checkout = workflow["jobs"]["investigate"]["steps"][0]["with"]
    assert checkout == {
        "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
        "persist-credentials": False,
    }


def test_read_only_container_has_writable_import_time_temp_root():
    recipe = (host.ROOT / "benchmarks/runtime-env-growth/Dockerfile").read_text()
    # Existing ownership-helper imports call tempfile.gettempdir before main.
    # At runtime /scratch is the sole writable mount; build RUNs precede this.
    runtime_environment = "ENV PATH=/workspace/.venv/bin:$PATH TMPDIR=/scratch HOME=/scratch"
    assert runtime_environment in recipe
    assert recipe.rfind("RUN ") < recipe.index(runtime_environment) < recipe.index("CMD ")


@pytest.mark.parametrize("drift", [None, "snapshot", "after"])
def test_django_preparation_checks_original_snapshot_original_in_order(
    monkeypatch, tmp_path, drift
):
    source, _, working = fake_upload(monkeypatch, tmp_path)
    runtime = types.ModuleType("django_ray.runtime.runtime_env")
    plans = types.ModuleType("django_ray.workflow.plans")
    calls = []
    resolved = SimpleNamespace(spec={"working_dir": str(source)})
    snap_path = tmp_path / "django-ray-runtime-env-snapshot-owned/working-dir"
    snap_path.mkdir(parents=True)
    (snap_path / "app_value.txt").write_text("A")
    snapshot = SimpleNamespace(spec={"working_dir": str(snap_path)})

    @contextlib.contextmanager
    def snapshot_local(value):
        assert value is resolved
        calls.append("open")
        try:
            yield snapshot
        finally:
            calls.append("close")
            (snap_path / "app_value.txt").unlink()
            snap_path.rmdir()

    def plan(value, **kwargs):
        stage = "snapshot" if value is snapshot else "after" if "close" in calls else "before"
        calls.append(stage)
        assert kwargs == {"trust_identity": {"environment_revision": "runtime-env-growth-v1"}}
        return SimpleNamespace(manifest={"digest": ("b" if drift == stage else "a") * 64})

    def prepare(value):
        assert value is snapshot
        calls.append("prepare")
        return working.upload_working_dir_if_needed(
            copy.deepcopy(value.spec),
            scratch_dir=str(tmp_path),
            include_gitignore=True,
        )

    runtime.normalize_runtime_env = lambda spec: resolved
    runtime.snapshot_local_runtime_env = snapshot_local
    runtime.prepare_runtime_env_for_ray_core = prepare
    plans.runtime_env_plan_identity = plan
    monkeypatch.setitem(sys.modules, "django_ray.runtime.runtime_env", runtime)
    monkeypatch.setitem(sys.modules, "django_ray.workflow.plans", plans)
    records = []
    if drift:
        with pytest.raises(evidence.GrowthError, match="^identity$"):
            driver.prepare("django", source, records.append)
        assert records[-1]["operation"] == "failed"
        assert "prepare" not in calls if drift == "snapshot" else "prepare" in calls
    else:
        driver.prepare("django", source, records.append)
        assert calls == ["before", "open", "snapshot", "prepare", "close", "after"]
        assert records[1] == {"snapshot_removed": True, "identity_sha256": "a" * 64}


def run_fake_raw_arm(monkeypatch, tmp_path):
    _, _, working = fake_upload(monkeypatch, tmp_path)
    root = tmp_path / "arm"
    root.mkdir()
    ray = types.ModuleType("ray")
    private = types.ModuleType("ray._private")
    worker = types.ModuleType("ray._private.worker")
    constants = SimpleNamespace(get_runtime_env_default_excludes=lambda: evidence.DEFAULT_EXCLUDES)
    session_path = root / "ray/session"
    session_path.mkdir(parents=True)
    worker._global_node = SimpleNamespace(get_session_dir_path=lambda: str(session_path))
    ray._private = private
    private.worker = worker
    private.ray_constants = constants
    submitted = []
    session = 0

    def init(**kwargs):
        nonlocal session
        session += 1
        assert kwargs["address"] == "local" and kwargs["_temp_dir"] == str(root / "ray")
        working.cache.clear()

    class Probe:
        def options(self, *, runtime_env):
            submitted.append((session, copy.deepcopy(runtime_env)))
            return self

        def remote(self):
            return {
                "variant": "B" if len(submitted) % 5 == 4 else "A",
                "included": True,
                "entries": 0,
            }

    ray.is_initialized = lambda: False
    ray.init = init
    ray.remote = lambda fn: Probe()
    ray.get = lambda value, timeout: value
    ray.shutdown = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.setitem(sys.modules, "ray._private", private)
    monkeypatch.setitem(sys.modules, "ray._private.worker", worker)
    monkeypatch.setattr(
        driver.importlib.metadata, "version", lambda name: "2.58.0" if name == "ray" else "0.7.0"
    )
    records = []
    driver.run_arm("raw", root, records.append, lambda: "start\n")
    return records, submitted


def test_actual_arm_orchestration_runs_three_five_control_sessions(monkeypatch, tmp_path):
    records, submitted = run_fake_raw_arm(monkeypatch, tmp_path)
    assert len(submitted) == 15
    for offset in (0, 5, 10):
        values = [spec["working_dir"] for _, spec in submitted[offset : offset + 5]]
        assert values[0] == values[1] == values[2] == values[4]
        assert values[3] != values[0]
    report.complete_trace(records, "raw")
    assert len(records) == 112


@pytest.mark.parametrize("repair", ["erase", "phase", "cache_archive", "variant", "partial"])
def test_complete_trace_refuses_erased_or_inconsistent_observations(monkeypatch, tmp_path, repair):
    records, _ = run_fake_raw_arm(monkeypatch, tmp_path)
    if repair == "erase":
        records.pop(3)
    elif repair == "phase":
        records[1]["index"] = 1
    elif repair == "cache_archive":
        data = next(
            row["value"] for row in records if row.get("value", {}).get("operation") == "cache_hit"
        )
        data["archives"] = [{"scope": "upload_scratch"}]
    elif repair == "variant":
        next(row["value"] for row in records if "variant" in row.get("value", {}))["variant"] = "B"
    else:
        records[3]["value"]["value"]["status"] = "scan_timeout"
    with pytest.raises(evidence.GrowthError, match="^report_invalid$"):
        report.complete_trace(records, "raw")


def test_complete_claim_cannot_replace_all_trace_records():
    with pytest.raises(evidence.GrowthError, match="^report_invalid$"):
        report.complete_trace([], "raw")


def identity_fixture(tmp_path):
    for name in (
        *identity.FIXED_FILES,
        "src/django_ray/__init__.py",
        "scripts/benchmark_runtime_env_growth.py",
        "scripts/runtime_env_growth/identity.py",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(name.encode())
    return tmp_path


def test_source_identity_separates_package_and_implementation_and_ignores_only_bytecode(tmp_path):
    root = identity_fixture(tmp_path)
    first = identity.source_identity(root)
    (root / "src/django_ray/__init__.py").write_bytes(b"changed")
    second = identity.source_identity(root)
    assert second["package_sha256"] != first["package_sha256"]
    assert second["implementation_sha256"] == first["implementation_sha256"]
    (root / "scripts/ignored.pyc").write_bytes(b"bytecode")
    assert identity.source_identity(root) == second
    (root / "scripts/new.py").write_bytes(b"untracked source")
    assert identity.source_identity(root)["copied_source_sha256"] != second["copied_source_sha256"]


@pytest.mark.parametrize("drift", [None, "installed", "copied"])
def test_inside_producer_identity_requires_sealed_bytes_and_actual_install(
    monkeypatch, tmp_path, drift
):
    root = identity_fixture(tmp_path)
    seal = {**identity.source_identity(root), "source_commit": "a" * 40, "source_tree": "b" * 40}
    (root / "growth-source.json").write_bytes(evidence.encode(seal))
    origin = root / ("foreign.py" if drift == "installed" else "src/django_ray/__init__.py")
    monkeypatch.setattr(
        identity.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origin))
    )
    monkeypatch.setattr(identity.importlib.metadata, "version", lambda name: "0.7.0")
    if drift == "copied":
        (root / "scripts/benchmark_runtime_env_growth.py").write_bytes(b"changed")
    if drift:
        with pytest.raises(evidence.GrowthError, match="^source_identity$"):
            identity.installed_identity(root)
    else:
        assert identity.installed_identity(root) == {**seal, "django_ray_version": "0.7.0"}


@pytest.mark.parametrize("drift", [None, "owner", "image", "identifier"])
def test_container_identity_requires_exact_image_and_owner_before_removal(monkeypatch, drift):
    expected = "sha256:" + "a" * 64
    values = ["b" * 64, expected, "owned-name"]
    if drift:
        values[{"identifier": 0, "image": 1, "owner": 2}[drift]] = "foreign"
    monkeypatch.setattr(
        host, "bounded_command", lambda *a, **kw: b"\n".join(json.dumps(v).encode() for v in values)
    )
    if drift:
        with pytest.raises(evidence.GrowthError, match="^container_identity$"):
            host.inspect_owned_container("owned-name", expected)
    else:
        assert host.inspect_owned_container("owned-name", expected) == "b" * 64


@pytest.mark.parametrize("verified", [True, False])
def test_ambiguous_create_is_removed_only_after_exact_owned_identity(
    monkeypatch, tmp_path, verified
):
    import psutil

    identity_fixture(tmp_path)
    monkeypatch.setattr(host, "ROOT", tmp_path)
    monkeypatch.setattr(host, "require_linux", lambda: None)
    monkeypatch.setattr(host.os, "sched_getaffinity", lambda pid: set(range(4)), raising=False)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=16_000_000_000))
    monkeypatch.setattr(host.shutil, "disk_usage", lambda path: SimpleNamespace(free=6_000_000_000))
    monkeypatch.setattr(host, "inspect_image", lambda *args: "sha256:" + "c" * 64)
    commands = []

    def command(argv, *args, **kwargs):
        commands.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return (("a" * 40) + "\n" + ("b" * 40) + "\n").encode()
        if argv[:2] == ["docker", "create"]:
            raise host.CommandError("command_timeout", cleanup_complete=False)
        return b""

    def inspect(name, image):
        assert name.startswith("django-ray-growth-") and image == "sha256:" + "c" * 64
        if not verified:
            raise evidence.GrowthError("container_identity")
        return "d" * 64

    monkeypatch.setattr(host, "bounded_command", command)
    monkeypatch.setattr(host, "inspect_owned_container", inspect)
    assert host.host() == 1
    saved = evidence.decode((tmp_path / "artifacts/runtime-env-growth/report.json").read_bytes())
    assert saved["failure"] == "command_timeout"
    assert saved["command_cleanup_complete"] is False
    removals = [argv for argv in commands if argv[:2] == ["docker", "rm"]]
    assert removals == ([["docker", "rm", "--force", "d" * 64]] if verified else [])
    assert saved["container_removed"] is verified
    assert saved["cleanup_failures"] == ([] if verified else ["container_identity"])


def test_source_clean_after_excludes_only_the_fixed_owned_artifact_directory(monkeypatch):
    commands = []
    monkeypatch.setattr(host, "bounded_command", lambda argv, *a: commands.append(argv) or b"")
    host.check_source_clean(artifacts_created=False)
    host.check_source_clean(artifacts_created=True)
    assert commands[0] == ["git", "status", "--porcelain", "--untracked-files=all", "--", "."]
    assert commands[1] == [*commands[0], ":(exclude)artifacts/runtime-env-growth"]


def test_docker_cli_uses_only_local_socket_and_closes_capture(monkeypatch):
    import psutil

    captured = []
    pipe = io.BytesIO()
    process = SimpleNamespace(stdout=pipe, poll=lambda: 0, returncode=0)
    selector = SimpleNamespace(
        register=lambda *a: None,
        unregister=lambda *a: None,
        close=lambda: None,
        select=lambda timeout: [(SimpleNamespace(fd=123, fileobj=pipe), None)],
    )
    monkeypatch.setattr(host, "require_linux", lambda: None)
    monkeypatch.setattr(psutil, "Process", lambda: SimpleNamespace(children=lambda **kw: []))
    monkeypatch.setattr(host, "enable_subreaper", lambda: None)
    monkeypatch.setattr(
        host.subprocess, "Popen", lambda argv, **kw: captured.append((argv, kw)) or process
    )
    monkeypatch.setattr(host.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(host.os, "read", lambda fd, size: b"")
    monkeypatch.setattr(host, "cleanup_owned", lambda p: (True, 0))
    monkeypatch.setenv("DOCKER_HOST", "private-remote-daemon")
    monkeypatch.setenv("DOCKER_CONTEXT", "other")
    assert host.bounded_command(["docker", "version"], 1) == b""
    assert captured[0][0] == ["docker", "--host=unix:///var/run/docker.sock", "version"]
    assert "DOCKER_HOST" not in captured[0][1]["env"]
    assert "DOCKER_CONTEXT" not in captured[0][1]["env"]
    assert pipe.closed


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("memory_bytes", evidence.MEMORY_BYTES + 1),
        ("pids", 257),
        ("cpu_quota", 200001),
        ("scratch_bytes", evidence.SCRATCH_BYTES + 1),
        ("scratch_inodes", 50001),
        ("shm_bytes", evidence.SHM_BYTES + 1),
    ],
)
def test_report_reapplies_effective_admission_ceilings(field, invalid):
    record = {
        "schema_version": 1,
        "status": "failed",
        "arms": [],
        "root_removed": False,
        "admission": {
            "memory_bytes": evidence.MEMORY_BYTES,
            "pids": 256,
            "cpu_quota": 200000,
            "cpu_period": 100000,
            "scratch_bytes": evidence.SCRATCH_BYTES,
            "scratch_inodes": 50000,
            "shm_bytes": evidence.SHM_BYTES,
        },
    }
    assert host.validate_report(record) == record
    record["admission"][field] = invalid
    with pytest.raises(evidence.GrowthError, match="^report_invalid$"):
        host.validate_report(record)
