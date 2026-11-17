"""Resource-free protected-surface comparison, identity and refusal boundaries."""

import base64
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from qualification.application import protected_logs as logs
from qualification.application import protected_runtime as runtime
from testproject import qualification_process as process


def proc(root, pid, argv, *, birth="123"):
    path = root / str(pid)
    path.mkdir()
    (path / "stat").write_text(f"{pid} (name (with) spaces) " + " ".join(["S"] * 19 + [birth]))
    (path / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    return path


def test_actual_visible_argv_includes_driver_and_unrelated_visible_process(tmp_path):
    driver = ["python", "-m", "django_ray.runtime.entrypoint", "--request-ref-b64", "locator"]
    proc(tmp_path, 10, driver)
    other = proc(tmp_path, 20, ["raylet"])
    assert process.scan_visible_argv(
        driver_pid=10, expected_argv=driver, forbidden=["canary"], proc_root=tmp_path
    ) == dict.fromkeys(process.FACTS, True)
    (other / "cmdline").write_bytes(b"raylet\0canary\0")
    with pytest.raises(ValueError, match="Protected process"):
        process.scan_visible_argv(
            driver_pid=10, expected_argv=driver, forbidden=["canary"], proc_root=tmp_path
        )


@pytest.mark.parametrize(
    "failure",
    [
        "missing-driver",
        "wrong-argv",
        "pid-count",
        "per-file",
        "total",
        "deadline",
        "denied",
        "recycled",
    ],
)
def test_visible_argv_fail_closed_bounds_and_identity(tmp_path, monkeypatch, failure):
    path = proc(tmp_path, 10, ["python", "locator"])
    expected = ["python", "locator"]
    if failure == "missing-driver":
        pid = 11
    else:
        pid = 10
    if failure == "wrong-argv":
        expected = ["python", "other"]
    if failure == "pid-count":
        monkeypatch.setattr(process, "MAX_PROCESSES", 0)
    if failure == "per-file":
        monkeypatch.setattr(process, "MAX_CMDLINE_BYTES", 2)
    if failure == "total":
        monkeypatch.setattr(process, "MAX_TOTAL_BYTES", 10)
    if failure == "deadline":
        ticks = iter([0, 3])
        monkeypatch.setattr(process.time, "monotonic", lambda: next(ticks))
    if failure == "denied":
        monkeypatch.setattr(
            process, "_read", lambda *a: (_ for _ in ()).throw(PermissionError("private"))
        )
    if failure == "recycled":
        values = iter([(10, "one"), (10, "two")])
        monkeypatch.setattr(process, "_identity", lambda *a: next(values))
    with pytest.raises((ValueError, TimeoutError, PermissionError)):
        process.scan_visible_argv(
            driver_pid=pid, expected_argv=expected, forbidden=["secret"], proc_root=tmp_path
        )
    assert path.exists()


def test_only_verified_vanished_nondriver_is_tolerated(tmp_path, monkeypatch):
    proc(tmp_path, 10, ["python"])
    other = proc(tmp_path, 20, ["other"])
    original = process._identity

    def vanished(path):
        if path == other:
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
            raise FileNotFoundError()
        return original(path)

    monkeypatch.setattr(process, "_identity", vanished)
    process.scan_visible_argv(
        driver_pid=10, expected_argv=["python"], forbidden=["secret"], proc_root=tmp_path
    )
    # A missing proc file while the numeric process still exists is not an exit.
    proc(tmp_path, 20, ["other"])
    monkeypatch.setattr(
        process,
        "_identity",
        lambda path: (
            original(path) if path.name == "10" else (_ for _ in ()).throw(FileNotFoundError())
        ),
    )
    with pytest.raises(ValueError, match="identity"):
        process.scan_visible_argv(
            driver_pid=10, expected_argv=["python"], forbidden=["secret"], proc_root=tmp_path
        )


def test_callable_does_not_effect_or_publish_private_probe_failure(tmp_path, settings, monkeypatch):
    from testproject.qualification_rq2_tasks import controlled

    settings.RQ2_ROOT = tmp_path
    monkeypatch.setattr(
        process, "observe_driver", lambda *a: (_ for _ in ()).throw(ValueError("private-canary"))
    )
    with pytest.raises(ValueError, match="^Owned live process qualification failed$"):
        controlled.func("success", "a" * 32)
    assert not (tmp_path / "success-effects").exists()
    assert not (tmp_path / "success-process.json").exists()


@pytest.mark.parametrize("field", ["live_driver_bound", "driver_argv_clear", "visible_argv_clear"])
def test_no_partial_or_truthy_process_receipt(tmp_path, field):
    facts = dict.fromkeys(process.FACTS, True)
    facts[field] = 1
    path = tmp_path / "receipt"
    path.write_text(json.dumps(facts))
    with pytest.raises(ValueError):
        process.read_receipt(path)


@pytest.mark.parametrize("failure", ["one-body", "aggregate", "count", "text"])
def test_http_capture_enforces_all_bounds_before_retention(monkeypatch, failure):
    body = b"private"
    if failure == "one-body":
        monkeypatch.setattr(runtime, "MAX_BODY", 1)
    if failure == "aggregate":
        monkeypatch.setattr(runtime, "MAX_TOTAL", 1)
    if failure == "count":
        monkeypatch.setattr(runtime, "MAX_BODIES", 0)
    if failure == "text":
        body = "private"
    capture = runtime.Capture(lambda *a, **kw: (200, body))
    with pytest.raises(ValueError):
        capture("/fixed")
    assert capture.bodies == []


@pytest.mark.parametrize("leak", [False, True])
def test_http_capture_checks_all_existing_bodies_and_clears_on_success(monkeypatch, leak):
    bodies = iter([b"enqueue", b"ciphertext" if leak else b"poll", b"fixed refusal"])
    capture = runtime.Capture(lambda *a, **kw: (200, next(bodies)))
    for _ in range(3):
        capture("/fixed")
    monkeypatch.setattr(
        runtime, "protected_values", lambda ids: ["ciphertext", "nonce", "envelope"]
    )
    if leak:
        with pytest.raises(ValueError, match="Protected surface observation failed"):
            capture.finish([1, 2, 3])
    else:
        receipt = capture.finish([1, 2, 3])
        runtime.validate(receipt)
        assert not capture.bodies


@pytest.mark.django_db
def test_real_owned_envelopes_include_nonce_ciphertext_and_no_row_mutation():
    from django_ray.models import RayTaskExecution

    rows = []
    for index in range(3):
        rows.append(
            RayTaskExecution.objects.create(
                task_id=f"protected-{index}",
                callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                state="SUCCEEDED" if index == 0 else "FAILED",
                runtime_env_profile="thin",
                runtime_env_json=json.dumps(
                    {"nonce": f"nonce-{index}", "ciphertext": f"cipher-{index}"}
                ),
            )
        )
    ids = [row.pk for row in rows]
    before = list(RayTaskExecution.objects.filter(pk__in=ids).values())
    values = runtime.protected_values(ids)
    assert len(values) == 11
    for index in range(3):
        assert rows[index].runtime_env_json in values
        assert f"nonce-{index}" in values
        assert f"cipher-{index}" in values
    assert list(RayTaskExecution.objects.filter(pk__in=ids).values()) == before
    rows[0].state = "FAILED"
    rows[0].save(update_fields=["state"])
    with pytest.raises(ValueError, match="outside"):
        runtime.protected_values(ids)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "oversized", ["x" * 65537, "é" * 32769, "ok\0" + "x" * 65537], ids=["ascii", "multibyte", "nul"]
)
def test_sql_projection_refuses_oversized_bytes_before_materializing(monkeypatch, oversized):
    from django.db.models.query import ValuesListIterable

    from django_ray.models import RayTaskExecution

    rows = [
        RayTaskExecution.objects.create(
            task_id=f"bounded-{index}",
            callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            state="SUCCEEDED" if index == 0 else "FAILED",
            runtime_env_profile="thin",
            runtime_env_json=oversized if index == 0 else '{"nonce":"n","ciphertext":"c"}',
        )
        for index in range(3)
    ]
    observed = []
    original = ValuesListIterable.__iter__

    def materialized(self):
        for row in original(self):
            observed.append(row)
            yield row

    monkeypatch.setattr(ValuesListIterable, "__iter__", materialized)
    with pytest.raises(ValueError, match="missing or oversized"):
        runtime.protected_values([row.pk for row in rows])
    assert len(observed) == 3
    assert next(row for row in observed if row[0] == rows[0].pk)[1] is None
    assert all(oversized not in row for row in observed)
    rows[0].refresh_from_db()
    assert rows[0].runtime_env_json == oversized


IMAGE = "registry/application@sha256:" + "a" * 64


def host_fixture(
    *, failure=None, generation="before", now=None, capture_seconds=60, source_seconds=120
):
    now: datetime = now or datetime.now(UTC)
    start = (now - timedelta(seconds=capture_seconds)).isoformat().replace("+00:00", "Z")
    early = (now - timedelta(seconds=source_seconds)).isoformat().replace("+00:00", "Z")
    evidence = {"api_clear": True, "row_ids": [1, 2, 3], "started_at": start}
    items = []
    for app, container in (
        ("django-web", "web"),
        ("django-manager", "manager"),
        (f"assert-{generation}", "assertions"),
    ):
        items.append(
            {
                "metadata": {"name": app + "-pod", "uid": app + "-uid", "labels": {"app": app}},
                "spec": {"containers": [{"name": container, "image": IMAGE}]},
                "status": {
                    "containerStatuses": [
                        {
                            "name": container,
                            "imageID": IMAGE,
                            "ready": True,
                            "restartCount": 0,
                            "state": {
                                "terminated" if app.startswith("assert") else "running": {
                                    "startedAt": early,
                                    "exitCode": 0,
                                }
                            },
                        }
                    ]
                },
            }
        )
    calls = []
    inventories = 0

    def run(argv, **kwargs):
        nonlocal inventories
        calls.append((argv, kwargs))
        if "get" in argv:
            inventories += 1
            current = json.loads(json.dumps(items))
            if failure == "replaced" and inventories == 2:
                current[0]["metadata"]["uid"] = "replacement"
            if failure == "restart":
                current[0]["status"]["containerStatuses"][0]["restartCount"] = 1
            if failure == "wrong-image":
                current[0]["spec"]["containers"][0]["image"] = "other"
            raw = json.dumps({"items": current}).encode()
        elif "exec" in argv:
            packet = json.loads(kwargs["input"])
            assert packet["protected_runtime"] == evidence
            if "--inspect-status" in argv:
                assert set(packet) == {"protected_runtime"}
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"refusal_api_clear": failure != "status"}).encode(), b""
                )
            assert len(packet["logs"]) == 2
            assert base64.b64decode(packet["logs"][0]).endswith(b"private log body\n")
            raw = json.dumps(
                {
                    "web_logs_clear": True,
                    "manager_logs_clear": failure != "comparison",
                }
            ).encode()
        elif f"assert-{generation}-pod" in argv:
            raw = json.dumps(
                {"layer": "application_core", "status": "passed", "protected_runtime": evidence}
            ).encode()
        else:
            raw = (early + " private log body\n").encode()
            if failure == "truncated":
                raw = b"x" * (logs.LOG_BYTES + 1)
            if failure == "missing-start":
                raw = (now.isoformat() + " private log body\n").encode()
            if failure == "empty":
                raw = b""
        return subprocess.CompletedProcess(argv, 0, raw, b"")

    return run, calls


def test_host_scan_binds_both_log_pods_and_compares_only_over_stdin(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/chainsaw/rewritten/config")
    run, calls = host_fixture()
    receipt = logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="before",
        run=run,
    )
    logs.validate_receipt(receipt, "before")
    assert "private" not in json.dumps(receipt)
    assert len(calls) == 7
    assert "--inspect-status" in calls[2][0]
    assert "--limit-bytes=1048577" in calls[3][0]
    assert "--limit-bytes=1048577" in calls[4][0]
    assert "--inspect-stdin" in calls[5][0]
    command_limits = [options["timeout"] for _, options in calls]
    assert command_limits == [2, 2, 7, 5, 5, 6, 2]
    assert logs.SECONDS == 30
    assert sum(command_limits) == logs.SECONDS - 1
    for argv, options in calls:
        assert argv[:3] == ["kubectl", "--context", "owned-context"]
        assert "private" not in " ".join(argv)
        assert options["timeout"] <= 7
        assert options["env"]["KUBECONFIG"] == "/original/config"
    assert sum("--limit-bytes=1048577" in argv for argv, _ in calls) == 2
    assert logs.os.environ["KUBECONFIG"] == "/chainsaw/rewritten/config"


def test_status_host_allowance_includes_time_outside_the_unchanged_child_alarm(monkeypatch):
    underlying, calls = host_fixture()
    now = 0.0
    monkeypatch.setattr(logs.time, "monotonic", lambda: now)

    def run(argv, **kwargs):
        nonlocal now
        # Synthetic transport/startup plus child work within its four seconds;
        # this models the nested limits, not the cause of any hosted failure.
        elapsed = 2.0 + 3.5 if "--inspect-status" in argv else 0.1
        if elapsed > kwargs["timeout"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        now += elapsed
        return underlying(argv, **kwargs)

    receipt = logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="before",
        run=run,
    )
    logs.validate_receipt(receipt, "before")
    assert logs.STATUS_HOST_SECONDS == 7
    assert logs.STATUS_CHILD_SECONDS == 4
    assert calls[2][1]["timeout"] == 7
    assert "--inspect-status" in calls[2][0]
    assert all("--limit-bytes=1048577" in calls[i][0] for i in (3, 4))
    assert now < logs.SECONDS == 30


def test_status_host_allowance_is_clipped_to_the_original_remaining_deadline(monkeypatch):
    underlying, calls = host_fixture()
    # A scheduling/processing pause before status can consume the observer's
    # elapsed budget even though the preceding commands completed promptly.
    ticks = iter([0, 0, 0, 25])
    monkeypatch.setattr(logs.time, "monotonic", lambda: next(ticks))

    def run(argv, **kwargs):
        if "--inspect-status" in argv:
            assert kwargs["timeout"] == 5 < logs.STATUS_HOST_SECONDS
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return underlying(argv, **kwargs)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="before",
            run=run,
        )
    assert caught.value.timeout == 5
    assert len(calls) == 2
    assert not any("--limit-bytes=1048577" in argv for argv, _ in calls)


def test_failed_status_prevents_log_snapshots():
    run, calls = host_fixture(failure="status")
    with pytest.raises(ValueError, match="comparison"):
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="before",
            run=run,
        )
    assert len(calls) == 3
    assert not any("--limit-bytes=1048577" in argv for argv, _ in calls)


@pytest.mark.parametrize("status_only", [False, True])
def test_private_checkers_close_database_before_status_and_final_checker_makes_no_request(
    monkeypatch,
    capsys,
    status_only,
):
    import io
    from types import SimpleNamespace

    from qualification.application import run_api

    closed = False
    alarms = []

    def read_rows(row_ids, *, status_only, started_at):
        nonlocal closed
        assert row_ids == [1, 2, 3]
        assert started_at == "2026-10-11T00:00:00Z"
        closed = True
        return ["private-canary"], ["first", "second"] if status_only else []

    def request(*args, **kwargs):
        assert closed
        assert status_only
        assert kwargs["request_timeout"] == 1
        return object()

    def statuses(*args, **kwargs):
        assert closed and status_only
        assert kwargs["tasks"] == ["first", "second"]

    packet = {
        "protected_runtime": {
            "api_clear": True,
            "row_ids": [1, 2, 3],
            "started_at": "2026-10-11T00:00:00Z",
        }
    }
    if not status_only:
        packet["logs"] = [base64.b64encode(b"safe").decode()] * 2
    monkeypatch.setattr(logs.sys, "platform", "linux")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setenv("DJANGO_API_TOKEN", "private-token")
    monkeypatch.setattr(
        logs.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(packet).encode()))
    )
    monkeypatch.setattr(logs.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(logs.signal, "signal", lambda *args: None)
    monkeypatch.setattr(logs.signal, "alarm", alarms.append, raising=False)
    monkeypatch.setattr(logs, "read_owned_rows", read_rows)
    monkeypatch.setattr(run_api, "ApplicationHttp", request)
    monkeypatch.setattr(logs, "verify_refusal_statuses", statuses)
    logs.inspect_stdin(status_only=status_only)
    assert closed
    assert alarms == [4 if status_only else 5]
    assert json.loads(capsys.readouterr().out) == (
        {"refusal_api_clear": True}
        if status_only
        else {"web_logs_clear": True, "manager_logs_clear": True}
    )


@pytest.mark.parametrize("status_only", [False, True])
def test_private_checker_path_never_initializes_django_in_a_fresh_process(status_only):
    import sys
    from pathlib import Path

    program = r"""
import base64, importlib.abc, io, json, os, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
class DenyApplications(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"django", "django_ray", "ray", "unfold", "testproject"}:
            raise AssertionError("Private checker imported an application")
sys.meta_path.insert(0, DenyApplications())
from qualification.application import protected_logs as logs
status_only = sys.argv[2] == "status"
packet = {"protected_runtime": {
    "api_clear": True, "row_ids": [1, 2, 3], "started_at": "2026-10-17T00:00:00Z"
}}
if not status_only:
    packet["logs"] = [base64.b64encode(b"safe").decode()] * 2
logs.sys.platform = "linux"
os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings_qualification"
os.environ["DJANGO_API_TOKEN"] = "private-canary"
logs.sys.stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(packet).encode()))
logs.signal.SIGALRM = 14
logs.signal.signal = lambda *args: None
logs.signal.alarm = lambda *args: None
logs.read_owned_rows = lambda *args, **kwargs: (["private-canary"], ["first", "second"])
def statuses(request, *, token, tasks, values):
    assert status_only and tasks == ["first", "second"] and values == ["private-canary"]
logs.verify_refusal_statuses = statuses
logs.inspect_stdin(status_only=status_only)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            program,
            str(Path(__file__).resolve().parents[2]),
            "status" if status_only else "logs",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stderr == ""
    assert json.loads(result.stdout) == (
        {"refusal_api_clear": True}
        if status_only
        else {"web_logs_clear": True, "manager_logs_clear": True}
    )


@pytest.mark.parametrize("configured", [None, "", "first", "first;second"])
def test_caller_kubeconfig_paths_preserve_default_and_multiple_files(
    tmp_path, monkeypatch, configured
):
    from qualification.application import run_chainsaw as runner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner.Path, "home", lambda: tmp_path)
    if configured is None:
        monkeypatch.delenv("KUBECONFIG", raising=False)
    else:
        monkeypatch.setenv("KUBECONFIG", configured.replace(";", runner.os.pathsep))
    names = configured.split(";") if configured else [".kube/config"]
    assert runner.caller_kubeconfig_paths() == runner.os.pathsep.join(
        str((tmp_path / name).resolve()) for name in names
    )


@pytest.mark.parametrize(
    "failure",
    [
        "replaced",
        "restart",
        "wrong-image",
        "comparison",
        "status",
        "truncated",
        "missing-start",
        "empty",
    ],
)
def test_host_scan_rejects_incomplete_or_changed_surfaces(failure):
    run, _ = host_fixture(failure=failure)
    with pytest.raises(ValueError):
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="before",
            run=run,
        )


def test_host_global_deadline_never_resets(monkeypatch):
    run, _ = host_fixture()
    ticks = iter([0, 31])
    monkeypatch.setattr(logs.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="before",
            run=run,
        )


def test_host_cli_failure_suppresses_raw_exceptions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        logs, "observe", lambda **kw: (_ for _ in ()).throw(ValueError("private log"))
    )
    assert (
        logs.main(
            [
                "--context=owned-context",
                "--kubeconfig-paths=/original/config",
                "--namespace=django-ray-core-owned",
                "--image=" + IMAGE,
                "--generation=before",
                "--output=" + str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "private log" not in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_failure_diagnostics_drop_unknown_values_and_keep_real_http_and_rq2_fields():
    from qualification.application import safe_diagnostics as safe
    from qualification.application.run_rq2 import Diagnostics

    diagnostic = Diagnostics().snapshot()
    diagnostic["failure_type"] = "private-canary"
    diagnostic["start_observation"]["job_status"] = "private-canary"
    raw = b"private-canary stdout\n" + b"\n".join(
        json.dumps(value).encode()
        for value in (
            {
                "generation_phase": "rq2",
                "status": "failed",
                "reason": "timeout",
                "private": "private-canary",
            },
            {
                "layer": "application_core",
                "status": "failed",
                "http_failure": {
                    "code": "response_headers_failed",
                    "status": None,
                    "observer": "application_core",
                    "request_substage": "task_status",
                    "method": "GET",
                    "attempt": 1,
                    "configured_timeout_seconds": 10,
                    "outer_remaining_seconds": 20,
                    "retry_policy": "none",
                    "path": "private-canary",
                    "message": "private-canary",
                },
            },
            {"layer": "application_rq2", "status": "failed", "diagnostics": diagnostic},
        )
    )
    clean = safe.lines(raw)
    assert "private-canary" not in json.dumps(clean)
    assert clean[0]["reason"] == "timeout"
    assert clean[1]["http_failure"]["request_substage"] == "task_status"
    assert clean[1]["http_failure"]["outer_remaining_seconds"] == 20
    assert clean[2]["diagnostics"]["failure_type"] == "unexpected"


def test_collector_failed_identity_never_writes_arbitrary_log_bytes(tmp_path, monkeypatch):
    from qualification.application import run_chainsaw as runner

    pod = {
        "metadata": {
            "name": "django-web-fixture",
            "uid": "11111111-1111-4111-8111-111111111111",
            "labels": {"app": "django-web"},
        },
        "spec": {
            "containers": [{"name": "web", "image": IMAGE}],
            "initContainers": [{"name": "setup", "image": IMAGE}],
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "web",
                    "imageID": IMAGE,
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {}},
                }
            ],
            "initContainerStatuses": [
                {
                    "name": "setup",
                    "imageID": IMAGE,
                    "restartCount": 1,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ],
        },
    }
    raw = (
        b"private-canary raw stdout\n"
        + json.dumps(
            {"layer": "application_core", "status": "failed", "message": "private-canary"}
        ).encode()
    )
    monkeypatch.setattr(
        runner,
        "checked",
        lambda argv: raw if "logs" in argv else json.dumps({"items": [pod]}).encode(),
    )
    with pytest.raises(
        ValueError, match="Receipt producer is not the completed candidate container"
    ):
        runner.collect(["kubectl"], "owned", tmp_path, IMAGE)
    for path in tmp_path.iterdir():
        assert b"private-canary" not in path.read_bytes()
    assert not list(tmp_path.glob("*.log"))


def test_collector_rejects_appended_fields_even_in_a_passed_receipt():
    from qualification.application import run_chainsaw as runner

    receipt = {
        "schema_version": 1,
        "layer": "application_setup",
        "status": "passed",
        "complete_application_gate": False,
        "message": "private-canary",
    }
    with pytest.raises(ValueError, match="unknown fields"):
        runner.parse_receipts(json.dumps(receipt).encode(), ("setup",))


def test_diagnostics_never_request_pod_logs_or_retain_free_text(tmp_path, monkeypatch):
    from qualification.application import run_chainsaw as runner

    item = {
        "metadata": {"name": "private-canary"},
        "status": {
            "phase": "Running",
            "message": "private-canary",
            "containerStatuses": [
                {
                    "name": "private-canary",
                    "restartCount": 1,
                    "state": {"terminated": {"message": "private-canary", "exitCode": 1}},
                }
            ],
        },
        "type": "Warning",
        "reason": "Failed",
        "message": "private-canary",
        "count": 2,
    }

    def checked(argv, **kwargs):
        assert "logs" not in argv
        return json.dumps({"items": [item]}).encode()

    monkeypatch.setattr(runner, "checked", checked)
    runner.diagnose(["kubectl"], "owned", tmp_path)
    assert len(list(tmp_path.iterdir())) == 3
    assert all(b"private-canary" not in path.read_bytes() for path in tmp_path.iterdir())


def test_actor_diagnostic_projection_refuses_arbitrary_strings():
    from qualification.application.safe_diagnostics import actor

    receipt = {
        "diagnostic_only": True,
        "status": "available",
        "message": "private-canary",
        "resources": {
            kind: {
                "status": "available",
                "records": [
                    {
                        "state": "private-canary",
                        "class_name": "private-canary",
                        "actor_id": "private-canary",
                        "pid": "private-canary",
                        "message": "private-canary",
                    }
                ],
            }
            for kind in ("actors", "tasks", "nodes")
        },
    }
    assert "private-canary" not in json.dumps(actor(receipt))


@pytest.mark.parametrize("invalid", [None, "private-canary", 3, {"message": "private-canary"}])
def test_malformed_status_lists_cannot_break_safe_diagnostics(invalid):
    from qualification.application.safe_diagnostics import resources

    assert resources("pods", invalid) == []
    clean = resources(
        "pods",
        [
            {
                "status": {
                    "containerStatuses": invalid,
                    "initContainerStatuses": invalid,
                }
            }
        ],
    )
    assert len(clean) == 1
    assert clean[0].pop("observed_at").endswith("Z")
    assert clean == [
        {"state": "unavailable", "role": "unavailable", "conditions": [], "containers": []}
    ]


def test_added_catalogue_failure_diagnostics_keep_only_finite_fields():
    from qualification.application import safe_diagnostics as safe

    values = [
        {
            "layer": "application_showcase_failure",
            "status": "failed",
            "failed_stage": "observe",
            "failure_kind": "validation",
            "message": "private-canary",
        },
        {
            "layer": "application_protocol",
            "status": "failed",
            "failure_phase": "strict_refusal",
            "failure_type": "validation",
            "cleanup_failed": True,
            "message": "private-canary",
        },
    ]
    clean = safe.lines(b"\n".join(json.dumps(item).encode() for item in values))
    assert clean == [
        {
            "layer": "application_showcase_failure",
            "status": "failed",
            "failed_stage": "observe",
            "failure_kind": "validation",
        },
        {
            "layer": "application_protocol",
            "status": "failed",
            "failure_phase": "strict_refusal",
            "failure_type": "validation",
            "cleanup_failed": True,
        },
    ]
    for value in values:
        for key in ("failed_stage", "failure_kind", "failure_phase", "failure_type"):
            if key in value:
                value[key] = "private-canary"
    assert "private-canary" not in json.dumps(
        safe.lines(b"\n".join(json.dumps(item).encode() for item in values))
    )


def test_application_workflow_has_no_raw_operator_diagnostic_sink():
    from qualification.application.run_chainsaw import ROOT

    text = (ROOT / ".github/workflows/application-qualification.yml").read_text()
    assert "operator.log" not in text
    assert "logs deployment/kuberay-operator" not in text
    assert "Upload application receipts and build identity" in text


def test_large_workflow_line_does_not_hide_later_fixed_failure_diagnostics():
    from qualification.application.safe_diagnostics import lines

    workflow = json.dumps(
        {
            "layer": "workflow_api_admin",
            "status": "passed",
            "observations": "x" * 19080,
        }
    ).encode()
    following = [
        {"layer": "application_rq2", "status": "failed"},
        {"generation_phase": "showcase", "status": "failed", "reason": "incomplete_command_set"},
        {
            "layer": "application_showcase_failure",
            "status": "failed",
            "http_failure": {
                "code": "response_headers_failed",
                "status": None,
                "endpoint": "node_detail",
            },
        },
    ]
    observed = lines(b"\n".join([workflow, *[json.dumps(item).encode() for item in following]]))
    assert observed == following
    assert "observations" not in json.dumps(observed)


@pytest.mark.parametrize(
    "failure", [None, "missing", "identity", "state", "oversized", "protected", "contract"]
)
def test_refused_status_reads_require_both_exact_bounded_failures(failure):
    from qualification.application.api import (
        EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
        EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
    )

    tasks = ["25200000-0000-4000-8000-000000000001", "25200000-0000-4000-8000-000000000002"]
    calls = []

    def request(path, **kwargs):
        calls.append(path)
        assert kwargs["method"] == "GET"
        assert kwargs["response_limit"] == 65536
        payload = {
            "task_id": path.rsplit("/", 1)[-1],
            "state": "FAILED",
            "status": "FAILED",
            "attempt_number": 1,
            "execution_generation": 1,
            "args": [],
            "kwargs": {},
            "input_max_bytes": EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
            "response_max_bytes": EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
            "input_omission_reason": None,
        }
        if failure == "identity":
            payload["task_id"] = tasks[0]
        if failure == "state":
            payload["state"] = "SUCCEEDED"
        if failure == "contract":
            payload.pop("input_omission_reason")
        if failure == "protected":
            payload["error"] = "private-canary"
        return (404 if failure == "missing" else 200), (
            b"x" * 65537 if failure == "oversized" else json.dumps(payload).encode()
        )

    if failure:
        with pytest.raises(ValueError):
            logs.verify_refusal_statuses(
                request, token="private-token", tasks=tasks, values=["private-canary"]
            )
    else:
        logs.verify_refusal_statuses(
            request, token="private-token", tasks=tasks, values=["private-canary"]
        )
        assert len(calls) == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "suffix", ["", "\0hidden", "x" * 65536], ids=["canonical", "nul", "oversized"]
)
def test_refused_task_ids_project_bounded_complete_canonical_values(suffix):
    from django_ray.models import RayTaskExecution

    tasks = ["25200000-0000-4000-8000-000000000001", "25200000-0000-4000-8000-000000000002"]
    rows = [
        RayTaskExecution.objects.create(
            task_id=task + (suffix if i else ""), callable_path="fixed", state="FAILED"
        )
        for i, task in enumerate(tasks)
    ]
    ids = [999, *[row.pk for row in rows]]
    if suffix:
        with pytest.raises(ValueError):
            runtime.refused_task_ids(ids)
    else:
        assert runtime.refused_task_ids(ids) == tasks


def provider_nanoseconds(stamp):
    """Independent fake-provider ordering: integer epoch nanoseconds, no floats."""
    assert stamp.endswith("Z")
    whole, _, fraction = stamp[:-1].partition(".")
    parsed = datetime.fromisoformat(whole)
    days = parsed.toordinal() - datetime(1970, 1, 1).toordinal()
    seconds = days * 86400 + parsed.hour * 3600 + parsed.minute * 60 + parsed.second
    return seconds * 1_000_000_000 + int(fraction.ljust(9, "0"))


def overlap_fixture(tmp_path, *, same_timestamp=False, failure=None):
    """Model source timestamps, inclusive filtering, then a prefix byte limit."""
    now = datetime.now(UTC).replace(microsecond=123456)
    stamp = (now - timedelta(seconds=230)).strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"
    old_time = (now - timedelta(seconds=250)).isoformat().replace("+00:00", "Z")
    new_time = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    old = (old_time + " synthetic old history\n").encode()
    old *= (logs.LOG_BYTES * 3 // 5) // len(old)
    earlier_same_time = (
        (stamp + " synthetic equal-time predecessor\n").encode() if same_timestamp else b""
    )
    anchor = (stamp + " synthetic complete overlap\n").encode()
    near_prior = (
        stamp.replace(".123456789Z", ".123456788Z") + " synthetic nanosecond predecessor\n"
    ).encode()
    baseline = old + near_prior + earlier_same_time + anchor
    current = (new_time + " synthetic current history\n").encode()
    current *= (logs.LOG_BYTES * 3 // 5) // len(current)
    event = (new_time + " synthetic required post-Capture event\n").encode()
    current += event
    assert len(baseline) <= logs.LOG_BYTES < len(baseline + current)
    previous, previous_calls = host_fixture(now=now, capture_seconds=240, source_seconds=300)
    following, following_calls = host_fixture(now=now, generation="after", source_seconds=300)
    flags = []

    def wrap(underlying, *, after):
        def run(argv, **kwargs):
            if "--inspect-stdin" in argv:
                packet = json.loads(kwargs["input"])
                manager = base64.b64decode(packet["logs"][1])
                expected = earlier_same_time + anchor + current if after else baseline
                assert manager == expected
                if after:
                    assert provider_nanoseconds(new_time) > provider_nanoseconds(
                        packet["protected_runtime"]["started_at"]
                    )
                    assert event in manager and near_prior not in manager
            result = underlying(argv, **kwargs)
            if after and "get" in argv and failure == "source-changed":
                value = json.loads(result.stdout)
                value["items"][1]["metadata"]["uid"] = "other-manager-uid"
                return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b"")
            if "logs" in argv and argv[argv.index("-c") + 1] == "manager":
                raw = baseline + current if after else baseline
                since = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--since-time=")]
                if after:
                    flags.append(since)
                if since:
                    # Deliberately keep all same-timestamp records before the anchor.
                    lower = provider_nanoseconds(since[0])
                    raw = b"".join(
                        line
                        for line in raw.splitlines(keepends=True)
                        if provider_nanoseconds(line.split(b" ", 1)[0].decode()) >= lower
                    )
                if after and failure == "changed-anchor":
                    raw = raw.replace(anchor, (stamp + " changed complete overlap\n").encode())
                elif after and failure == "missing-anchor":
                    raw = raw.replace(anchor, b"")
                elif after and failure == "overflow":
                    raw += current
                elif after and failure == "empty":
                    raw = b""
                elif after and failure == "partial-only":
                    raw = (stamp + " unfinished overlap").encode()
                elif after and failure == "first-later":
                    later = stamp.replace(".123456789Z", ".123456790Z")
                    raw = (later + " impossible later prefix\n").encode() + anchor + current
                elif after and failure == "last-earlier":
                    earlier = stamp.replace(".123456789Z", ".123456788Z")
                    raw += (earlier + " impossible earlier suffix\n").encode()
                (maximum,) = [
                    int(arg.split("=", 1)[1]) for arg in argv if arg.startswith("--limit-bytes=")
                ]
                return subprocess.CompletedProcess(argv, 0, raw[:maximum], b"")
            return result

        return run

    before = logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="before",
        run=wrap(previous, after=False),
    )
    raw = json.dumps(before, sort_keys=True).encode()
    path = tmp_path / "before-protected-logs.json"
    path.write_bytes(raw)
    return raw, path, wrap(following, after=True), previous_calls, following_calls, flags, stamp


@pytest.mark.parametrize("same_timestamp", [False, True])
def test_host_continuation_filters_only_after_an_exact_full_scan(tmp_path, same_timestamp):
    before, path, run, previous_calls, calls, flags, stamp = overlap_fixture(
        tmp_path, same_timestamp=same_timestamp
    )
    after = logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="after",
        previous_receipt=path,
        run=run,
    )
    raw = json.dumps(after, sort_keys=True).encode()
    logs.validate_pair(before, raw)
    assert flags == [[stamp]]  # Preserve all nine fractional digits in argv.
    assert not any(arg.startswith("--since-time=") for argv, _ in previous_calls for arg in argv)
    assert len(previous_calls) == len(calls) == 7
    assert [options["timeout"] for _, options in calls] == [2, 2, 7, 5, 5, 6, 2]
    assert sum("--limit-bytes=1048577" in argv for argv, _ in calls) == 2
    for argv, _ in calls:
        if "--timestamps=true" in argv:
            role = argv[argv.index("-c") + 1]
            expected = json.loads(before)["log_window"]["sources"][role]["last_time"]
            assert [arg for arg in argv if arg.startswith("--since-time=")] == [
                "--since-time=" + expected
            ]
    assert max(len(before), len(raw)) <= 2048
    assert "synthetic" not in raw.decode() and "synthetic" not in before.decode()


@pytest.mark.parametrize(
    "failure",
    [
        "changed-anchor",
        "missing-anchor",
        "overflow",
        "empty",
        "partial-only",
        "source-changed",
        "first-later",
        "last-earlier",
    ],
)
def test_host_continuation_refuses_missing_or_incomplete_overlap(tmp_path, failure):
    _, path, run, _, calls, _, _ = overlap_fixture(tmp_path, failure=failure)
    with pytest.raises(ValueError) as caught:
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="after",
            previous_receipt=path,
            run=run,
        )
    assert not any("--inspect-stdin" in argv for argv, _ in calls)

    diagnostic = json.loads(logs.diagnostics.diagnostic(caught.value, "host"))
    assert (
        diagnostic["failure_phase"]
        == {
            "changed-anchor": "manager_log",
            "missing-anchor": "manager_log_coverage",
            "overflow": "manager_log_command_size",
            "empty": "manager_log_empty",
            "partial-only": "manager_log",
            "source-changed": "manager_log",
            "first-later": "manager_log",
            "last-earlier": "manager_log",
        }[failure]
    )
    assert diagnostic["failure_type"] == "invalid"
    assert "synthetic" not in json.dumps(diagnostic)


@pytest.mark.parametrize("failure", ["missing", "oversized", "future-anchor"])
def test_host_continuation_requires_a_bounded_previous_proof(tmp_path, failure):
    _, path, run, _, calls, _, _ = overlap_fixture(tmp_path)
    if failure == "missing":
        path = None
    elif failure == "oversized":
        path.write_bytes(b" " * 2049)
    else:
        previous = json.loads(path.read_bytes())
        previous["log_window"]["sources"]["manager"]["last_time"] = "2999-01-01T00:00:00Z"
        path.write_text(json.dumps(previous))
    with pytest.raises(ValueError):
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="after",
            previous_receipt=path,
            run=run,
        )
    assert not any("exec" in argv or "--timestamps=true" in argv for argv, _ in calls)


@pytest.mark.parametrize("mutation", ["digest", "identity", "since", "overlap", "unknown"])
def test_protected_window_pair_rejects_broken_joins(tmp_path, mutation):
    before, path, run, _, _, _, _ = overlap_fixture(tmp_path)
    after = logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="after",
        previous_receipt=path,
        run=run,
    )
    window = after["log_window"]
    if mutation == "digest":
        window["previous_receipt_sha256"] = "f" * 64
    elif mutation == "identity":
        window["sources"]["web"]["identity_sha256"] = "f" * 64
    elif mutation == "since":
        window["sources"]["web"]["since_time"] = "2000-01-01T00:00:00Z"
    elif mutation == "overlap":
        window["sources"]["web"]["overlap_sha256"] = "f" * 64
    else:
        window["private-canary"] = "must-not-pass"
    with pytest.raises(ValueError):
        logs.validate_pair(before, json.dumps(after).encode())


def test_current_protected_logs_refuse_legacy_except_fixed_historical_policy():
    legacy = {
        "schema_version": 1,
        "layer": "application_protected_logs",
        "status": "passed",
        "complete_application_gate": False,
        "generation": "before",
        "assertions": dict.fromkeys(logs.FACTS, True),
    }
    with pytest.raises(ValueError):
        logs.validate_receipt(legacy, "before")
    logs.validate_receipt(legacy, "before", allow_historical_schema1=True)
    legacy["assertions"][logs.FACTS[0]] = 1
    with pytest.raises(ValueError):
        logs.validate_receipt(legacy, "before", allow_historical_schema1=True)
    with pytest.raises(ValueError):
        logs.validate_receipt(legacy, "before", allow_historical_schema1=1)


@pytest.mark.parametrize(
    "raw", [b"2000-01-01T00:00:00Z unfinished", b"no-time complete\n"], ids=["partial", "malformed"]
)
def test_protected_log_anchor_requires_a_complete_timestamped_record(raw):
    with pytest.raises(ValueError):
        logs._last_record(raw)


@pytest.mark.parametrize("boundary", ["capture", "last"])
def test_protected_window_refuses_reversed_nanosecond_chronology(tmp_path, boundary):
    _, path, run, _, _, _, _ = overlap_fixture(tmp_path)
    after = logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="after",
        previous_receipt=path,
        run=run,
    )
    window = after["log_window"]
    second = window["capture_started_at"][:19]
    window["capture_started_at"] = second + (
        ".123456000Z" if boundary == "capture" else ".123456999Z"
    )
    for observed in window["sources"].values():
        observed["since_time"] = second + (
            ".123456001Z" if boundary == "capture" else ".123456002Z"
        )
        observed["last_time"] = second + (".123456002Z" if boundary == "capture" else ".123456001Z")
    with pytest.raises(ValueError):
        logs.validate_receipt(after, "after")


def test_host_continuation_rejects_anchor_after_capture_within_one_microsecond(tmp_path):
    before, path, run, _, calls, _, _ = overlap_fixture(tmp_path)
    previous = json.loads(before)
    capture = datetime.fromisoformat(previous["log_window"]["capture_started_at"]) + timedelta(
        seconds=180
    )
    later = capture.isoformat().replace("+00:00", "Z").replace(".123456Z", ".123456001Z")
    previous["log_window"]["sources"]["manager"]["last_time"] = later
    path.write_text(json.dumps(previous))
    with pytest.raises(ValueError):
        logs.observe(
            context="owned-context",
            kubeconfig_paths="/original/config",
            namespace="django-ray-core-owned",
            image=IMAGE,
            generation="after",
            previous_receipt=path,
            run=run,
        )
    assert len(calls) == 2
    assert not any("exec" in argv or "--timestamps=true" in argv for argv, _ in calls)
