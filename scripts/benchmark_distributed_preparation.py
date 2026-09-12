"""Compare exact-source fanout preparation with a nonexecuting Ray sink on Linux.

This measures sender CPU and traced Python allocations, not Ray execution or
throughput. Each case gets a fresh, time-bounded Python process. No database or
Ray service is started, and no application callable is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def _square(value: int) -> int:
    raise AssertionError("benchmark must not execute application code")


def _add(left: int, right: int) -> int:
    raise AssertionError("benchmark must not execute application code")


def _sender_context(identity):
    """Create canonical synthetic sender controls; never qualify a Ray target."""
    from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
    from django_ray.runtime.context import durable_task_execution

    controls = {}
    if EXECUTION_PROTOCOL_VERSION == 3:
        from datetime import UTC, datetime, timedelta

        from django_ray import __version__
        from django_ray.execution_codec import ExecutionIdentity
        from django_ray.target.attestation import (
            RayNodeStateVersion,
            RayRunnerFamily,
            RayRuntimeVersion,
            RayTargetExpectation,
            build_ray_cluster_attestation,
            build_ray_node_observation,
            build_ray_observation_boundary,
            ray_target_expectation_digest,
        )
        from django_ray.target.cohort_contract import (
            CohortExecutionContract,
            cohort_execution_contract_digest,
            encode_cohort_execution_contract,
        )

        # Fixed times and identities make request-byte comparisons reproducible.
        # No collector or point guard runs, and the sink never executes a leaf.
        now = datetime(2020, 1, 1, tzinfo=UTC)
        runtime = RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 0)
        expectation = RayTargetExpectation(
            "benchmark-only", RayRunnerFamily.RAY_CORE, "session_benchmark", 1, runtime
        )
        node_id = "a" * 56
        nodes = (RayNodeStateVersion(node_id, 1),)
        observation = build_ray_cluster_attestation(
            expectation=expectation,
            boundary=build_ray_observation_boundary(
                resource_state_version_before=1,
                resource_state_version_after=1,
                node_state_versions_before=nodes,
                node_state_versions_after=nodes,
            ),
            nodes=(
                build_ray_node_observation(
                    node_id=node_id, cluster_session=expectation.cluster_session, runtime=runtime
                ),
            ),
            observed_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        contract = CohortExecutionContract(
            identity=ExecutionIdentity(41, "benchmark-41", 1, 1),
            expected_django_ray_version=__version__,
            target_binding_id=1,
            cohort_evidence_id=1,
            cohort_evidence_digest="sha256:" + "a" * 64,
            claimed_at=now,
            target_expectation=expectation,
            target_expectation_digest=ray_target_expectation_digest(expectation),
            claim_attestation=observation,
            claim_attestation_digest=observation.attestation_digest,
        )
        controls = {
            "cohort_contract_json": encode_cohort_execution_contract(contract),
            "cohort_contract_digest": cohort_execution_contract_digest(contract),
        }
    elif EXECUTION_PROTOCOL_VERSION != 1:
        raise ValueError("benchmark requires a supported sender protocol")
    return durable_task_execution(
        41,
        task_id="benchmark-41",
        execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
        attempt_number=1,
        execution_generation=1 if EXECUTION_PROTOCOL_VERSION == 3 else 0,
        runtime_env_plan_identity=identity,
        strict_execution_request=True,
        **controls,
    )


def _compare_request_bytes(baseline, candidate):
    if baseline["execution_protocol_version"] != candidate["execution_protocol_version"]:
        raise ValueError("benchmark cannot compare different active execution protocols")
    if baseline["wire_sha256"] != candidate["wire_sha256"]:
        raise AssertionError("baseline and candidate request bytes differ")


def _measure(helper: str, count: int, window: int) -> dict:
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DJANGO_RAY={"RAY_ADDRESS": "local"}, INSTALLED_APPS=[], SECRET_KEY="benchmark"
        )

    from django_ray import execution_codec as codec
    from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
    from django_ray.runtime import distributed
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.workflow.plans import runtime_env_plan_identity

    counters = {"requests": 0, "full_encodings": 0, "callable_hashes": 0, "max_prepared_ahead": 0}
    submitted = 0
    consumed = 0
    first_submit_seconds: float | None = None
    wire_digest = hashlib.sha256()
    original_request = distributed._nested_distributed_request
    # Current preparation shares its full encoder with the explicit cohort
    # codec. Older benchmark baselines called the public encoder directly.
    encoder_name = (
        "_encode_nested_request_for_protocols"
        if hasattr(codec, "_encode_nested_request_for_protocols")
        else "encode_nested_execution_request"
    )
    original_encode = getattr(codec, encoder_name)
    original_hash = codec.nested_callable_digest

    def request(*args):
        counters["requests"] += 1
        counters["max_prepared_ahead"] = max(
            counters["max_prepared_ahead"], counters["requests"] - consumed
        )
        return original_request(*args)

    def encode(value, *args, **kwargs):
        counters["full_encodings"] += 1
        return original_encode(value, *args, **kwargs)

    def callable_hash(value):
        counters["callable_hashes"] += 1
        return original_hash(value)

    def submit(*args):
        nonlocal submitted, first_submit_seconds
        if first_submit_seconds is None:
            first_submit_seconds = time.perf_counter() - started
        request_index = -12 if EXECUTION_PROTOCOL_VERSION == 3 else -10
        wire_digest.update(args[request_index].encode("utf-8"))
        result = submitted
        submitted += 1
        return result

    def get(refs):
        nonlocal consumed
        consumed += len(refs) if isinstance(refs, list) else 1
        return refs

    remote = SimpleNamespace(remote=submit)
    remote.options = lambda **kwargs: remote
    ray = SimpleNamespace(get=get, wait=lambda refs, **kwargs: (refs[:1], refs[1:]))
    identity = runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()
    items = list(range(count))
    star_items = [(i, 1) for i in items]
    scattered = [(_square, (i,), {}) if i % 2 else (_add, (i, 1), {}) for i in items]
    scatter_bounded = "max_concurrency" in inspect.signature(distributed.scatter_gather).parameters
    effective_window = window if helper != "scatter" or scatter_bounded else None
    with (
        patch.dict(sys.modules, {"ray": ray}),
        patch.object(distributed, "is_ray_available", lambda: True),
        patch.object(distributed, "_get_cached_remote", lambda _: remote),
        patch.object(distributed, "uuid4", lambda: UUID(int=1)),
        patch.object(distributed, "_nested_distributed_request", request),
        patch.object(codec, encoder_name, encode),
        patch.object(codec, "nested_callable_digest", callable_hash),
        _sender_context(identity),
    ):
        tracemalloc.start()
        started = time.perf_counter()
        cpu_started = time.process_time()
        try:
            if helper == "map":
                result = distributed.parallel_map(_square, items, max_concurrency=window)
            elif helper == "starmap":
                result = distributed.parallel_starmap(_add, star_items, max_concurrency=window)
            else:
                kwargs = {"max_concurrency": window} if scatter_bounded else {}
                result = distributed.scatter_gather(scattered, **kwargs)
            cpu_seconds = time.process_time() - cpu_started
            wall_seconds = time.perf_counter() - started
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    if result != items or submitted != count or consumed != count:
        raise AssertionError("collector did not preserve all ordered results")
    return {
        "execution_protocol_version": EXECUTION_PROTOCOL_VERSION,
        "context_kind": "synthetic-sender-only",
        "source_paths": {"distributed": distributed.__file__, "codec": codec.__file__},
        "helper": helper,
        "items": count,
        "effective_window": effective_window,
        "cpu_seconds": cpu_seconds,
        "wall_seconds": wall_seconds,
        "first_submit_seconds": first_submit_seconds,
        "python_peak_bytes": peak_bytes,
        "wire_sha256": wire_digest.hexdigest(),
        **counters,
    }


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--items", nargs="+", type=int, default=[100, 1000])
    parser.add_argument("--window", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--case-timeout", type=int, default=120)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=["map", "starmap", "scatter"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (
        not args.items
        or len(args.items) > 3
        or len(set(args.items)) != len(args.items)
        or any(not 1 <= size <= 5000 for size in args.items)
        or not 1 <= args.window <= 256
        or not 1 <= args.repetitions <= 5
        or not 1 <= args.case_timeout <= 180
    ):
        parser.error("invalid bounded benchmark profile")
    if sys.platform != "linux":
        parser.error(
            "benchmark execution requires Linux; focused resource-free unit checks remain available"
        )
    if args.worker:
        import resource

        # A worker has no child processes. The parent owns its wall-clock timeout.
        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (args.case_timeout, args.case_timeout))
        print(json.dumps(_measure(args.worker, args.items[0], args.window), sort_keys=True))
        return 0
    if not args.baseline_ref or args.output is None:
        parser.error("--baseline-ref and --output are required")
    if _git("status", "--porcelain", "--untracked-files=no"):
        parser.error("benchmark requires a clean committed source tree")
    baseline = _git("rev-parse", "--verify", "--end-of-options", f"{args.baseline_ref}^{{commit}}")
    candidate = _git("rev-parse", "HEAD")
    report = {
        "schema_version": 1,
        "baseline_revision": baseline,
        "candidate_revision": candidate,
        "candidate_tree": _git("rev-parse", "HEAD^{tree}"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "dependencies": {"django": version("Django"), "ray": version("ray")},
        "available_cpus": len(os.sched_getaffinity(0)),
        "command": sys.argv,
        "cases": [],
        "scope": "sender-only with a nonexecuting Ray sink; timings include tracemalloc overhead",
        "memory_scope": "helper allocations including copied input references and results; excludes caller input storage and Ray",
        "bounds": {"address_space_bytes": 3 * 1024**3, "case_timeout_seconds": args.case_timeout},
    }
    with tempfile.TemporaryDirectory(prefix="django-ray-fanout-benchmark-") as directory:
        source_roots = {}
        for variant, revision in (("baseline", baseline), ("candidate", candidate)):
            source = Path(directory) / variant
            source.mkdir()
            archive = subprocess.check_output(["git", "archive", revision, "src"], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source_archive:
                source_archive.extractall(source, filter="data")
            source_roots[variant] = source
        for size in args.items:
            for helper in ("map", "starmap", "scatter"):
                for repetition in range(args.repetitions):
                    # Alternate case order; every measurement has a fresh process.
                    variants = (
                        ("baseline", "candidate")
                        if repetition % 2 == 0
                        else ("candidate", "baseline")
                    )
                    comparison = {}
                    for variant in variants:
                        source = source_roots[variant]
                        env = dict(
                            os.environ,
                            PYTHONPATH=str(source / "src"),
                            OPENBLAS_NUM_THREADS="1",
                            OMP_NUM_THREADS="1",
                            MKL_NUM_THREADS="1",
                            NUMEXPR_NUM_THREADS="1",
                        )
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--worker",
                                helper,
                                "--items",
                                str(size),
                                "--window",
                                str(args.window),
                                "--case-timeout",
                                str(args.case_timeout),
                            ],
                            cwd=ROOT,
                            env=env,
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=args.case_timeout,
                        )
                        measurement = json.loads(result.stdout)
                        if any(
                            not Path(path).resolve().is_relative_to((source / "src").resolve())
                            for path in measurement["source_paths"].values()
                        ):
                            raise AssertionError("benchmark imported a different source tree")
                        comparison[variant] = measurement
                        report["cases"].append(
                            {"variant": variant, "repetition": repetition, **measurement}
                        )
                    _compare_request_bytes(comparison["baseline"], comparison["candidate"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
