"""Resource-free evidence checks for the disposable application core layer."""

import base64
import hashlib
import json
from datetime import timedelta
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from django.utils import timezone

from django_ray.conf import settings as ray_settings
from django_ray.models import RayTaskExecution, TaskWorkerLease
from django_ray.runtime.runtime_env_encryption import (
    RuntimeEnvEncryptionError,
    protect_runtime_env_snapshot,
    validate_runtime_env_encryption_settings,
)
from qualification.application import run_core

TASK_ID = "25200000-0000-4000-8000-000000000002"
MARKER = "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"


@pytest.fixture(autouse=True)
def maintenance_observer(monkeypatch):
    from qualification.application import lease_policy, runtime_env_maintenance

    def verify(*args, evidence, **kwargs):
        evidence.update(
            {
                case: dict.fromkeys(facts, True)
                for case, facts in runtime_env_maintenance.FACTS.items()
            }
        )

    monkeypatch.setattr(runtime_env_maintenance, "verify_maintenance", verify)
    monkeypatch.setattr(
        lease_policy,
        "verify_lease_policy",
        lambda **kwargs: kwargs["evidence"].update(dict.fromkeys(lease_policy.FACTS, True)),
    )


@pytest.fixture
def durable(monkeypatch):
    config = {
        "RAY_ADDRESS": "ray://ray-head:10001",
        "WORKER_LEASE_SECONDS": 60,
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "qualification",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {
            "qualification": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
        },
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
    }
    monkeypatch.setattr(ray_settings, "get_settings", lambda: config)
    monkeypatch.setenv("DJANGO_RAY_RECOVERY_WORKING_DIR", "/runtime/recovery.zip")
    now = timezone.now()
    package_version = version("django-ray")
    runtime = {
        "working_dir": "/runtime/recovery.zip",
        "env_vars": {"DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE": MARKER},
    }
    row = SimpleNamespace(
        pk=5,
        task_id=TASK_ID,
        state="SUCCEEDED",
        attempt_number=1,
        execution_generation=1,
        execution_protocol_version=1,
        runtime_env_profile="project",
        ray_target_address=config["RAY_ADDRESS"],
        ray_address=config["RAY_ADDRESS"],
        ray_job_id="ray_core:5",
        ray_job_request_reference=None,
        created_with_django_ray_version=package_version,
        managed_with_django_ray_version=package_version,
        executor_django_ray_version=package_version,
        created_at=now - timedelta(seconds=4),
        started_at=now - timedelta(seconds=3),
        finished_at=now - timedelta(seconds=1),
        claimed_by_worker="owned-worker",
        result_data="5",
        result_reference=None,
    )
    worker = SimpleNamespace(
        worker_id="owned-worker",
        is_active=True,
        hostname="django-manager-owned",
        django_ray_version=package_version,
        started_at=now - timedelta(seconds=5),
        last_heartbeat_at=now - timedelta(seconds=1),
    )
    attempt = SimpleNamespace(**vars(row))
    row.attempts = SimpleNamespace(all=lambda: [attempt])

    def seal():
        plaintext = json.dumps(runtime, sort_keys=True, separators=(",", ":"))
        row.runtime_env_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        row.runtime_env_json = protect_runtime_env_snapshot(
            plaintext,
            task_id=row.task_id,
            profile=row.runtime_env_profile,
            digest=row.runtime_env_hash,
            encryption=validate_runtime_env_encryption_settings(
                config, django_secret_key="unused", django_secret_key_fallbacks=[]
            ),
        )

    seal()
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **kwargs: row)
    monkeypatch.setattr(TaskWorkerLease.objects, "get", lambda **kwargs: worker)
    return SimpleNamespace(row=row, worker=worker, attempt=attempt, runtime=runtime, seal=seal)


def test_durable_evidence_authenticates_snapshot_and_manager(durable):
    result = run_core.verify_durable_task(
        TASK_ID, profile="project", manager_prefix="django-manager-"
    )
    assert result["encrypted_snapshot_authenticated"] is True
    assert result["worker_id"] == "owned-worker"
    assert result["elapsed_seconds"] == 3
    assert MARKER not in json.dumps(result)
    assert "ciphertext" not in json.dumps(result)


def test_durable_evidence_identifies_inverted_claim_time_without_values(durable):
    durable.row.created_at = durable.row.started_at + timedelta(microseconds=1)
    with pytest.raises(run_core.CoreEvidenceError) as raised:
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")
    assert raised.value.code is run_core.CoreEvidenceFailure.EXECUTION_TIMING
    assert str(raised.value) == "execution_timing"


def test_owner_clock_is_captured_after_concurrent_heartbeat(durable, monkeypatch):
    observed_at = timezone.now()
    clock = [observed_at]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])

    def read_worker(**kwargs):
        clock[0] += timedelta(milliseconds=1)
        durable.worker.last_heartbeat_at = clock[0]
        return durable.worker

    monkeypatch.setattr(TaskWorkerLease.objects, "get", read_worker)
    result = run_core.verify_durable_task(
        TASK_ID, profile="project", manager_prefix="django-manager-"
    )
    assert result["worker_id"] == "owned-worker"


@pytest.mark.parametrize("offset", [-61, 1])
def test_owner_clock_still_rejects_stale_or_future_heartbeats(durable, monkeypatch, offset):
    observed_at = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: observed_at)
    durable.worker.last_heartbeat_at = observed_at + timedelta(seconds=offset)
    with pytest.raises(run_core.CoreEvidenceError) as raised:
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")
    assert raised.value.code is run_core.CoreEvidenceFailure.OWNER_HEARTBEAT


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "FAILED"),
        ("attempt_number", 2),
        ("execution_generation", 0),
        ("execution_protocol_version", 2),
        ("runtime_env_profile", "different"),
        ("ray_target_address", "ray://other:10001"),
        ("ray_address", "auto"),
        ("ray_job_id", None),
        ("ray_job_request_reference", "rq2:another-transport"),
        ("executor_django_ray_version", "0.0.0"),
        ("finished_at", None),
    ],
)
def test_durable_evidence_rejects_wrong_execution(durable, field, value):
    setattr(durable.row, field, value)
    with pytest.raises((ValueError, RuntimeEnvEncryptionError)):
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")


@pytest.mark.parametrize(
    "failure",
    ["inactive", "foreign", "stale", "attempt", "tamper", "identity", "pip", "path", "marker"],
)
def test_durable_evidence_rejects_unproven_owner_or_environment(durable, failure):
    if failure == "inactive":
        durable.worker.is_active = False
    elif failure == "foreign":
        durable.worker.hostname = "other-manager"
    elif failure == "stale":
        durable.worker.last_heartbeat_at -= timedelta(seconds=61)
    elif failure == "attempt":
        durable.attempt.result_data = "incorrect"
    elif failure == "tamper":
        envelope = json.loads(durable.row.runtime_env_json)
        envelope["ciphertext"] = "A" * len(envelope["ciphertext"])
        durable.row.runtime_env_json = json.dumps(envelope)
    elif failure == "identity":
        durable.row.runtime_env_hash = "0" * 64
    else:
        if failure == "pip":
            durable.runtime["pip"] = ["unreviewed"]
        elif failure == "path":
            durable.runtime["working_dir"] = "/other/recovery.zip"
        else:
            durable.runtime["env_vars"]["DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE"] = "wrong"
        durable.seal()
    with pytest.raises((ValueError, RuntimeEnvEncryptionError)):
        run_core.verify_durable_task(TASK_ID, profile="project", manager_prefix="django-manager-")


def test_runtime_probe_uses_existing_bounded_api_route(durable, monkeypatch):
    durable.row.callable_path = "testproject.apps.cluster_tasks.tasks.runtime_env_probe"
    durable.row.result_data = json.dumps(
        {
            "profile_marker": "thin",
            "storage_encryption_verified": True,
            "package": "django-ray",
            "package_version": version("django-ray"),
        }
    )
    monkeypatch.setattr(
        run_core, "validate_task_status_payload", lambda payload, **kwargs: "SUCCEEDED"
    )
    requests = []

    def request(path, **kwargs):
        requests.append((path, kwargs))
        return 200, json.dumps({"task_id": TASK_ID}).encode()

    assert run_core.verify_runtime_probe(request, token="private", timeout=180) == TASK_ID
    assert requests[0][0] == "/api/cluster/runtime-env/probe?profile=thin&package=django-ray"
    assert requests[1][0] == f"/api/tasks/{TASK_ID}"
    assert requests[1][1]["response_limit"] == 64 * 1024
    assert requests[1][1]["required_response_headers"] == {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


@pytest.mark.parametrize("classified", [False, True, "http"])
def test_core_failure_retains_safe_api_progress_without_private_response(
    tmp_path, monkeypatch, capsys, classified
):
    import django

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(django, "setup", lambda: None)

    def private_failure(*args, **kwargs):
        args[0].requests = 11
        args[0].last_http_status = 200
        kwargs["evidence"].task_id = TASK_ID
        kwargs["evidence"].task_state = "FAILED"
        if classified == "http":
            from qualification.application.run_api import ApplicationHttpError, HttpFailureCode

            raise ApplicationHttpError(
                "private API response", code=HttpFailureCode.BODY_READ, status=200
            )
        if classified:
            raise run_core.CoreEvidenceError(run_core.CoreEvidenceFailure.OWNER_HEARTBEAT)
        raise ValueError("private API response")

    monkeypatch.setattr(run_core, "verify_application_api", private_failure)
    receipt = tmp_path / "core.json"
    assert run_core.main(["--token-file", str(tmp_path / "secret"), "--receipt", str(receipt)]) == 1
    output = capsys.readouterr().out
    assert "private" not in output
    failed = json.loads(output)
    assert failed["failure_code"] == ("owner_heartbeat" if classified is True else "unclassified")
    if classified == "http":
        assert failed["http_failure"] == {"code": "body_read_failed", "status": 200}
    assert failed["failed_stage"] == "application_api"
    assert failed["api_diagnostics"]["requests"] == 11
    assert failed["api_diagnostics"]["last_http_status"] == 200
    assert failed["api_diagnostics"]["observations"]["task_id"] == TASK_ID
    assert failed["api_diagnostics"]["observations"]["task_state"] == "FAILED"
    assert not receipt.exists()


@pytest.mark.parametrize("failure", [False, True])
def test_core_requires_negative_cases_and_retains_partial_safe_progress(
    tmp_path, monkeypatch, capsys, failure
):
    import django

    from qualification.application import (
        dashboard_browser,
        protected_runtime,
        result_presentation,
        runtime_env_refusal,
    )
    from qualification.application.api import ApiEvidence

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(django, "setup", lambda: None)
    monkeypatch.setattr(run_core, "read_token", lambda path: "unprinted-token")
    monkeypatch.setattr(run_core, "verify_application_api", lambda *args, **kwargs: ApiEvidence())
    monkeypatch.setattr(
        run_core, "verify_durable_task", lambda *args, **kwargs: {"execution_id": 1}
    )
    monkeypatch.setattr(run_core, "verify_runtime_probe", lambda *args, **kwargs: TASK_ID)
    monkeypatch.setattr(dashboard_browser, "observe_dashboard", lambda *args: {})
    monkeypatch.setattr(
        run_core,
        "verify_state_api_selection",
        lambda _task_id, *, execution_id: state_selection_record(execution_id),
    )
    monkeypatch.setattr(
        result_presentation,
        "observe_result_presentation",
        lambda **kwargs: dict(result_presentation.EVIDENCE),
    )

    def verify(request, *, evidence, **kwargs):
        for case in runtime_env_refusal.CASES:
            if failure and case == runtime_env_refusal.CASES[1]:
                raise ValueError("unprinted-envelope-and-exception")
            evidence[case] = dict.fromkeys(runtime_env_refusal.FACTS, True)

        return [2, 3]

    monkeypatch.setattr(
        protected_runtime.Capture,
        "finish",
        lambda self, ids: {"api_clear": True, "row_ids": ids, "started_at": self.started_at},
    )
    monkeypatch.setattr(runtime_env_refusal, "verify_refusals", verify)
    path = tmp_path / "receipt.json"
    assert run_core.main(["--token-file", "unused", "--receipt", str(path)]) == int(failure)
    output = capsys.readouterr().out
    assert "unprinted" not in output
    receipt = json.loads(output)
    assert all(receipt["runtime_env_refusals"][runtime_env_refusal.CASES[0]].values())
    if failure:
        assert receipt["status"] == "failed"
        assert receipt["failed_stage"] == "runtime_env_refusal"
        assert not any(receipt["runtime_env_refusals"][runtime_env_refusal.CASES[1]].values())
        assert not path.exists()
    else:
        assert receipt["status"] == "passed"
        from qualification.application import lease_policy, runtime_env_maintenance

        lease_policy.validate_evidence(receipt["lease_policy"])
        runtime_env_maintenance.validate_evidence(receipt["runtime_env_maintenance"])
        runtime_env_refusal.validate_evidence(receipt["runtime_env_refusals"])
        assert (
            json.loads(path.read_bytes())["runtime_env_refusals"] == receipt["runtime_env_refusals"]
        )


@pytest.mark.parametrize("failed", [False, True])
def test_core_requires_result_presentation_before_writing_a_passing_receipt(
    tmp_path, monkeypatch, capsys, failed
):
    from qualification.application import (
        dashboard_browser,
        protected_runtime,
        result_presentation,
        runtime_env_refusal,
    )
    from qualification.application.api import ApiEvidence

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr(run_core, "read_token", lambda _path: "fixed-token")
    monkeypatch.setattr(
        run_core, "verify_application_api", lambda *_a, **_k: ApiEvidence(task_id=TASK_ID)
    )
    monkeypatch.setattr(run_core, "verify_durable_task", lambda *_a, **_k: {"execution_id": 1})
    monkeypatch.setattr(run_core, "verify_runtime_probe", lambda *_a, **_k: TASK_ID)
    monkeypatch.setattr(dashboard_browser, "observe_dashboard", lambda *_a: {})
    monkeypatch.setattr(
        run_core,
        "verify_state_api_selection",
        lambda _task_id, *, execution_id: state_selection_record(execution_id),
    )

    def verify_refusals(request, *, evidence, **kwargs):
        evidence.update(
            {
                case: dict.fromkeys(runtime_env_refusal.FACTS, True)
                for case in runtime_env_refusal.CASES
            }
        )
        return [2, 3]

    monkeypatch.setattr(runtime_env_refusal, "verify_refusals", verify_refusals)
    monkeypatch.setattr(
        protected_runtime.Capture,
        "finish",
        lambda self, ids: {"api_clear": True, "row_ids": ids, "started_at": self.started_at},
    )

    def observe(*, token):
        assert token == "fixed-token"
        if failed:
            raise ValueError("private result response must not be emitted")
        return dict(result_presentation.EVIDENCE)

    monkeypatch.setattr(result_presentation, "observe_result_presentation", observe)
    path = tmp_path / "core.json"
    assert run_core.main(["--token-file", "unused", "--receipt", str(path)]) == int(failed)
    output = capsys.readouterr().out
    assert "private result response" not in output
    receipt = json.loads(output)
    assert path.exists() is not failed
    if failed:
        assert receipt["failed_stage"] == "ordinary_result_presentation"
        assert "ordinary_results" not in receipt
    else:
        result_presentation.validate_evidence(receipt["ordinary_results"])


@pytest.mark.parametrize("stage", ["lease_policy", "runtime_env_maintenance"])
def test_operational_observer_failure_stops_core_and_retains_only_fixed_facts(
    tmp_path, monkeypatch, capsys, stage
):
    from qualification.application import (
        dashboard_browser,
        lease_policy,
        runtime_env_maintenance,
        runtime_env_refusal,
    )
    from qualification.application.api import ApiEvidence
    from qualification.application.safe_diagnostics import lines

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr(run_core, "read_token", lambda _path: "fixed-token")
    monkeypatch.setattr(
        run_core, "verify_application_api", lambda *_a, **_k: ApiEvidence(task_id=TASK_ID)
    )
    monkeypatch.setattr(run_core, "verify_durable_task", lambda *_a, **_k: {"execution_id": 1})
    monkeypatch.setattr(run_core, "verify_runtime_probe", lambda *_a, **_k: TASK_ID)
    monkeypatch.setattr(dashboard_browser, "observe_dashboard", lambda *_a: {})
    monkeypatch.setattr(
        run_core,
        "verify_state_api_selection",
        lambda _task_id, *, execution_id: state_selection_record(execution_id),
    )
    monkeypatch.setattr(runtime_env_refusal, "verify_refusals", lambda *_a, **_k: [2, 3])

    def fail(*args, evidence, **kwargs):
        if stage == "lease_policy":
            assert kwargs["task_id"] == TASK_ID
            evidence["authority_matched"] = True
        else:
            assert kwargs["refusal_ids"] == [2, 3]
            evidence["scan"]["bounded_inventory"] = True
        raise ValueError("private operational error must be discarded")

    module, function = (
        (lease_policy, "verify_lease_policy")
        if stage == "lease_policy"
        else (runtime_env_maintenance, "verify_maintenance")
    )
    monkeypatch.setattr(module, function, fail)
    path = tmp_path / "core.json"
    assert run_core.main(["--token-file", "unused", "--receipt", str(path)]) == 1
    output = capsys.readouterr().out
    assert "private operational error" not in output
    assert not path.exists()
    receipt = json.loads(output)
    assert receipt["failed_stage"] == stage
    assert lines(output.encode())[0]["failed_stage"] == stage
    with pytest.raises(ValueError):
        module.validate_evidence(receipt[stage])


def state_selection_record(execution_id=5):
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "query_count": 4,
        "explicit_api_server_address": True,
        "explicit_ray_address": True,
        "configured_api_server_address": True,
        "configured_ray_address": True,
    }


@pytest.fixture
def state_selection_seams(monkeypatch):
    from django_ray import observability
    from qualification.application import http_context

    row = SimpleNamespace(
        pk=5,
        task_id=TASK_ID,
        state="SUCCEEDED",
        attempt_number=1,
        execution_protocol_version=1,
        ray_job_id="a" * 8 + ":" + "b" * 48,
    )
    config = {"RAY_STATE_API_ADDRESS": "http://ray-head:8265", "RAY_STATE_API_TIMEOUT_SECONDS": 5}
    clock = [1.0]
    monkeypatch.setattr(run_core.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http_context, "local_outer_deadline", lambda: 100.0)
    monkeypatch.setattr(ray_settings, "get_settings", lambda: config)
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **kwargs: row)
    calls = []

    def getter(task_id, **kwargs):
        import os

        calls.append(
            (
                task_id,
                kwargs,
                {
                    key: os.environ[key]
                    for key in ("RAY_API_SERVER_ADDRESS", "RAY_ADDRESS")
                    if key in os.environ
                },
            )
        )
        return [{"task_id": "b" * 48, "job_id": "a" * 8, "state": "FINISHED", "attempt_number": 0}]

    monkeypatch.setattr(observability, "get_ray_task_state", getter)
    return row, config, clock, calls, getter


@pytest.mark.parametrize(
    "original",
    [
        {},
        {"RAY_ADDRESS": ""},
        {
            "RAY_API_SERVER_ADDRESS": "original-api",
            "RAY_ADDRESS": "original-ray",
        },
    ],
)
def test_state_selection_exercises_four_unmasked_cases_and_restores_exact_environment(
    monkeypatch, state_selection_seams, original
):
    import os

    from qualification.application.api import validate_state_api_selection

    _row, _config, _clock, calls, _getter = state_selection_seams
    keys = ("RAY_API_SERVER_ADDRESS", "RAY_ADDRESS")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    evidence = run_core.verify_state_api_selection(TASK_ID, execution_id=5)
    assert evidence == state_selection_record()
    validate_state_api_selection(evidence, execution_id=5)
    wrong = "http://ray-head:8265/__django_ray_qualification_unselected_state_api__"
    assert calls == [
        ("b" * 48, {"address": "http://ray-head:8265"}, {keys[0]: wrong}),
        ("b" * 48, {"address": "http://ray-head:8265"}, {keys[1]: wrong}),
        ("b" * 48, {}, {keys[0]: wrong}),
        ("b" * 48, {}, {keys[1]: wrong}),
    ]
    assert {key: os.environ[key] for key in keys if key in os.environ} == original


@pytest.mark.parametrize("failure_index", range(4))
def test_state_selection_restores_both_address_keys_when_each_lookup_fails(
    monkeypatch, state_selection_seams, failure_index
):
    import os

    from django_ray import observability

    _row, _config, _clock, calls, getter = state_selection_seams
    monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "")
    monkeypatch.setenv("RAY_ADDRESS", "original-ray")

    def fail(task_id, **kwargs):
        result = getter(task_id, **kwargs)
        if len(calls) - 1 == failure_index:
            raise RuntimeError("private-sdk-response-canary")
        return result

    monkeypatch.setattr(observability, "get_ray_task_state", fail)
    with pytest.raises(run_core.CoreEvidenceError) as caught:
        run_core.verify_state_api_selection(TASK_ID, execution_id=5)
    assert caught.value.code is run_core.CoreEvidenceFailure.STATE_API_SELECTION
    assert str(caught.value) == "state_api_selection"
    assert len(calls) == failure_index + 1
    assert (
        os.environ["RAY_API_SERVER_ADDRESS"] == "" and os.environ["RAY_ADDRESS"] == "original-ray"
    )


@pytest.mark.parametrize("failure_index", range(4))
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "wrong_task",
        "wrong_job",
        "not_finished",
        "bool_attempt",
        "second_attempt",
    ],
)
def test_state_selection_rejects_wrong_or_untyped_actual_getter_results(
    monkeypatch, state_selection_seams, failure, failure_index
):
    import os

    from django_ray import observability

    _row, _config, _clock, calls, getter = state_selection_seams
    monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "original-api")
    monkeypatch.setenv("RAY_ADDRESS", "")

    def changed(task_id, **kwargs):
        values = getter(task_id, **kwargs)
        if len(calls) - 1 != failure_index:
            return values
        if failure == "missing":
            return []
        if failure == "duplicate":
            return values * 2
        value = values[0]
        value.update(
            {
                "wrong_task": {"task_id": "c" * 48},
                "wrong_job": {"job_id": "c" * 8},
                "not_finished": {"state": "RUNNING"},
                "bool_attempt": {"attempt_number": False},
                "second_attempt": {"attempt_number": 1},
            }[failure]
        )
        return values

    monkeypatch.setattr(observability, "get_ray_task_state", changed)
    with pytest.raises(run_core.CoreEvidenceError) as caught:
        run_core.verify_state_api_selection(TASK_ID, execution_id=5)
    assert caught.value.code is run_core.CoreEvidenceFailure.STATE_API_SELECTION
    assert len(calls) == failure_index + 1
    assert (
        os.environ["RAY_API_SERVER_ADDRESS"] == "original-api" and os.environ["RAY_ADDRESS"] == ""
    )


@pytest.mark.parametrize(
    "failure",
    [
        "owner",
        "task",
        "handle",
        "state",
        "attempt",
        "protocol",
        "endpoint",
        "timeout",
        "no_deadline",
        "late",
    ],
)
def test_state_selection_refuses_bad_preconditions_before_any_getter_call(
    monkeypatch, state_selection_seams, failure
):
    from qualification.application import http_context

    row, config, clock, calls, _getter = state_selection_seams
    if failure in {"owner", "task", "handle", "state", "attempt", "protocol"}:
        field, value = {
            "owner": ("pk", 6),
            "task": ("task_id", "another-task"),
            "handle": ("ray_job_id", "a:b"),
            "state": ("state", "RUNNING"),
            "attempt": ("attempt_number", 2),
            "protocol": ("execution_protocol_version", 2),
        }[failure]
        setattr(row, field, value)
    elif failure == "endpoint":
        config["RAY_STATE_API_ADDRESS"] = "http://127.0.0.1:8765/ray"
    elif failure == "timeout":
        config["RAY_STATE_API_TIMEOUT_SECONDS"] = True
    elif failure == "no_deadline":
        monkeypatch.setattr(http_context, "local_outer_deadline", lambda: None)
    else:
        clock[0] = 96
    with pytest.raises(run_core.CoreEvidenceError):
        run_core.verify_state_api_selection(TASK_ID, execution_id=5)
    assert calls == []


def test_state_selection_cannot_accept_a_reply_after_the_original_deadline(
    monkeypatch, state_selection_seams
):
    from django_ray import observability

    _row, _config, clock, calls, getter = state_selection_seams

    def late(task_id, **kwargs):
        result = getter(task_id, **kwargs)
        clock[0] = 100
        return result

    monkeypatch.setattr(observability, "get_ray_task_state", late)
    with pytest.raises(run_core.CoreEvidenceError):
        run_core.verify_state_api_selection(TASK_ID, execution_id=5)
    assert len(calls) == 1
