from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from ray.dashboard.modules.job.common import JobStatus
from ray.dashboard.modules.job.pydantic_models import DriverInfo, JobDetails, JobType

from django_ray.runtime import cohort_job as job
from django_ray.runtime import cohort_job_entrypoint as entry
from django_ray.target import cohort_job_http
from django_ray.target.attestation import encode_ray_cluster_attestation
from tests.unit.test_cohort_job import NOW, attestation, environment, request
from tests.unit.test_cohort_job import driver as driver  # noqa: F401

SETTINGS_MODULE = "tests.cohort_probe_settings"
PROFILE = {
    "working_dir": "gcs://qualified-probe.zip",
    "env_vars": {"DJANGO_SETTINGS_MODULE": SETTINGS_MODULE},
}
BOOTSTRAP = entry._bootstrap_and_write


def launch(**changes):
    value = request()
    return replace(
        entry.CohortProbeJobLaunch(
            request=value,
            request_digest=job.probe_job_request_digest(value),
            jobs_endpoint="http://127.0.0.1:8265",
            submitted_runtime_env_digest=entry.cohort_probe_submitted_runtime_env_digest(PROFILE),
            django_settings_module=SETTINGS_MODULE,
        ),
        **changes,
    )


def argv(value=None):
    return entry.probe_job_launch_entrypoint(value or launch()).split(" ")[-2:]


def details(value, *, native=None):
    assert JobDetails is not None
    return JobDetails(
        type=JobType.SUBMISSION,
        submission_id=job.probe_job_submission_id(value.request),
        status=JobStatus.RUNNING,
        job_id=native,
        metadata=job.probe_job_metadata(value.request),
        entrypoint=entry.probe_job_launch_entrypoint(value),
        runtime_env=PROFILE,
    )


@pytest.fixture
def boundary(monkeypatch, driver):
    ray, driver_state = driver
    value = launch()
    state = SimpleNamespace(
        launch=value,
        ray=ray,
        driver=driver_state,
        queries=[],
        writes=[],
        records=[details(value), details(value, native=driver_state.native_job_id)],
        events=[],
        now=NOW,
    )
    monkeypatch.setattr(entry, "_now", lambda: state.now)
    monkeypatch.setattr(entry, "_ensure_no_django", lambda: state.events.append("no-django"))
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
    for key, item in environment(value.request).items():
        monkeypatch.setenv(key, item)

    def fetch(endpoint, submission_id, *, timeout_seconds):
        assert ray.is_initialized() is False
        if state.queries:
            assert driver_state.shutdown_calls == 1
            assert driver_state.context_calls == 1
        state.events.append("fetch")
        state.queries.append((endpoint, submission_id, timeout_seconds))
        return state.records[len(state.queries) - 1]

    def write(observed_launch, receipt, window, began):
        assert observed_launch == value
        assert len(state.queries) == 2
        assert ray.is_initialized() is False
        state.events.append("bootstrap-write")
        state.writes.append(receipt)

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    monkeypatch.setattr(entry, "_bootstrap_and_write", write)
    return state


def test_launch_roundtrip_and_command_have_only_nonce_free_canonical_bindings():
    value = launch()
    serialized = entry.encode_probe_job_launch(value)
    body = json.loads(serialized)
    assert body["request"] == json.loads(job.encode_probe_job_request(value.request))
    assert set(body) == entry._KEYS
    assert "nonce" not in serialized
    assert "entrypoint_digest" not in serialized
    assert "submission_id" not in body
    assert entry.decode_probe_job_launch(serialized) == value
    assert entry.parse_probe_job_launch_argv(argv(value)) == value
    assert entry.parse_probe_job_launch_argv(tuple(argv(value))) == value
    command = entry.probe_job_launch_entrypoint(value)
    assert command.startswith(
        "python -m django_ray.runtime.cohort_job_entrypoint --probe-launch-b64 "
    )
    assert "=" not in argv(value)[1]
    assert len(command.encode()) < 16 * 1024
    assert value.jobs_endpoint not in repr(value)
    assert SETTINGS_MODULE not in repr(value)


@pytest.mark.parametrize(
    "change",
    [
        {"request_digest": "sha256:" + "0" * 64},
        {"request_digest": True},
        {"submitted_runtime_env_digest": "bad"},
        {"jobs_endpoint": "auto"},
        {"jobs_endpoint": "ray://localhost:10001"},
        {"jobs_endpoint": "http://user:secret@localhost:8265"},
        {"jobs_endpoint": "http://localhost:8265?"},
        {"jobs_endpoint": "http://localhost:8265#"},
        {"jobs_endpoint": "http://localhost:8265/%2e"},
        {"jobs_endpoint": "http://localhost:8265/\\thing"},
        {"jobs_endpoint": "http://localhost:8265/\u00e9"},
        {"django_settings_module": "settings"},
        {"django_settings_module": ".settings"},
        {"django_settings_module": "settings;secret"},
        {"django_settings_module": "tests..settings"},
        {"django_settings_module": "tests.1settings"},
        {"django_settings_module": "tests.settings\n"},
        {"django_settings_module": "tests." + "x" * 250},
        {"django_settings_module": None},
        {"request": replace(request(), challenge_revision=True)},
    ],
)
def test_launch_field_validation_rejects_bad_bindings(change):
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.encode_probe_job_launch(launch(**change))
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("value", [None, {}, SimpleNamespace(), "secret"])
def test_encoder_requires_the_exact_launch_type(value):
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.encode_probe_job_launch(value)


@pytest.mark.parametrize("limit", [True, False, 0, -1, 10_241, 10_240.0, None])
def test_launch_bounds_cannot_be_disabled_or_enlarged(limit):
    for function, value in (
        (entry.encode_probe_job_launch, launch()),
        (entry.decode_probe_job_launch, entry.encode_probe_job_launch(launch())),
    ):
        with pytest.raises(entry.CohortJobEntrypointError) as error:
            function(value, max_bytes=limit)
        assert error.value.reason is entry.CohortJobEntrypointReason.RESOURCE_LIMIT


def test_smaller_launch_byte_bound_is_enforced():
    serialized = entry.encode_probe_job_launch(launch())
    assert entry.decode_probe_job_launch(serialized, max_bytes=len(serialized)) == launch()
    for function, value in (
        (entry.encode_probe_job_launch, launch()),
        (entry.decode_probe_job_launch, serialized),
    ):
        with pytest.raises(entry.CohortJobEntrypointError):
            function(value, max_bytes=len(serialized) - 1)


@pytest.mark.parametrize(
    "serialized",
    [None, b"{}", "null", "[]", "{", "\ud800", " " * 10_241, "[" * 100 + "]" * 100],
)
def test_decoder_rejects_bad_or_unbounded_json(serialized):
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.decode_probe_job_launch(serialized)


@pytest.mark.parametrize("field", sorted(entry._KEYS))
def test_decoder_requires_every_field(field):
    body = json.loads(entry.encode_probe_job_launch(launch()))
    del body[field]
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.decode_probe_job_launch(entry._canonical(body))


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "manager-only-secret"),
        ("schema", "different"),
        ("schema_version", True),
        ("schema_version", 2),
        ("request_digest", "sha256:" + "0" * 64),
        ("request", []),
        ("django_settings_module", "tests.settings\x00"),
    ],
)
def test_decoder_rejects_wrong_schema_and_extra_authority(field, value):
    body = json.loads(entry.encode_probe_job_launch(launch()))
    body[field] = value
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.decode_probe_job_launch(entry._canonical(body))


def test_duplicate_and_noncanonical_fields_and_numbers_are_rejected():
    encoded = entry.encode_probe_job_launch(launch())
    for malformed in (
        '{"schema_version":1,' + encoded[1:],
        encoded.replace('"challenge_id":11', '"challenge_id":11,"challenge_id":11'),
        " " + encoded,
        json.dumps(json.loads(encoded), indent=2),
        encoded.replace('"challenge_id":11', '"challenge_id":11.0'),
        encoded.replace('"challenge_id":11', '"challenge_id":NaN'),
        encoded.replace('"challenge_id":11', '"challenge_id":1e100000'),
        encoded.replace('"challenge_id":11', '"challenge_id":' + "1" * 5000),
    ):
        with pytest.raises(entry.CohortJobEntrypointError):
            entry.decode_probe_job_launch(malformed)


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        "--probe-launch-b64 secret",
        [],
        ["--probe-launch-b64"],
        ["--payload-b64", "secret"],
        ["--probe-launch-b64", "a", "--other"],
        ["--probe-launch-b64", b"secret"],
        ["--probe-launch-b64", ""],
        ["--probe-launch-b64", "a="],
        ["--probe-launch-b64", "a b"],
        ["--probe-launch-b64", "+/"],
        ["--probe-launch-b64", "a"],
        ["--probe-launch-b64", "_w"],
        ["--probe-launch-b64", "a" * (entry._ARGUMENT_MAX_BYTES + 1)],
    ],
)
def test_cli_has_one_exact_bounded_argument_form(arguments):
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.parse_probe_job_launch_argv(arguments)


def test_cli_rejects_nonzero_padding_bits():
    # e30 is canonical base64url for {}; e31 decodes to the same bytes.
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.parse_probe_job_launch_argv(["--probe-launch-b64", "e31"])
    assert error.value.reason is entry.CohortJobEntrypointReason.NONCANONICAL


def test_driver_queries_actual_job_twice_around_owned_probe_cleanup(boundary):
    entry.run_probe_job_launch(boundary.launch)
    assert len(boundary.queries) == 2
    assert all(
        query
        == (
            boundary.launch.jobs_endpoint,
            job.probe_job_submission_id(boundary.launch.request),
            5.0,
        )
        for query in boundary.queries
    )
    assert boundary.driver.init_calls == [{"address": "10.0.0.2:6379", "log_to_driver": False}]
    assert boundary.driver.shutdown_calls == 1
    assert boundary.writes[0].native_job_id == boundary.driver.native_job_id
    assert boundary.events[-1] == "bootstrap-write"


@pytest.mark.parametrize("lookup", [0, 1])
@pytest.mark.parametrize(
    "change",
    [
        {"type": JobType.DRIVER},
        {"status": JobStatus.PENDING},
        {"status": JobStatus.SUCCEEDED},
        {"status": JobStatus.FAILED},
        {"status": JobStatus.STOPPED},
        {"status": "RUNNING"},
        {"submission_id": "forged-submission"},
        {"metadata": None},
        {"metadata": {"job_submission_id": "forged"}},
        {"entrypoint": "python -c secret"},
        {"runtime_env": None},
        {"runtime_env": {"env_vars": {"DJANGO_SETTINGS_MODULE": "other.settings"}}},
    ],
)
def test_each_actual_record_must_match_reserved_controls(boundary, lookup, change):
    boundary.records[lookup] = boundary.records[lookup].model_copy(update=change)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.JOB_MISMATCH
    assert not boundary.writes
    assert boundary.driver.shutdown_calls == lookup


@pytest.mark.parametrize("native", [None, "02000000", "ffffffff"])
def test_second_record_must_retain_collected_native_identity(boundary, native):
    boundary.records[1] = boundary.records[1].model_copy(update={"job_id": native})
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.JOB_MISMATCH
    assert boundary.driver.shutdown_calls == 1
    assert not boundary.writes


def test_second_record_driver_info_cannot_disagree(boundary):
    assert DriverInfo is not None
    boundary.records[1] = boundary.records[1].model_copy(
        update={"driver_info": DriverInfo(id="02000000", node_ip_address="10.0.0.2", pid="12")}
    )
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.run_probe_job_launch(boundary.launch)
    assert not boundary.writes


def test_submitted_profile_must_explicitly_pin_settings_even_if_digest_matches(boundary):
    profile = {"working_dir": "gcs://qualified-probe.zip"}
    value = replace(
        boundary.launch,
        submitted_runtime_env_digest=entry.cohort_probe_submitted_runtime_env_digest(profile),
    )
    boundary.records[0] = details(value).model_copy(update={"runtime_env": profile})
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.run_probe_job_launch(value)
    assert not boundary.driver.init_calls


def test_runtime_env_dependency_delivery_is_not_blanket_banned(boundary, monkeypatch):
    profile = PROFILE | {"pip": ["qualified-dependency==1.0"]}
    value = replace(
        boundary.launch,
        submitted_runtime_env_digest=entry.cohort_probe_submitted_runtime_env_digest(profile),
    )
    boundary.records = [
        details(value, native=native).model_copy(update={"runtime_env": profile})
        for native in (None, boundary.driver.native_job_id)
    ]
    monkeypatch.setattr(
        entry, "_bootstrap_and_write", lambda *args: boundary.writes.append(args[1])
    )
    entry.run_probe_job_launch(value)
    assert boundary.writes


@pytest.mark.parametrize("lookup", [0, 1])
def test_network_uncertainty_never_becomes_a_receipt(boundary, monkeypatch, lookup):
    original = cohort_job_http.fetch_reserved_cohort_job_details

    def fetch(*args, **kwargs):
        if len(boundary.queries) == lookup:
            raise RuntimeError("secret transport error")
        return original(*args, **kwargs)

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.JOB_UNAVAILABLE
    assert "secret" not in str(error.value)
    assert not boundary.writes


def test_fake_record_object_is_not_an_actual_job_details(boundary):
    boundary.records[0] = SimpleNamespace(**boundary.records[0].model_dump())
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.run_probe_job_launch(boundary.launch)
    assert not boundary.driver.init_calls


@pytest.mark.parametrize("setting", [None, "untrusted.settings"])
def test_missing_or_different_environment_settings_refuse_before_network(
    boundary, monkeypatch, setting
):
    if setting is None:
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE")
    else:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", setting)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.SETTINGS_MISMATCH
    assert not boundary.queries


def test_already_imported_django_refuses_before_network(boundary, monkeypatch):
    # Pytest's conftest has already imported Django, even without requesting a DB.
    monkeypatch.setattr(entry, "_ensure_no_django", _ensure_no_django_original)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.DJANGO_ALREADY_IMPORTED
    assert not boundary.queries


_ensure_no_django_original = entry._ensure_no_django


@pytest.mark.parametrize("kind", ["package", "runtime", "unsupported", "connected", "unknown"])
def test_actual_local_runtime_refusal_precedes_network(boundary, monkeypatch, kind):
    value = boundary.launch
    if kind == "package":
        changed = replace(value.request, expected_package_version="99.0.0")
        value = replace(
            value, request=changed, request_digest=job.probe_job_request_digest(changed)
        )
    elif kind == "runtime":
        changed = replace(
            value.request, expected_runtime=replace(value.request.expected_runtime, python_patch=99)
        )
        value = replace(
            value, request=changed, request_digest=job.probe_job_request_digest(changed)
        )
    elif kind == "unsupported":
        boundary.ray.__version__ = "2.56.0"
    elif kind == "connected":
        boundary.driver.initialized = True
    else:
        boundary.driver.initialized = 0
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.run_probe_job_launch(value)
    assert not boundary.queries
    assert not boundary.driver.shutdown_calls


def test_probe_refusal_and_cleanup_failure_cannot_write(boundary):
    boundary.driver.error = ValueError("secret probe failure")
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.PROBE_FAILED
    assert boundary.driver.shutdown_calls == 1
    assert len(boundary.queries) == 1
    assert not boundary.writes


def test_actual_cleanup_must_leave_no_initialized_connection(boundary, monkeypatch):
    monkeypatch.setattr(boundary.ray, "shutdown", lambda: None)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.EXISTING_CONNECTION
    assert len(boundary.queries) == 1
    assert not boundary.writes


@pytest.mark.parametrize("seconds", [-1, 300])
def test_request_window_refuses_before_any_network(boundary, seconds):
    boundary.now = NOW + timedelta(seconds=seconds)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.REQUEST_EXPIRED
    assert not boundary.queries


@pytest.mark.parametrize("seconds", [-1, 30, 300])
def test_clock_or_attestation_expiry_after_http_cannot_write(boundary, monkeypatch, seconds):
    original = cohort_job_http.fetch_reserved_cohort_job_details

    def fetch(*args, **kwargs):
        result = original(*args, **kwargs)
        boundary.now = NOW + timedelta(seconds=seconds)
        return result

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.run_probe_job_launch(boundary.launch)
    assert not boundary.writes


@pytest.mark.parametrize("change", ["future", "different-request"])
def test_collector_value_must_match_launch_and_current_time(boundary, monkeypatch, change):
    collect = job.collect_verified_probe

    def changed(*args, **kwargs):
        proof = collect(*args, **kwargs)
        if change == "future":
            return replace(proof, collected_at=NOW + timedelta(seconds=1))
        other = replace(proof.request, challenge_id=12)
        return replace(
            proof,
            request=other,
            request_digest=job.probe_job_request_digest(other),
            submission_id=job.probe_job_submission_id(other),
        )

    monkeypatch.setattr(job, "collect_verified_probe", changed)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.PROBE_FAILED
    assert len(boundary.queries) == 1
    assert not boundary.writes


def test_environment_drift_during_first_lookup_prevents_probe(boundary, monkeypatch):
    fetch = cohort_job_http.fetch_reserved_cohort_job_details

    def changed(*args, **kwargs):
        record = fetch(*args, **kwargs)
        os.environ["DJANGO_SETTINGS_MODULE"] = "other.settings"
        return record

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", changed)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.SETTINGS_MISMATCH
    assert not boundary.driver.init_calls


def test_late_owned_ray_cleanup_prevents_second_lookup(boundary, monkeypatch):
    shutdown = boundary.ray.shutdown

    def late():
        shutdown()
        boundary.now = NOW + timedelta(seconds=300)

    monkeypatch.setattr(boundary.ray, "shutdown", late)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.REQUEST_EXPIRED
    assert len(boundary.queries) == 1
    assert not boundary.writes


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_invalid_monotonic_clock_is_not_a_deadline(boundary, monkeypatch, value):
    monkeypatch.setattr(entry.time, "monotonic", lambda: value)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(boundary.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.CLOCK_UNAVAILABLE
    assert not boundary.queries


def test_wall_clock_regression_is_detected_by_window(monkeypatch):
    monkeypatch.setattr(entry, "_now", lambda: NOW)
    window = entry._Window(request())
    monkeypatch.setattr(entry, "_now", lambda: NOW - timedelta(microseconds=1))
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        window.check()
    assert error.value.reason is entry.CohortJobEntrypointReason.CLOCK_REGRESSION


@pytest.mark.parametrize("next_time", [-1.0, 300.0])
def test_monotonic_regression_or_deadline_cannot_be_hidden_by_wall_clock(monkeypatch, next_time):
    monkeypatch.setattr(entry, "_now", lambda: NOW)
    monkeypatch.setattr(entry.time, "monotonic", lambda: 0.0)
    window = entry._Window(request())
    monkeypatch.setattr(entry.time, "monotonic", lambda: next_time)
    with pytest.raises(entry.CohortJobEntrypointError):
        window.check()


@pytest.mark.parametrize("clock", [None, datetime(2026, 9, 12), True])
def test_invalid_wall_clock_fails_closed(monkeypatch, clock):
    monkeypatch.setattr(entry, "_now", lambda: clock)
    with pytest.raises(entry.CohortJobEntrypointError):
        entry._Window(request())


@pytest.fixture
def bootstrap(boundary, monkeypatch):
    state = boundary
    state.closed = 0
    state.stored = []
    state.setup_error = None
    state.store_error = None
    state.store_result = True
    state.settings = SimpleNamespace(configured=False)
    state.apps = SimpleNamespace(ready=False, apps_ready=False, models_ready=False, loading=False)
    django = ModuleType("django")

    def setup():
        assert state.driver.shutdown_calls == 1
        assert len(state.queries) == 2
        assert os.environ["DJANGO_SETTINGS_MODULE"] == SETTINGS_MODULE
        state.events.append("django-setup")
        if state.setup_error:
            raise state.setup_error
        state.settings.configured = True
        state.settings.SETTINGS_MODULE = SETTINGS_MODULE
        state.apps.ready = True

    def store(receipt, *, using):
        assert state.apps.ready
        assert using == "default"
        assert state.ray.is_initialized() is False
        state.events.append("receipt-write")
        if state.store_error:
            raise state.store_error
        state.stored.append(receipt)
        return state.store_result

    def close():
        state.events.append("close-db")
        state.closed += 1

    django.setup = setup
    modules = {
        "django": django,
        "django.apps": SimpleNamespace(apps=state.apps),
        "django.conf": SimpleNamespace(settings=state.settings),
        "django.db": SimpleNamespace(connections=SimpleNamespace(close_all=close)),
        "django_ray.target.cohort_job_receipt_storage": SimpleNamespace(
            write_cohort_job_receipt=store
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(entry, "_bootstrap_and_write", BOOTSTRAP)
    return state


@pytest.mark.parametrize("changed", [True, False])
def test_bootstrap_pins_settings_writes_default_receipt_and_closes(bootstrap, changed):
    bootstrap.store_result = changed
    entry.run_probe_job_launch(bootstrap.launch)
    assert len(bootstrap.stored) == 1
    assert bootstrap.closed == 1
    assert bootstrap.events[-3:] == ["django-setup", "receipt-write", "close-db"]


@pytest.mark.parametrize("stage", ["setup", "store"])
def test_bootstrap_or_database_uncertainty_has_fixed_failure_and_closes(bootstrap, stage):
    setattr(bootstrap, f"{stage}_error", RuntimeError("sensitive database details"))
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert "sensitive" not in str(error.value)
    assert not bootstrap.stored
    assert bootstrap.closed == 1


@pytest.mark.parametrize("result", [None, 0, 1, "success"])
def test_only_boolean_write_acknowledgment_is_accepted(bootstrap, result):
    bootstrap.store_result = result
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.RECEIPT_REFUSED
    assert bootstrap.closed == 1


@pytest.mark.parametrize("flag", ["ready", "apps_ready", "models_ready", "loading", "configured"])
def test_preconfigured_django_refuses_setup_and_write(bootstrap, flag):
    setattr(bootstrap.settings if flag == "configured" else bootstrap.apps, flag, True)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.DJANGO_ALREADY_IMPORTED
    assert "django-setup" not in bootstrap.events
    assert not bootstrap.stored


@pytest.mark.parametrize("stage", ["setup", "write", "close"])
def test_late_bootstrap_write_or_close_cannot_report_success(bootstrap, monkeypatch, stage):
    if stage == "setup":
        module = sys.modules["django"]
        attribute = "setup"
    elif stage == "write":
        module = sys.modules["django_ray.target.cohort_job_receipt_storage"]
        attribute = "write_cohort_job_receipt"
    else:
        module = sys.modules["django.db"].connections
        attribute = "close_all"
    original = getattr(module, attribute)

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        bootstrap.now = NOW + timedelta(seconds=300)
        return result

    monkeypatch.setattr(module, attribute, late)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.REQUEST_EXPIRED
    assert bootstrap.closed == 1
    if stage == "setup":
        assert not bootstrap.stored


def test_settings_changed_by_setup_cannot_write(bootstrap, monkeypatch):
    original = sys.modules["django"].setup

    def setup():
        original()
        os.environ["DJANGO_SETTINGS_MODULE"] = "another.settings"

    monkeypatch.setattr(sys.modules["django"], "setup", setup)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.SETTINGS_MISMATCH
    assert not bootstrap.stored


def test_django_cannot_configure_a_different_settings_object(bootstrap, monkeypatch):
    original = sys.modules["django"].setup

    def setup():
        original()
        bootstrap.settings.SETTINGS_MODULE = "other.settings"

    monkeypatch.setattr(sys.modules["django"], "setup", setup)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.SETTINGS_MISMATCH
    assert not bootstrap.stored


def test_database_close_failure_cannot_report_success(bootstrap, monkeypatch):
    def close():
        raise RuntimeError("secret connection failure")

    monkeypatch.setattr(sys.modules["django.db"].connections, "close_all", close)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.RECEIPT_REFUSED
    assert len(bootstrap.stored) == 1


@pytest.mark.parametrize("change", ["settings", "attestation-expired"])
def test_second_lookup_drift_cannot_reach_django_setup(bootstrap, monkeypatch, change):
    fetch = cohort_job_http.fetch_reserved_cohort_job_details

    def changed(*args, **kwargs):
        record = fetch(*args, **kwargs)
        if len(bootstrap.queries) == 2:
            if change == "settings":
                os.environ["DJANGO_SETTINGS_MODULE"] = "other.settings"
            else:
                bootstrap.now = NOW + timedelta(seconds=30)
        return record

    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", changed)
    with pytest.raises(entry.CohortJobEntrypointError):
        entry.run_probe_job_launch(bootstrap.launch)
    assert "django-setup" not in bootstrap.events
    assert not bootstrap.stored


def test_connection_opened_by_application_setup_is_not_owned_or_published(bootstrap, monkeypatch):
    original = sys.modules["django"].setup

    def setup():
        original()
        bootstrap.driver.initialized = True

    monkeypatch.setattr(sys.modules["django"], "setup", setup)
    with pytest.raises(entry.CohortJobEntrypointError) as error:
        entry.run_probe_job_launch(bootstrap.launch)
    assert error.value.reason is entry.CohortJobEntrypointReason.EXISTING_CONNECTION
    assert bootstrap.driver.shutdown_calls == 1
    assert not bootstrap.stored


def test_main_succeeds_without_logs_after_receipt_ack(boundary, capsys):
    assert entry.main(argv(boundary.launch)) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "failure,reason,exit_code",
    [
        (RuntimeError("secret"), "unexpected_failure", 1),
        (SystemExit("secret"), "unexpected_failure", 1),
        (KeyboardInterrupt(), "interrupted", 130),
        (
            entry.CohortJobEntrypointError(entry.CohortJobEntrypointReason.JOB_MISMATCH),
            "job_mismatch",
            1,
        ),
    ],
)
def test_main_redacts_all_failures(boundary, monkeypatch, capsys, failure, reason, exit_code):
    def run(_launch):
        raise failure

    monkeypatch.setattr(entry, "run_probe_job_launch", run)
    assert entry.main(argv(boundary.launch)) == exit_code
    assert capsys.readouterr() == ("", f"Cohort probe driver refused: {reason}\n")


def test_module_entrypoint_refuses_bad_argv_without_application_imports():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "django_ray.runtime.cohort_job_entrypoint", "--secret-argument"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Cohort probe driver refused: invalid_launch\n"


def test_native_fixture_settings_bootstrap_without_opening_ray_or_database(tmp_path):
    from tests.integration.test_task_execution import _native_cohort_environment

    environment = _native_cohort_environment(tmp_path)
    script = """
import os
import django
import ray
django.setup()
from django.conf import settings
from django.db import connections
assert settings.SETTINGS_MODULE == 'cohort_native_fixture.settings'
assert settings.DATABASES['default']['NAME'] == os.environ['COHORT_PROBE_DB']
assert all(connection.connection is None for connection in connections.all(initialized_only=True))
assert ray.is_initialized() is False
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "probe.sqlite3").exists()
    assert not (tmp_path / "bootstrap-marker").exists()


def test_native_fixture_does_not_inherit_sdk_endpoint_or_proxy_overrides(tmp_path, monkeypatch):
    from tests.integration.test_task_execution import _native_cohort_environment

    names = (
        "RAY_API_SERVER_ADDRESS",
        "RAY_ADDRESS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in names:
        monkeypatch.setenv(name, "http://outside-fixture.invalid:8265")
    environment = _native_cohort_environment(tmp_path)
    assert all(name not in environment for name in names)
    assert all(os.environ[name] == "http://outside-fixture.invalid:8265" for name in names)


@pytest.mark.parametrize(
    "terminal", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED, None]
)
def test_native_cleanup_requires_terminal_state_after_stop_ack(monkeypatch, terminal):
    import ray.job_submission

    from tests.integration import test_task_execution as native

    state = SimpleNamespace(now=0.0, stopped=[], polled=[])
    fake_clock = SimpleNamespace(monotonic=lambda: state.now)

    def sleep(seconds):
        state.now += seconds

    def fetch(endpoint, handle, *, timeout_seconds):
        state.polled.append((endpoint, handle, timeout_seconds))
        status = terminal if len(state.polled) > 1 and terminal else JobStatus.RUNNING
        return SimpleNamespace(status=status)

    def client(endpoint):
        return SimpleNamespace(stop_job=lambda handle: state.stopped.append((endpoint, handle)))

    fake_clock.sleep = sleep
    monkeypatch.setattr(native, "time", fake_clock)
    monkeypatch.setattr(ray.job_submission, "JobSubmissionClient", client)
    monkeypatch.setattr(cohort_job_http, "fetch_reserved_cohort_job_details", fetch)
    endpoint, handle = "http://127.0.0.1:8265", job.probe_job_submission_id(request())
    if terminal is None:
        with pytest.raises(AssertionError, match="did not reach terminal"):
            native._native_cohort_stop_owned_job(endpoint, handle)
    else:
        native._native_cohort_stop_owned_job(endpoint, handle)
        assert len(state.polled) == 2
    assert state.stopped == [(endpoint, handle)]
    assert all(item[:2] == (endpoint, handle) for item in state.polled)


def test_fresh_process_launch_import_and_codec_never_import_django_or_ray():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
class Poison(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'ray', 'testproject'}:
            raise RuntimeError('forbidden import')
sys.meta_path.insert(0, Poison())
from django_ray.runtime import cohort_job_entrypoint as entry
launch = entry.decode_probe_job_launch(sys.argv[1])
command = entry.probe_job_launch_entrypoint(launch)
assert entry.parse_probe_job_launch_argv(command.split(' ')[-2:]) == launch
assert 'django_ray.runtime.entrypoint' not in sys.modules
assert 'django_ray.target.cohort_job_receipt_storage' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, entry.encode_probe_job_launch(launch())],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout


def test_fresh_process_two_actual_lookup_boundary_surrounds_cleanup_before_bootstrap():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import json
import os
import sys
from types import SimpleNamespace
from ray.dashboard.modules.job.common import JobStatus
from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType
from django_ray.runtime import cohort_job_entrypoint as entry, cohort_job as job
from django_ray.target import cohort_job_http, cohort_probe
from django_ray.target.attestation import decode_ray_cluster_attestation
import ray
class Poison(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'testproject'}:
            raise RuntimeError('Django before approved bootstrap')
sys.meta_path.insert(0, Poison())
launch = entry.decode_probe_job_launch(sys.argv[1])
evidence = decode_ray_cluster_attestation(sys.argv[2])
profile = json.loads(sys.argv[3])
state = {'connected': False, 'queries': 0, 'cleaned': False, 'written': False}
ray.is_initialized = lambda: state['connected']
def initialize(**kwargs):
    assert state['queries'] == 1
    assert not any(k == 'django' or k.startswith('django.') for k in sys.modules)
    state['connected'] = True
def shutdown():
    state['connected'] = False
    state['cleaned'] = True
ray.init = initialize
ray.shutdown = shutdown
ray.get_runtime_context = lambda: SimpleNamespace(get_job_id=lambda: '01000000')
entry._now = job._now = lambda: launch.request.issued_at
cohort_probe.observe_current_cohort_target = lambda **kwargs: evidence
def fetch(endpoint, handle, **kwargs):
    assert endpoint == launch.jobs_endpoint
    assert handle == job.probe_job_submission_id(launch.request)
    assert not state['connected']
    assert not any(k == 'django' or k.startswith('django.') for k in sys.modules)
    if state['queries']:
        assert state['cleaned']
    state['queries'] += 1
    return JobDetails(type=JobType.SUBMISSION, status=JobStatus.RUNNING,
        submission_id=handle, job_id='01000000' if state['cleaned'] else None,
        metadata=job.probe_job_metadata(launch.request),
        entrypoint=entry.probe_job_launch_entrypoint(launch), runtime_env=profile)
cohort_job_http.fetch_reserved_cohort_job_details = fetch
def bootstrap(checked, receipt, window, began):
    assert checked == launch and state['queries'] == 2
    assert state['cleaned'] and not state['connected']
    assert receipt.native_job_id == '01000000'
    assert 'django_ray.target.cohort_job_receipt_storage' not in sys.modules
    state['written'] = True
entry._bootstrap_and_write = bootstrap
os.environ['DJANGO_SETTINGS_MODULE'] = launch.django_settings_module
os.environ['RAY_ADDRESS'] = '127.0.0.1:6379'
os.environ['RAY_JOB_CONFIG_JSON_ENV_VAR'] = json.dumps({'runtime_env': {}, 'metadata':
    job.probe_job_metadata(launch.request) | {'job_submission_id': job.probe_job_submission_id(launch.request)}})
entry.run_probe_job_launch(launch)
assert state['written']
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            entry.encode_probe_job_launch(launch()),
            encode_ray_cluster_attestation(attestation()),
            json.dumps(PROFILE),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout
