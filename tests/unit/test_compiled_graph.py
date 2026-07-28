"""Tests for the fail-closed Compiled Graph compatibility policy."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from django_ray.runtime import compiled_graph
from django_ray.runtime.compiled_graph import (
    CompiledGraphReason,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
    CompiledGraphUnsupportedError,
    candidate_compiled_graph_runtime_rows,
    evaluate_compiled_graph_support,
    require_compiled_graph_support,
    verified_compiled_graph_capability_rows,
)

PROJECT_ROOT = Path(__file__).parents[2]


def _runtime(
    *,
    ray_version: str | None = "2.56.1",
    python_version: str = "3.12.12",
    operating_system: str = "linux",
    architecture: str = "x86_64",
    python_implementation: str | None = "cpython",
    python_abi: str | None = "cpython-312-x86_64-linux-gnu",
    dependency_profile: str | None = "ray=2.56.1;numpy=2.5.1;pyarrow=absent",
    platform_profile: str | None = "Linux-6.17-x86_64-with-glibc2.39",
    libc_profile: str | None = "glibc-2.39",
    container_profile: str | None = "kubernetes:gha-ubuntu-24.04",
    deployment_profile: str | None = f"sha256:{'a' * 64}",
    shared_memory_profile: str | None = "posix-dev-shm:size=68719476736:mount=tmpfs",
    object_store_profile: str | None = "ray-plasma:memory=2147483648:spill=disabled",
) -> CompiledGraphRuntimeIdentity:
    return CompiledGraphRuntimeIdentity(
        ray_version=ray_version,
        python_version=python_version,
        operating_system=operating_system,
        architecture=architecture,
        python_implementation=python_implementation,
        python_abi=python_abi,
        dependency_profile=dependency_profile,
        platform_profile=platform_profile,
        libc_profile=libc_profile,
        container_profile=container_profile,
        deployment_profile=deployment_profile,
        shared_memory_profile=shared_memory_profile,
        object_store_profile=object_store_profile,
    )


@pytest.mark.parametrize(
    ("topology", "submission_transport"),
    [
        (
            CompiledGraphTopology.DIRECT_DRIVER,
            CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        ),
        (
            CompiledGraphTopology.NESTED_RAY_TASK,
            CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        ),
        (
            CompiledGraphTopology.RAY_JOB_DRIVER,
            CompiledGraphSubmissionTransport.RAY_JOB,
        ),
    ],
)
@pytest.mark.parametrize("ray_version", ["2.53.0", "2.56.0", "2.56.1"])
def test_proposed_linux_cpu_rows_remain_unverified_candidates(
    ray_version: str,
    topology: CompiledGraphTopology,
    submission_transport: CompiledGraphSubmissionTransport,
) -> None:
    decision = evaluate_compiled_graph_support(
        topology,
        submission_transport=submission_transport,
        runtime=_runtime(
            ray_version=ray_version,
            dependency_profile=f"ray={ray_version};numpy=2.5.1;pyarrow=absent",
        ),
    )

    assert decision.eligible is False
    assert decision.candidate is True
    assert decision.verified is False
    assert decision.reason is CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE
    assert decision.plan_rejection_code == "INCOMPATIBLE_PLATFORM"
    assert (decision.capability_set or "").startswith("ray-cgraph-policy-v2:")
    assert decision.submission_transport == submission_transport.value
    serialized = json.loads(json.dumps(decision.asdict()))
    assert serialized["schema_version"] == 2
    assert serialized["policy_version"] == 2
    assert serialized["beta"] is True


def test_exact_reviewed_capability_can_become_eligible(monkeypatch) -> None:
    runtime = _runtime()
    capability = compiled_graph._CapabilityIdentity(
        runtime=runtime,
        topology=CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY,
    )
    monkeypatch.setattr(compiled_graph, "_VERIFIED_CAPABILITIES", frozenset({capability}))

    verified = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=runtime,
    )
    other_topology = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=runtime,
    )

    assert verified.eligible is True
    assert verified.candidate is True
    assert verified.verified is True
    assert verified.reason is CompiledGraphReason.ELIGIBLE
    assert verified.plan_rejection_code is None
    assert other_topology.reason is CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE
    assert verified_compiled_graph_capability_rows() == (
        {
            "ray_version": "2.56.1",
            "python_version": "3.12.12",
            "operating_system": "linux",
            "architecture": "x86_64",
            "python_implementation": "cpython",
            "python_abi": "cpython-312-x86_64-linux-gnu",
            "dependency_profile": "ray=2.56.1;numpy=2.5.1;pyarrow=absent",
            "platform_profile": "Linux-6.17-x86_64-with-glibc2.39",
            "libc_profile": "glibc-2.39",
            "container_profile": "kubernetes:gha-ubuntu-24.04",
            "deployment_profile": f"sha256:{'a' * 64}",
            "shared_memory_profile": "posix-dev-shm:size=68719476736:mount=tmpfs",
            "object_store_profile": "ray-plasma:memory=2147483648:spill=disabled",
            "topology": "nested-ray-task",
            "submission_transport": "direct-ray-core",
            "transport": "cpu-shared-memory",
        },
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python_version", "3.12.13"),
        ("python_abi", "cpython-312d-x86_64-linux-gnu"),
        ("dependency_profile", "ray=2.56.1;numpy=2.5.2;pyarrow=absent"),
        ("platform_profile", "Linux-6.18-x86_64-with-glibc2.39"),
        ("libc_profile", "glibc-2.40"),
        ("container_profile", "kubernetes:gha-ubuntu-24.10"),
        ("deployment_profile", f"sha256:{'b' * 64}"),
        ("shared_memory_profile", "posix-dev-shm:size=8589934592:mount=tmpfs"),
        ("object_store_profile", "ray-plasma:memory=4294967296:spill=disabled"),
    ],
)
def test_near_neighbor_runtime_cannot_inherit_verified_row(
    monkeypatch, field: str, value: str
) -> None:
    runtime = _runtime()
    capability = compiled_graph._CapabilityIdentity(
        runtime=runtime,
        topology=CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY,
    )
    monkeypatch.setattr(compiled_graph, "_VERIFIED_CAPABILITIES", frozenset({capability}))

    neighbor = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=replace(runtime, **{field: value}),
    )

    assert neighbor.eligible is False
    assert neighbor.reason is CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE
    assert neighbor.capability_set != _capability_id(capability)


def test_ray_client_submitted_owner_cannot_inherit_local_nested_row(monkeypatch) -> None:
    runtime = _runtime()
    capability = compiled_graph._CapabilityIdentity(
        runtime=runtime,
        topology=CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY,
    )
    monkeypatch.setattr(compiled_graph, "_VERIFIED_CAPABILITIES", frozenset({capability}))

    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.RAY_CLIENT,
        runtime=runtime,
    )

    assert decision.eligible is False
    assert decision.reason is CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT
    assert decision.plan_rejection_code == "OWNER_LIFETIME_MISMATCH"
    assert decision.capability_set is None


@pytest.mark.parametrize(
    "field",
    [
        "python_implementation",
        "python_abi",
        "dependency_profile",
        "platform_profile",
        "libc_profile",
        "container_profile",
        "deployment_profile",
        "shared_memory_profile",
        "object_store_profile",
    ],
)
def test_missing_exact_context_fails_closed_but_preserves_candidate_discovery(
    field: str,
) -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=replace(_runtime(), **{field: None}),
    )

    assert decision.eligible is False
    assert decision.candidate is True
    assert decision.reason is CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT
    assert field in decision.message
    assert decision.capability_set is None


@pytest.mark.parametrize(
    ("field", "value", "missing_fragment"),
    [
        ("platform_profile", "unknown", "platform_profile"),
        ("libc_profile", "glibc-unknown", "libc_profile"),
        ("container_profile", "host", "container_profile_specificity"),
        ("container_profile", "container", "container_profile_specificity"),
        ("container_profile", "docker", "container_profile_specificity"),
        ("deployment_profile", "release-latest", "deployment_profile_immutable"),
        ("deployment_profile", "unresolved", "deployment_profile"),
        ("shared_memory_profile", "unavailable", "shared_memory_profile"),
        ("object_store_profile", "default", "object_store_profile"),
    ],
)
def test_generic_or_unresolved_context_fails_closed(
    field: str,
    value: str,
    missing_fragment: str,
) -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=replace(_runtime(), **{field: value}),
    )

    assert decision.eligible is False
    assert decision.candidate is True
    assert decision.reason is CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT
    assert missing_fragment in decision.message
    assert decision.capability_set is None


def test_oversized_programmatic_identity_is_rejected_and_digest_normalized() -> None:
    oversized = "\U0001f4a5" * (compiled_graph._MAX_IDENTITY_FIELD_CHARS + 1)
    runtime = CompiledGraphRuntimeIdentity(**dict.fromkeys(_runtime().asdict(), oversized))

    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=runtime,
    )
    serialized = decision.asdict()
    encoded = json.dumps(serialized, ensure_ascii=True, sort_keys=True)

    assert decision.reason is CompiledGraphReason.INVALID_RUNTIME_IDENTITY
    assert "exceed the bounded compatibility-record limit" in decision.message
    assert set(runtime.asdict()) == set(serialized["runtime"])
    assert all(
        value is not None and value.startswith("<oversized:1025:sha256:")
        for value in serialized["runtime"].values()
    )
    assert len(encoded) < 10_000


@pytest.mark.parametrize(
    ("environment_name", "field"),
    [
        (compiled_graph._CONTAINER_PROFILE_ENV, "container_profile"),
        (compiled_graph._DEPLOYMENT_PROFILE_ENV, "deployment_profile"),
        (compiled_graph._SHARED_MEMORY_PROFILE_ENV, "shared_memory_profile"),
        (compiled_graph._OBJECT_STORE_PROFILE_ENV, "object_store_profile"),
    ],
)
def test_oversized_environment_identity_is_rejected_and_bounded(
    monkeypatch,
    environment_name: str,
    field: str,
) -> None:
    monkeypatch.setenv(
        environment_name,
        "x" * (compiled_graph._MAX_IDENTITY_FIELD_CHARS + 1),
    )

    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
    )
    serialized = decision.asdict()

    assert decision.reason is CompiledGraphReason.INVALID_RUNTIME_IDENTITY
    assert field in decision.message
    assert serialized["runtime"][field].startswith("<oversized:1025:sha256:")
    assert len(json.dumps(serialized, ensure_ascii=True, sort_keys=True)) < 10_000


def test_oversized_request_dimensions_and_message_serialize_with_digests() -> None:
    oversized = "x" * (compiled_graph._MAX_DECISION_TEXT_CHARS + 1)

    decision = evaluate_compiled_graph_support(
        oversized,
        oversized,
        submission_transport=oversized,
        runtime=_runtime(),
    )
    decision = replace(decision, message=oversized)
    serialized = decision.asdict()

    assert decision.reason is CompiledGraphReason.UNSUPPORTED_TOPOLOGY
    assert serialized["message"].startswith("<oversized:2049:sha256:")
    assert serialized["topology"].startswith("<oversized:2049:sha256:")
    assert serialized["submission_transport"].startswith("<oversized:2049:sha256:")
    assert serialized["transport"].startswith("<oversized:2049:sha256:")
    assert len(json.dumps(serialized, ensure_ascii=True, sort_keys=True)) < 10_000


def test_missing_submission_transport_fails_closed() -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        runtime=_runtime(),
    )

    assert decision.eligible is False
    assert decision.reason is CompiledGraphReason.UNSUPPORTED_SUBMISSION_TRANSPORT
    assert decision.submission_transport == ""


def test_dependency_profile_must_name_the_exact_ray_release() -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.NESTED_RAY_TASK,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=replace(_runtime(), dependency_profile="ray=2.56.0;numpy=2.5.1"),
    )

    assert decision.eligible is False
    assert decision.reason is CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT
    assert "dependency_profile_ray_version" in decision.message


@pytest.mark.parametrize(
    ("runtime", "reason", "message_fragment"),
    [
        (_runtime(ray_version=None), CompiledGraphReason.RAY_NOT_INSTALLED, "not installed"),
        (
            _runtime(ray_version="development"),
            CompiledGraphReason.INVALID_RUNTIME_IDENTITY,
            "numeric major.minor",
        ),
        (
            _runtime(python_version="development"),
            CompiledGraphReason.INVALID_RUNTIME_IDENTITY,
            "numeric major.minor",
        ),
        (
            _runtime(operating_system="windows"),
            CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM,
            "only Linux",
        ),
        (
            _runtime(architecture="aarch64"),
            CompiledGraphReason.UNSUPPORTED_ARCHITECTURE,
            "only x86_64",
        ),
        (
            _runtime(python_version="3.13.7"),
            CompiledGraphReason.UNSUPPORTED_PYTHON,
            "Python 3.12 only",
        ),
        (
            _runtime(ray_version="2.56.2"),
            CompiledGraphReason.UNSUPPORTED_RAY_VERSION,
            "2.53.0, 2.56.0, 2.56.1",
        ),
        (
            _runtime(ray_version="3.0.0.dev0"),
            CompiledGraphReason.UNSUPPORTED_RAY_VERSION,
            "untested",
        ),
    ],
)
def test_runtime_rejections_are_structured(
    runtime: CompiledGraphRuntimeIdentity,
    reason: CompiledGraphReason,
    message_fragment: str,
) -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=runtime,
    )

    assert decision.eligible is False
    assert decision.reason is reason
    assert decision.plan_rejection_code == "INCOMPATIBLE_PLATFORM"
    assert message_fragment in decision.message
    assert decision.asdict()["capability_set"] is None


@pytest.mark.parametrize(
    "topology",
    [CompiledGraphTopology.RAY_CLIENT_DRIVER, "future-owner", ""],
)
def test_unvalidated_topologies_reject_with_owner_code(topology: str) -> None:
    decision = evaluate_compiled_graph_support(
        topology,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=_runtime(),
    )

    assert decision.reason is CompiledGraphReason.UNSUPPORTED_TOPOLOGY
    assert decision.plan_rejection_code == "OWNER_LIFETIME_MISMATCH"
    assert repr(str(topology)) in decision.message


def test_ray_client_driver_rejection_distinguishes_cluster_side_owner() -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.RAY_CLIENT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.RAY_CLIENT,
        runtime=_runtime(),
    )

    assert "direct compilation by a Ray Client driver" in decision.message
    assert "nested owner submitted through Ray Client" in decision.message
    assert "local nested-owner tuple" in decision.message


@pytest.mark.parametrize("transport", [CompiledGraphTransport.GPU_NCCL, "future-channel"])
def test_unvalidated_transports_reject_with_transport_code(transport: str) -> None:
    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        transport,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=_runtime(),
    )

    assert decision.reason is CompiledGraphReason.UNSUPPORTED_TRANSPORT
    assert decision.plan_rejection_code == "UNSUPPORTED_TRANSPORT"


def test_require_support_returns_only_verified_decision(monkeypatch) -> None:
    runtime = _runtime()
    capability = compiled_graph._CapabilityIdentity(
        runtime=runtime,
        topology=CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY,
    )
    monkeypatch.setattr(compiled_graph, "_VERIFIED_CAPABILITIES", frozenset({capability}))
    eligible = require_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
        runtime=runtime,
    )
    rejected = _runtime(operating_system="windows")

    assert eligible.eligible is True
    with pytest.raises(CompiledGraphUnsupportedError) as raised:
        require_compiled_graph_support(
            CompiledGraphTopology.DIRECT_DRIVER,
            submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
            runtime=rejected,
        )

    assert raised.value.decision.runtime is rejected
    assert str(raised.value).startswith("UNSUPPORTED_OPERATING_SYSTEM:")


def test_require_support_rejects_candidate_without_native_evidence() -> None:
    with pytest.raises(CompiledGraphUnsupportedError) as raised:
        require_compiled_graph_support(
            CompiledGraphTopology.NESTED_RAY_TASK,
            submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
            runtime=_runtime(),
        )

    assert raised.value.decision.reason is CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE


def test_detect_runtime_normalizes_platform_without_importing_ray(monkeypatch) -> None:
    monkeypatch.setattr(compiled_graph.metadata, "version", lambda name: "2.56.1")
    monkeypatch.setattr(compiled_graph.platform, "python_version", lambda: "3.12.9")
    monkeypatch.setattr(compiled_graph.platform, "python_implementation", lambda: "CPython")
    monkeypatch.setattr(compiled_graph.platform, "system", lambda: " Linux ")
    monkeypatch.setattr(compiled_graph.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(compiled_graph.platform, "platform", lambda: "linux-profile")
    monkeypatch.setattr(compiled_graph.sysconfig, "get_config_var", lambda _name: "cp312-abi")
    monkeypatch.setattr(compiled_graph, "_detect_dependency_profile", lambda: "deps")
    monkeypatch.setattr(compiled_graph, "_detect_libc_profile", lambda: "glibc-2.39")
    monkeypatch.setattr(compiled_graph, "_detect_container_profile", lambda: "container")
    monkeypatch.setattr(
        compiled_graph,
        "_detect_deployment_profile",
        lambda: f"sha256:{'a' * 64}",
    )
    monkeypatch.setattr(compiled_graph, "_detect_shared_memory_profile", lambda: "shm")
    monkeypatch.setattr(compiled_graph, "_detect_object_store_profile", lambda: "object-store")

    identity = compiled_graph.detect_compiled_graph_runtime()

    assert identity.asdict() == {
        "ray_version": "2.56.1",
        "python_version": "3.12.9",
        "operating_system": "linux",
        "architecture": "x86_64",
        "python_implementation": "cpython",
        "python_abi": "cp312-abi",
        "dependency_profile": "deps",
        "platform_profile": "linux-profile",
        "libc_profile": "glibc-2.39",
        "container_profile": "container",
        "deployment_profile": f"sha256:{'a' * 64}",
        "shared_memory_profile": "shm",
        "object_store_profile": "object-store",
    }


def test_detect_runtime_handles_missing_ray(monkeypatch) -> None:
    def missing(_name: str) -> str:
        raise compiled_graph.metadata.PackageNotFoundError

    monkeypatch.setattr(compiled_graph.metadata, "version", missing)

    assert compiled_graph.detect_compiled_graph_runtime().ray_version is None


def test_deployment_storage_profiles_require_explicit_configuration(monkeypatch) -> None:
    monkeypatch.delenv(compiled_graph._DEPLOYMENT_PROFILE_ENV, raising=False)
    monkeypatch.delenv(compiled_graph._SHARED_MEMORY_PROFILE_ENV, raising=False)
    monkeypatch.delenv(compiled_graph._OBJECT_STORE_PROFILE_ENV, raising=False)

    assert compiled_graph._detect_deployment_profile() == "unresolved"
    assert compiled_graph._detect_shared_memory_profile() == "unresolved"
    assert compiled_graph._detect_object_store_profile() == "unresolved"


def test_candidate_rows_are_sorted_and_verified_rows_start_empty() -> None:
    rows = candidate_compiled_graph_runtime_rows()

    assert [row["ray_version"] for row in rows] == ["2.53.0", "2.56.0", "2.56.1"]
    assert all(row["python_minor"] == "3.12" for row in rows)
    rows[0]["ray_version"] = "changed"
    assert candidate_compiled_graph_runtime_rows()[0]["ray_version"] == "2.53.0"
    assert verified_compiled_graph_capability_rows() == ()


def test_platform_investigation_retains_exact_measured_outcomes() -> None:
    path = (
        PROJECT_ROOT
        / "docs"
        / "investigations"
        / "compiled-graph-platform-evidence-2026-07-19.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    outcomes = record["outcomes"]

    environments = {environment["id"]: environment for environment in record["environments"]}
    assert environments["windows-local"]["platform"] == {
        "operating_system": "Windows 11",
        "os_build": "26200",
        "architecture": "AMD64",
    }
    assert environments["windows-local"]["python_version"] == "3.12.12"
    assert environments["windows-local"]["parent_timeout_seconds"] == 60
    assert environments["linux-actions-29714241117"]["source_commit"] == (
        "7fc0071f7d06c2ffb0e37ba44f6f3029841873db"
    )
    assert {(outcome["ray_version"], outcome["compiler_owner"]) for outcome in outcomes} == {
        (ray_version, topology)
        for ray_version in ("2.53.0", "2.56.0", "2.56.1")
        for topology in ("direct-driver", "local-nested-owner")
    }
    linux = [outcome for outcome in outcomes if outcome["environment_id"].startswith("linux-")]
    windows = [outcome for outcome in outcomes if outcome["environment_id"] == "windows-local"]
    assert len(linux) == 3
    assert all(outcome["compiler_owner"] == "local-nested-owner" for outcome in linux)
    assert all(outcome["probe_status"] == "success" for outcome in linux)
    assert len(windows) == 6
    assert all(outcome["probe_status"] == "native_crash" for outcome in windows)
    windows_direct = [
        outcome for outcome in windows if outcome["compiler_owner"] == "direct-driver"
    ]
    windows_nested = [
        outcome for outcome in windows if outcome["compiler_owner"] == "local-nested-owner"
    ]
    assert all(outcome["process_exit_code"] == 3221225477 for outcome in windows_direct)
    assert all(outcome["native_exit_code"] == "0xC0000005" for outcome in windows_direct)
    assert all(outcome["error_type"] == "WorkerCrashedError" for outcome in windows_nested)
    assert "ray-client-submitted-nested-owner" in record["unmeasured_capabilities"]
    assert record["policy_effect"] == "none; policy version 2 remains fail-closed"
    assert record["completeness"]["complete"] is False
    assert record["completeness"]["promotion_eligible"] is False
    assert record["completeness"]["follow_up_owner"] == "#100"
    assert record["completeness"]["follow_up_due_on"] == "2026-07-27"
    assert record["completeness"]["blocks_issue_86_or_pr_92"] is False
    assert record["completeness"]["blocks_linux_kubernetes_promotion"] is False
    assert any(
        "Raw Windows stdout and stderr logs were not retained" in limitation
        for limitation in record["completeness"]["limitations"]
    )
    assert record["nightly"]["fresh_evidence_follow_up_on"] == "2026-07-27"
    assert record["nightly"]["follow_up_owner"] == "#100"
    assert record["nightly"]["linux_infrastructure_merge_gate"] is False
    assert record["nightly"]["run"] is False


def test_capability_review_retains_exact_no_promotion_decision() -> None:
    path = (
        PROJECT_ROOT
        / "docs"
        / "investigations"
        / "compiled-graph-capability-review-2026-07-20.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["schema_version"] == 1
    assert record["policy_version"] == compiled_graph.COMPILED_GRAPH_POLICY_VERSION == 2
    assert (
        record["capability_schema_version"]
        == compiled_graph.COMPILED_GRAPH_CAPABILITY_SCHEMA_VERSION
        == 2
    )
    assert record["decision"] == "no_promotion"
    assert record["verified_capability_rows"] == []
    assert verified_compiled_graph_capability_rows() == ()
    assert record["quarantined_evidence_ids"] == []

    run = record["workflow_run"]
    assert run["run_id"] == 29759326381
    assert run["run_number"] == 182
    assert run["run_attempt"] == 1
    assert run["head_sha"] == "d54aa5d5e2d57a382e387bd0276e2dc16b61bd42"
    assert run["synthetic_merge_sha"] == "776b2f69bfdc30763abfd466732fd58eae14e704"
    assert run["tested_tree_sha"] == "e0820c5c07765d918476402f55446e8130b7923a"
    assert run["repository_merge_sha"] == "90aba75696f39b4d77dd0ef39e604ad167b973c1"
    assert run["runner"] == {
        "requested_labels": ["ubuntu-latest"],
        "image": "ubuntu-24.04",
        "image_version": "20260714.240.1",
        "immutable": False,
    }

    artifacts = {artifact["ray_version"]: artifact for artifact in record["artifacts"]}
    expected = {
        "2.53.0": {
            "job_id": 88409701323,
            "artifact_id": 8468065623,
            "archive_size_bytes": 2545,
            "archive_digest": (
                "sha256:de17ac271b571a938400d81d42bc6ddbdcd27d6da928ceb7086e1c9488bb3c16"
            ),
            "files": {
                "environment.json": "2f6486231cf05bdbc0b5ab456f24833b7f25d0e5102992a14e3f1c5a8bf3b24b",
                "packages.txt": "9e77949e2eb08936433bb796a978f907d9cdf11bda60730c5721f8b88ae6631f",
                "probe.json": "0edabddf2f9e6df27186bfc8a2c8135a748c7b22fd0fa88f3b94252d456b40db",
            },
        },
        "2.56.0": {
            "job_id": 88409701353,
            "artifact_id": 8468066557,
            "archive_size_bytes": 2304,
            "archive_digest": (
                "sha256:4430fefd13d1203cb2c84c9765da5eaa363fbdc0e30fc1117d323526966ccda9"
            ),
            "files": {
                "environment.json": "9bf7206cc54ffdfbe7ecd9d2e34c6c063289e46520ffe5678d31cdd68a453722",
                "packages.txt": "1644605ea4dff9ffa88ce1fa7a31b60f0f382318f23fce816134e0832bf6fa07",
                "probe.json": "dafc55d392a4d1a823a256afe0f906e811421e10de434a99ec0f676c6f6e7c05",
            },
        },
        "2.56.1": {
            "job_id": 88409701415,
            "artifact_id": 8468065248,
            "archive_size_bytes": 2305,
            "archive_digest": (
                "sha256:e755fab0fe3b07ecf390fa193e2a409dc40232d3e83a8bc6db20e464a85d0bb5"
            ),
            "files": {
                "environment.json": "a9a1dcdf11c2e6aeeefe7107e0467f25f6e74f77f64a263cf86ca7164191f228",
                "packages.txt": "aa5255e36f5af57f33c05d1b8b43f01584bed9c296edd6b0d3c42857a2cdd6da",
                "probe.json": "1319909b6b2ecb01c025b12005f4c59829921f2276c74d39e83e149ee2621da8",
            },
        },
    }
    assert set(artifacts) == set(expected)
    for ray_version, expected_artifact in expected.items():
        artifact = artifacts[ray_version]
        assert artifact["job_id"] == expected_artifact["job_id"]
        assert artifact["artifact_id"] == expected_artifact["artifact_id"]
        assert artifact["archive_size_bytes"] == expected_artifact["archive_size_bytes"]
        assert artifact["archive_digest"] == expected_artifact["archive_digest"]
        assert artifact["artifact_url"].endswith(f"/artifacts/{artifact['artifact_id']}")
        assert artifact["expires_at"] == "2026-10-18T16:22:37Z"
        assert artifact["quarantined"] is False
        assert {item["path"]: item["sha256"] for item in artifact["files"]} == (
            expected_artifact["files"]
        )
        assert artifact["observation"]["native_probe_status"] == "success"
        assert artifact["observation"]["adapter_eligible"] is False
        assert artifact["observation"]["adapter_reason"] == "INCOMPLETE_CAPABILITY_CONTEXT"

    maintenance = record["maintenance_policy"]
    assert maintenance["no_promotion"]["artifact_expiry_invalidates_policy"] is False
    assert maintenance["verified_rows"] == {
        "must_match_runtime_policy_exactly": True,
        "evidence_ids_required": True,
        "reviewed_on_required": True,
        "revalidate_on_or_before_required": True,
        "unexpired_artifacts_required": True,
        "quarantined_rows_allowed": False,
    }
    assert record["follow_up"]["issue"].endswith("/issues/102")


def test_windows_reproducer_is_standalone_ray_only_python() -> None:
    path = PROJECT_ROOT / "docs" / "investigations" / "reproduce_ray_compiled_graph_windows.py"
    source = path.read_text(encoding="utf-8")

    ast.parse(source)
    assert "django_ray" not in source
    assert "experimental_compile()" in source
    assert "max_retries=0" in source


def test_upstream_draft_requires_fresh_redacted_bounded_evidence() -> None:
    path = PROJECT_ROOT / "docs" / "investigations" / "ray-compiled-graph-windows-report-draft.md"
    report = path.read_text(encoding="utf-8")

    assert "current summary is not promotion-grade evidence" in report
    assert "reviewed, redacted," in report
    assert "bounded stdout/stderr tails" in report
    assert "Never attach complete" in report
    assert "2026-07-27" in report
    assert "issue #100 owns" in report
    assert "non-blocking for issue #86 / PR #92" in report


def test_automatic_detection_path_uses_detected_identity(monkeypatch) -> None:
    identity = _runtime()
    monkeypatch.setattr(compiled_graph, "detect_compiled_graph_runtime", lambda: identity)

    decision = evaluate_compiled_graph_support(
        CompiledGraphTopology.DIRECT_DRIVER,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE,
    )

    assert decision.runtime is identity


def test_policy_module_does_not_import_ray_before_guard() -> None:
    source_path = PROJECT_ROOT / "src" / "django_ray" / "runtime" / "compiled_graph.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "ray" not in imported_roots


def test_candidate_versions_remain_policy_data_without_a_hosted_native_smoke() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    candidate_versions = {row["ray_version"] for row in candidate_compiled_graph_runtime_rows()}

    assert candidate_versions == {"2.53.0", "2.56.0", "2.56.1"}
    assert "compiled-graph-candidate-smoke" not in workflow
    assert "ray[cgraph]" not in workflow
    assert "--candidate-native" not in workflow


def test_gpu_dependencies_are_not_mandatory_application_dependencies() -> None:
    project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"ray[default]>=2.53.0"' in project
    assert "cupy" not in project.lower()
    assert '"ray[cgraph]' not in project


def test_hosted_native_canary_is_absent() -> None:
    assert not (PROJECT_ROOT / ".github" / "workflows" / "compiled-graph-canary.yml").exists()


def _capability_id(capability: compiled_graph._CapabilityIdentity) -> str:
    return compiled_graph._capability_set_identifier(capability)
