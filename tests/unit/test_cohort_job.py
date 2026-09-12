from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import django_ray
from django_ray.runtime import cohort_job as job
from django_ray.target.attestation import (
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    encode_ray_cluster_attestation,
)
from django_ray.target.cohort_probe import derive_cohort_target_key

NOW = datetime(2026, 9, 12, tzinfo=UTC)
NODE = "1" * 56
SESSION = "session_jobs_discovered"
NATIVE_JOB_ID = "01000000"


def runtime():
    return RayRuntimeVersion(
        2,
        58,
        0,
        platform.python_implementation().lower(),
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )


def request(**changes):
    return replace(
        job.CohortProbeJobRequest(
            challenge_id=11,
            challenge_revision=2,
            lease=job.CohortProbeJobLease(
                "worker-one", "manager-one", 456, NOW - timedelta(seconds=10)
            ),
            configuration_digest="sha256:" + "b" * 64,
            target_key=None,
            runner_family=RayRunnerFamily.RAY_JOB,
            expected_package_version=django_ray.__version__,
            expected_runtime=runtime(),
            expected_cluster_session=None,
            expected_target_policy_id=None,
            policy_revision=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=300),
        ),
        **changes,
    )


def environment(value):
    return {
        "RAY_ADDRESS": "10.0.0.2:6379",
        "RAY_JOB_CONFIG_JSON_ENV_VAR": json.dumps(
            {
                "runtime_env": {},
                "metadata": job.probe_job_metadata(value)
                | {
                    "job_name": "probe",
                    "job_submission_id": job.probe_job_submission_id(value),
                },
            }
        ),
    }


def collect(value=None, **changes):
    value = value or request()
    arguments = {
        "jobs_submission_id": job.probe_job_submission_id(value),
        "jobs_metadata": job.probe_job_metadata(value),
        "environment": environment(value),
    }
    return job.collect_verified_probe(job.encode_probe_job_request(value), **(arguments | changes))


def attestation(value=None, *, session=SESSION, observed_at=NOW, **changes):
    value = value or request()
    expected = RayTargetExpectation(
        value.target_key
        if value.target_key is not None
        else derive_cohort_target_key(value.runner_family, session),
        value.runner_family,
        session,
        value.policy_revision,
        value.expected_runtime,
    )
    return build_ray_cluster_attestation(
        expectation=replace(expected, **changes),
        boundary=build_ray_observation_boundary(
            resource_state_version_before=1,
            resource_state_version_after=2,
            node_state_versions_before=(RayNodeStateVersion(NODE, 1),),
            node_state_versions_after=(RayNodeStateVersion(NODE, 2),),
        ),
        nodes=(
            build_ray_node_observation(
                node_id=NODE, cluster_session=session, runtime=expected.runtime
            ),
        ),
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=30),
    )


@pytest.fixture
def driver(monkeypatch):
    ray = ModuleType("ray")
    ray.__version__ = "2.58.0"
    state = SimpleNamespace(
        initialized=False,
        init_calls=[],
        shutdown_calls=0,
        observations=[],
        result=attestation(),
        error=None,
        native_job_id=NATIVE_JOB_ID,
        context_calls=0,
    )

    def initialize(**kwargs):
        state.init_calls.append(kwargs)
        state.initialized = True

    def shutdown():
        state.shutdown_calls += 1
        state.initialized = False

    def observe(**kwargs):
        state.observations.append(kwargs)
        assert state.initialized
        if state.error:
            raise state.error
        return state.result

    def runtime_context():
        assert state.initialized
        state.context_calls += 1
        return SimpleNamespace(get_job_id=lambda: state.native_job_id)

    ray.is_initialized = lambda: state.initialized
    ray.init = initialize
    ray.shutdown = shutdown
    ray.get_runtime_context = runtime_context
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.setattr(job, "_now", lambda: NOW)
    monkeypatch.setattr("django_ray.target.cohort_probe.observe_current_cohort_target", observe)
    return ray, state


def assert_refusal(error, reason):
    assert error.value.reason is reason
    assert str(error.value) == f"Cohort probe Job refused: {reason.value}"
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None


def test_first_discovery_returns_only_a_request_bound_typed_proof(driver, capsys):
    _ray, state = driver
    value = request()
    proof = collect(value)
    assert proof.request == value
    assert proof.request_digest == job.probe_job_request_digest(value)
    assert proof.submission_id == job.probe_job_submission_id(value)
    assert proof.native_job_id == NATIVE_JOB_ID
    assert proof.observed_package_version == django_ray.__version__
    assert proof.collected_at == NOW
    assert proof.attestation.expectation.cluster_session == SESSION
    assert state.init_calls == [{"address": "10.0.0.2:6379", "log_to_driver": False}]
    assert state.shutdown_calls == 1
    assert state.context_calls == 1
    assert len(state.observations) == 1
    assert state.observations[0]["expected_cluster_session"] is None
    assert state.observations[0]["timeout_seconds"] == 30.0
    assert state.observations[0]["max_nodes"] == 64
    assert not capsys.readouterr().out
    assert not hasattr(value, "nonce")
    assert "nonce" not in json.loads(job.encode_probe_job_request(value))


@pytest.mark.parametrize(
    "native_id",
    [None, True, 1, b"01000000", "", "AB000000", "010000000", "ffffffff", "g1000000"],
)
def test_invalid_actual_native_job_id_refuses_after_owned_cleanup(driver, native_id):
    _ray, state = driver
    state.native_job_id = native_id
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, job.CohortProbeJobReason.PROBE_FAILED)
    assert state.shutdown_calls == 1
    assert state.context_calls == 1


def test_native_job_id_comes_from_connected_runtime_not_injected_metadata(driver):
    _ray, state = driver
    env = environment(request())
    config = json.loads(env["RAY_JOB_CONFIG_JSON_ENV_VAR"])
    config["metadata"]["job_id"] = "02000000"
    config["metadata"]["native_job_id"] = "03000000"
    env["RAY_JOB_CONFIG_JSON_ENV_VAR"] = json.dumps(config)
    proof = collect(environment=env)
    assert proof.native_job_id == NATIVE_JOB_ID
    assert state.context_calls == 1
    assert state.initialized is False


def test_native_job_context_failure_is_redacted_and_cleans_owned_connection(driver):
    ray, state = driver

    def failed_context():
        assert state.initialized
        raise RuntimeError("private-value")

    ray.get_runtime_context = failed_context
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, job.CohortProbeJobReason.PROBE_FAILED)
    assert state.shutdown_calls == 1


def test_collected_proof_can_be_encoded_as_pending_receipt(driver):
    from django_ray.target.cohort_job_receipt import (
        cohort_job_receipt_from_probe,
        decode_cohort_job_receipt,
        encode_cohort_job_receipt,
    )

    proof = collect()
    receipt = cohort_job_receipt_from_probe(proof)
    assert receipt.native_job_id == NATIVE_JOB_ID
    assert receipt.attestation == proof.attestation
    assert decode_cohort_job_receipt(encode_cohort_job_receipt(receipt)) == receipt


def test_refresh_preserves_independent_session_policy_binding(driver):
    _ray, state = driver
    value = request(
        target_key="jobs-first",
        expected_cluster_session=SESSION,
        expected_target_policy_id=9,
        policy_revision=4,
    )
    state.result = attestation(value)
    assert collect(value).request.expected_target_policy_id == 9
    assert state.observations[0]["expected_cluster_session"] == SESSION
    assert state.observations[0]["policy_revision"] == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"configuration_digest": "sha256:" + "c" * 64},
        {
            "target_key": "another-endpoint",
            "expected_cluster_session": SESSION,
            "expected_target_policy_id": 9,
        },
        {"challenge_id": 12},
        {"challenge_revision": 3},
        {"lease": replace(request().lease, worker_id="different-worker")},
        {"lease": replace(request().lease, hostname="different-host")},
        {"lease": replace(request().lease, pid=999)},
        {"lease": replace(request().lease, started_at=NOW - timedelta(seconds=9))},
        {"expires_at": NOW + timedelta(seconds=301)},
    ],
)
def test_every_endpoint_and_challenge_binding_changes_submission_identity(changes):
    original, changed = request(), request(**changes)
    assert job.probe_job_request_digest(original) != job.probe_job_request_digest(changed)
    assert job.probe_job_submission_id(original) != job.probe_job_submission_id(changed)


def test_packet_for_another_jobs_endpoint_cannot_reuse_independent_metadata(driver):
    _ray, state = driver
    value = request(configuration_digest="sha256:" + "c" * 64)
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(value, jobs_metadata=job.probe_job_metadata(request()))
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert state.init_calls == []


@pytest.mark.parametrize(
    "key",
    [job.COHORT_PROBE_JOB_METADATA_KIND, job.COHORT_PROBE_JOB_METADATA_DIGEST],
)
@pytest.mark.parametrize("source", ["independent", "injected"])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_wrong_independent_and_injected_metadata_refuses_before_ray(
    driver, key, source, missing
):
    _ray, state = driver
    metadata = job.probe_job_metadata(request())
    if missing:
        metadata.pop(key)
    else:
        metadata[key] = "private-value"
    changes = {"jobs_metadata": metadata}
    if source == "injected":
        env = environment(request())
        metadata["job_submission_id"] = job.probe_job_submission_id(request())
        env["RAY_JOB_CONFIG_JSON_ENV_VAR"] = json.dumps({"runtime_env": {}, "metadata": metadata})
        changes = {"environment": env}
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(**changes)
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert state.init_calls == []
    assert state.shutdown_calls == 0


@pytest.mark.parametrize(
    "value",
    [None, "{}", "null", "{", "x" * 65537, '{"metadata":{},"metadata":{}}'],
    ids=["missing", "empty", "null", "malformed", "oversized", "duplicate"],
)
def test_missing_malformed_or_oversized_jobs_injected_config_refuses(driver, value):
    _ray, state = driver
    env = environment(request())
    env["RAY_JOB_CONFIG_JSON_ENV_VAR"] = value
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(environment=env)
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert state.init_calls == []


@pytest.mark.parametrize(
    "address",
    [
        None,
        "",
        "auto",
        "local",
        "http://head:8265",
        "https://head:8265",
        "ray://head:10001",
        "head",
        "head:0",
        "head:65536",
        "head:6379/path",
        "user:secret@head:6379",
        "head:6379?token=private",
        "head:6379?",
        "head:6379#",
        " head:6379",
        "head:6379\n",
    ],
)
def test_requires_explicit_jobs_gcs_address_not_manager_or_client_endpoint(driver, address):
    _ray, state = driver
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(environment=environment(request()) | {"RAY_ADDRESS": address})
    assert_refusal(error, job.CohortProbeJobReason.INVALID_GCS_ADDRESS)
    assert state.init_calls == []


@pytest.mark.parametrize("address", ["ray-head.private:6379", "127.0.0.1:6379", "[::1]:6379"])
def test_passes_exact_resolved_gcs_endpoint_to_owned_connection(driver, address):
    _ray, state = driver
    collect(environment=environment(request()) | {"RAY_ADDRESS": address})
    assert state.init_calls[0]["address"] == address


@pytest.mark.parametrize(
    "field,value",
    [
        ("ray_minor", 57),
        ("ray_patch", 1),
        ("python_implementation", "pypy"),
        ("python_minor", 99),
        ("python_patch", 999),
    ],
)
def test_actual_tuple_mismatch_refuses_before_ray_init(driver, field, value):
    _ray, state = driver
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(request(expected_runtime=replace(runtime(), **{field: value})))
    assert_refusal(error, job.CohortProbeJobReason.RUNTIME_MISMATCH)
    assert state.init_calls == []


def test_package_mismatch_precedes_even_ray_import(driver, monkeypatch):
    _ray, state = driver
    monkeypatch.setitem(sys.modules, "ray", None)
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(request(expected_package_version="99.0.0"))
    assert_refusal(error, job.CohortProbeJobReason.RUNTIME_MISMATCH)
    assert state.init_calls == []


def test_matching_unqualified_ray_release_still_refuses_before_init(driver):
    ray, state = driver
    ray.__version__ = "2.59.0"
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(request(expected_runtime=replace(runtime(), ray_minor=59)))
    assert_refusal(error, job.CohortProbeJobReason.RUNTIME_MISMATCH)
    assert state.init_calls == []


def test_existing_ray_connection_is_neither_reused_nor_shut_down(driver):
    _ray, state = driver
    state.initialized = True
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, job.CohortProbeJobReason.EXISTING_CONNECTION)
    assert state.init_calls == []
    assert state.shutdown_calls == 0


@pytest.mark.parametrize("initialized", [None, 0, 1, "", [], {}])
def test_non_boolean_initialized_observation_fails_closed(driver, initialized):
    _ray, state = driver
    state.initialized = initialized
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, job.CohortProbeJobReason.RUNTIME_UNAVAILABLE)
    assert state.init_calls == []
    assert state.shutdown_calls == 0


def test_custom_metadata_never_overrides_ray_reserved_submission_id():
    assert set(job.probe_job_metadata(request())) == {
        job.COHORT_PROBE_JOB_METADATA_KIND,
        job.COHORT_PROBE_JOB_METADATA_DIGEST,
    }


@pytest.mark.parametrize("injected_id", [None, "another-physical-job", "expected"])
def test_actual_physical_job_mismatch_refuses_even_with_valid_custom_metadata(driver, injected_id):
    _ray, state = driver
    env = environment(request())
    config = json.loads(env["RAY_JOB_CONFIG_JSON_ENV_VAR"])
    if injected_id is None:
        config["metadata"].pop("job_submission_id")
    elif injected_id != "expected":
        config["metadata"]["job_submission_id"] = injected_id
    env["RAY_JOB_CONFIG_JSON_ENV_VAR"] = json.dumps(config)
    # Even a reserved metadata echo of the expected ID is insufficient when
    # the independently held Jobs API record identifies a different real Job.
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(environment=env, jobs_submission_id="another-physical-job")
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert state.init_calls == []


@pytest.mark.parametrize("injected_id", [None, "another-physical-job"])
def test_driver_reserved_id_must_match_actual_independent_job_record(driver, injected_id):
    _ray, state = driver
    env = environment(request())
    config = json.loads(env["RAY_JOB_CONFIG_JSON_ENV_VAR"])
    if injected_id is None:
        config["metadata"].pop("job_submission_id")
    else:
        config["metadata"]["job_submission_id"] = injected_id
    env["RAY_JOB_CONFIG_JSON_ENV_VAR"] = json.dumps(config)
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(environment=env)
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert state.init_calls == []


def test_user_metadata_cannot_spoof_reserved_physical_id(driver):
    _ray, state = driver
    metadata = job.probe_job_metadata(request()) | {
        "job_submission_id": job.probe_job_submission_id(request()),
    }
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(jobs_metadata=metadata)
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert state.init_calls == []


@pytest.mark.parametrize(
    "offset,reason",
    [(-1, job.CohortProbeJobReason.NOT_YET_VALID), (300, job.CohortProbeJobReason.EXPIRED)],
)
def test_challenge_window_checked_before_connecting(driver, monkeypatch, offset, reason):
    _ray, state = driver
    monkeypatch.setattr(job, "_now", lambda: NOW + timedelta(seconds=offset))
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, reason)
    assert state.init_calls == []


@pytest.mark.parametrize(
    "moments,reason",
    [
        ([0, 300], job.CohortProbeJobReason.EXPIRED),
        ([0, 0, 300], job.CohortProbeJobReason.EXPIRED),
        ([0, 0, 0, 300], job.CohortProbeJobReason.EXPIRED),
        ([1, 0], job.CohortProbeJobReason.CLOCK_REGRESSION),
        ([0, 1, 0], job.CohortProbeJobReason.CLOCK_REGRESSION),
    ],
)
def test_no_proof_if_init_observation_or_cleanup_outlives_deadline_or_clock_regresses(
    driver, monkeypatch, moments, reason
):
    _ray, state = driver
    times = iter(NOW + timedelta(seconds=offset) for offset in moments)
    monkeypatch.setattr(job, "_now", lambda: next(times))
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, reason)
    assert state.shutdown_calls == 1


def test_collector_deadline_is_capped_by_remaining_challenge(driver, monkeypatch):
    _ray, state = driver
    times = iter(
        [
            NOW,
            NOW + timedelta(seconds=290),
            NOW + timedelta(seconds=291),
            NOW + timedelta(seconds=291),
        ]
    )
    monkeypatch.setattr(job, "_now", lambda: next(times))
    state.result = attestation(observed_at=NOW + timedelta(seconds=290))
    collect()
    assert state.observations[0]["timeout_seconds"] == 10.0


@pytest.mark.parametrize(
    "failure", ["collector", "foreign_target", "foreign_session", "malformed", "stale"]
)
def test_failed_or_rebound_observation_never_returns_positive_proof(driver, failure):
    _ray, state = driver
    value = request()
    if failure == "collector":
        state.error = RuntimeError("private-value")
    elif failure == "foreign_target":
        state.result = attestation(target_key="another-target")
    elif failure == "foreign_session":
        value = request(
            target_key="jobs-first",
            expected_cluster_session="session_expected",
            expected_target_policy_id=3,
        )
    elif failure == "malformed":
        state.result = {"ok": True}
    else:
        state.result = attestation(observed_at=NOW - timedelta(seconds=1))
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(value)
    assert_refusal(error, job.CohortProbeJobReason.PROBE_FAILED)
    assert state.shutdown_calls == 1


@pytest.mark.parametrize("operation", ["init", "shutdown"])
def test_connection_errors_are_redacted_and_cannot_be_proof(driver, operation):
    ray, state = driver

    def fail(**kwargs):
        raise RuntimeError("private-value")

    setattr(ray, operation, fail)
    with pytest.raises(job.CohortProbeJobError) as error:
        collect()
    assert_refusal(error, job.CohortProbeJobReason.PROBE_FAILED)
    if operation == "init":
        assert state.shutdown_calls == 1


def test_canonical_roundtrip_exact_bounds_and_request_not_in_errors():
    value = request()
    encoded = job.encode_probe_job_request(value)
    assert job.decode_probe_job_request(encoded) == value
    assert job.decode_probe_job_request(encoded, max_bytes=len(encoded)) == value
    with pytest.raises(job.CohortProbeJobError) as error:
        job.decode_probe_job_request(encoded, max_bytes=len(encoded) - 1)
    assert_refusal(error, job.CohortProbeJobReason.RESOURCE_LIMIT)
    assert encoded not in str(error.value)


@pytest.mark.parametrize("field", ["nonce", "nonce_digest"])
def test_manager_consumption_authority_is_rejected_before_remote_probe(driver, field):
    _ray, state = driver
    value = request()
    wire = json.loads(job.encode_probe_job_request(value))
    # In particular, a previously well-formed nonce-bearing carrier cannot
    # silently regain admission after the manager-only authority boundary.
    wire[field] = "a" * 64
    with pytest.raises(job.CohortProbeJobError) as error:
        job.collect_verified_probe(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            jobs_submission_id=job.probe_job_submission_id(value),
            jobs_metadata=job.probe_job_metadata(value),
            environment=environment(value),
        )
    assert_refusal(error, job.CohortProbeJobReason.INVALID)
    assert wire[field] not in str(error.value)
    assert state.init_calls == []
    assert state.shutdown_calls == 0
    assert state.observations == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("challenge_id", True),
        ("challenge_revision", 0),
        ("runner_family", "ray_core"),
        ("configuration_digest", "sha256:" + "A" * 64),
        ("expected_package_version", "v0.5.0"),
        ("expected_cluster_session", ""),
        ("expected_target_policy_id", 9),
        ("policy_revision", 2),
        ("issued_at", NOW.replace(tzinfo=None).isoformat()),
        (
            "expires_at",
            (NOW + timedelta(seconds=601))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        ),
        ("entrypoint", "private.application.function"),
        ("schema_version", True),
        ("schema_version", 1),
        ("target_key", "caller-selected"),
    ],
)
def test_strict_fixed_request_rejects_malformed_extended_or_unbound_fields(field, value):
    wire = json.loads(job.encode_probe_job_request(request()))
    wire[field] = value
    with pytest.raises(job.CohortProbeJobError):
        job.decode_probe_job_request(json.dumps(wire, sort_keys=True, separators=(",", ":")))


def test_version_one_digest_cannot_identify_a_version_two_discovery(driver):
    _ray, state = driver
    value = request()
    encoded = job.encode_probe_job_request(value)
    assert json.loads(encoded)["schema_version"] == 2
    old_digest = (
        "sha256:"
        + hashlib.sha256(b"django-ray/cohort-probe-job/v1\x00" + encoded.encode()).hexdigest()
    )
    assert job.probe_job_request_digest(value) != old_digest
    old_metadata = job.probe_job_metadata(value) | {
        job.COHORT_PROBE_JOB_METADATA_DIGEST: old_digest
    }
    with pytest.raises(job.CohortProbeJobError) as error:
        collect(value, jobs_metadata=old_metadata)
    assert_refusal(error, job.CohortProbeJobReason.METADATA_MISMATCH)
    assert not state.init_calls


@pytest.mark.parametrize("limit", [0, -1, True, job.COHORT_PROBE_JOB_MAX_BYTES + 1])
def test_packet_limit_can_only_be_lowered(limit):
    with pytest.raises(job.CohortProbeJobError) as error:
        job.encode_probe_job_request(request(), max_bytes=limit)
    assert_refusal(error, job.CohortProbeJobReason.RESOURCE_LIMIT)


@pytest.mark.parametrize(
    "serialized",
    [None, b"{}", "[]", "null", "NaN", "{", "\ud800", " " * (job.COHORT_PROBE_JOB_MAX_BYTES + 1)],
)
def test_decoder_rejects_invalid_or_oversized_input(serialized):
    with pytest.raises(job.CohortProbeJobError):
        job.decode_probe_job_request(serialized)


def test_duplicate_fields_and_noncanonical_json_are_rejected():
    encoded = job.encode_probe_job_request(request())
    for invalid in (
        '{"schema_version":1,' + encoded[1:],
        " " + encoded,
        json.dumps(json.loads(encoded), indent=2),
    ):
        with pytest.raises(job.CohortProbeJobError):
            job.decode_probe_job_request(invalid)


def test_normal_runtime_guard_import_precedes_django_ray_and_application_imports():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
class NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'ray', 'testproject'}:
            raise RuntimeError('forbidden application import')
sys.meta_path.insert(0, NoApplication())
from django_ray.runtime.cohort_job import decode_probe_job_request, CohortProbeJobError
import django_ray.runtime as runtime_package
assert set(runtime_package.__all__).issubset(dir(runtime_package))
try:
    decode_probe_job_request('{}')
except CohortProbeJobError:
    pass
assert 'django_ray.runtime.entrypoint' not in sys.modules
assert 'django_ray.runner.leasing' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_lazy_runtime_package_exports_preserve_original_objects_and_all():
    import django_ray.runtime as runtime_package

    exports = {
        "execute_task": "entrypoint",
        "import_callable": "import_utils",
        "serialize_args": "serialization",
        "deserialize_args": "serialization",
    }
    assert runtime_package.__all__ == list(exports)
    for name, module in exports.items():
        assert getattr(runtime_package, name) is getattr(
            import_module(f"django_ray.runtime.{module}"), name
        )
    with pytest.raises(AttributeError):
        _ = runtime_package.unknown_export


def test_positive_collection_does_not_import_django_or_application_code():
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import json
import sys
from types import ModuleType, SimpleNamespace
class NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'testproject'}:
            raise RuntimeError('forbidden application import')
sys.meta_path.insert(0, NoApplication())
from django_ray.runtime import cohort_job as job
from django_ray.target.attestation import decode_ray_cluster_attestation
request = job.decode_probe_job_request(sys.argv[1])
evidence = decode_ray_cluster_attestation(sys.argv[2])
ray = ModuleType('ray')
ray.__version__ = '2.58.0'
ray.is_initialized = lambda: False
ray.init = lambda **kwargs: None
ray.shutdown = lambda: None
ray.get_runtime_context = lambda: SimpleNamespace(get_job_id=lambda: '01000000')
sys.modules['ray'] = ray
import django_ray.target.cohort_probe as probe
probe.observe_current_cohort_target = lambda **kwargs: evidence
job._now = lambda: request.issued_at
metadata = job.probe_job_metadata(request)
submission_id = job.probe_job_submission_id(request)
injected = metadata | {'job_submission_id': submission_id}
environment = {'RAY_ADDRESS': '10.0.0.2:6379', 'RAY_JOB_CONFIG_JSON_ENV_VAR': json.dumps({'runtime_env': {}, 'metadata': injected})}
proof = job.collect_verified_probe(sys.argv[1], jobs_submission_id=submission_id, jobs_metadata=metadata, environment=environment)
assert proof.attestation == evidence
assert proof.native_job_id == '01000000'
assert 'django_ray.runtime.entrypoint' not in sys.modules
assert 'django_ray.target.cohort_probe_challenges' not in sys.modules
assert 'django_ray.models' not in sys.modules
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            job.encode_probe_job_request(request()),
            encode_ray_cluster_attestation(attestation()),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
