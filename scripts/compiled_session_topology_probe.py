"""Opt-in raw-Ray probe for one per-run Compiled Graph owner process.

This artifact proves only the direct-driver nested Ray Core process topology chosen as
ADR-0002's initial evidence boundary. It is not a django-ray execution strategy, a
compatibility decision, or a Ray Client-submitted live-cluster probe.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

OPT_IN_ENV = "DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE"
RESULT_SCHEMA_VERSION = 1
TOPOLOGY = "ray_core_outer_task_owner"


@dataclass(frozen=True)
class ProbeConfig:
    """Bounded settings for one native topology probe."""

    address: str = "auto"
    invocations: int = 3
    timeout_seconds: float = 60.0
    namespace: str = "django-ray-compiled-session-probe"

    def validate(self) -> None:
        """Reject unsupported or unbounded probe settings before importing Ray."""
        if self.address.startswith("ray://"):
            raise ValueError(
                "Ray Client submission requires the dedicated live-cluster topology probe"
            )
        if not 2 <= self.invocations <= 128:
            raise ValueError("invocations must be between 2 and 128")
        if not 1 <= self.timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be between 1 and 600")
        if not self.namespace.strip():
            raise ValueError("namespace must not be empty")


def build_run_identity(run_id: str) -> dict[str, Any]:
    """Return the versioned shape fenced by django-ray issue #81."""
    return {
        "schema_version": 1,
        "task_execution_pk": 0,
        "attempt_number": 1,
        "execution_generation": 1,
        "run_id": run_id,
    }


def _run_in_owner_process(
    run_identity: dict[str, Any],
    invocations: int,
) -> dict[str, Any]:  # pragma: no cover - exercised only by the opt-in native probe
    """Compile, invoke, consume, and teardown inside one outer Ray task process."""
    import ray
    from ray.dag import InputNode

    class ProbeStage:
        def __init__(self, name: str) -> None:
            self.name = name
            self._run_identity: dict[str, Any] | None = None
            self._invocation_count = 0

        def apply(self, envelope: dict[str, Any]) -> dict[str, Any]:
            received_identity = envelope["run_identity"]
            if self._run_identity is None:
                self._run_identity = received_identity
            elif received_identity != self._run_identity:
                raise RuntimeError("probe stage rejected a stale workflow run identity")

            self._invocation_count += 1
            result = dict(envelope)
            trace = list(result.get("stage_trace", []))
            trace.append(
                {
                    "name": self.name,
                    "pid": os.getpid(),
                    "invocation_count": self._invocation_count,
                }
            )
            result["stage_trace"] = trace
            result["value"] = int(result["value"]) + 1
            return result

    owner_pid = os.getpid()
    stage_class = ray.remote(num_cpus=1)(ProbeStage)
    left = stage_class.remote("left")
    right = stage_class.remote("right")
    compiled = None
    report: dict[str, Any] | None = None
    try:
        with InputNode() as graph_input:
            graph = left.apply.bind(graph_input)
            graph = right.apply.bind(graph)
        compiled = graph.experimental_compile(
            _max_inflight_executions=1,
            _max_buffered_results=1,
        )

        results: list[dict[str, Any]] = []
        for index in range(invocations):
            envelope = {
                "run_identity": run_identity,
                "invocation_id": str(uuid4()),
                "invoked_by_pid": owner_pid,
                "value": index,
            }
            result = ray.get(compiled.execute(envelope), timeout=20)
            results.append(result)

        report = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "topology": TOPOLOGY,
            "reuse_boundary": "within_run",
            "ray_version": ray.__version__,
            "python_version": platform.python_version(),
            "platform": platform.system().lower(),
            "run_identity": run_identity,
            "compiled_by_pid": owner_pid,
            "owner_worker_id": str(ray.get_runtime_context().get_worker_id()),
            "compile_count": 1,
            "invocation_count": invocations,
            "max_in_flight": 1,
            "max_buffered_results": 1,
            "results": results,
        }
    finally:
        if compiled is not None:
            compiled.teardown(kill_actors=True)
        else:
            ray.kill(left, no_restart=True)
            ray.kill(right, no_restart=True)

    if report is None:  # pragma: no cover - protects future refactors
        raise AssertionError("probe did not produce a report")
    report["teardown_completed"] = True
    return report


def validate_report(
    report: dict[str, Any],
    *,
    expected_run_identity: dict[str, Any],
    expected_invocations: int,
) -> None:
    """Validate the topology evidence returned by the outer Ray task."""
    if report.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise AssertionError("unexpected probe result schema")
    if report.get("topology") != TOPOLOGY:
        raise AssertionError("unexpected probe topology")
    if report.get("reuse_boundary") != "within_run":
        raise AssertionError("probe claimed an unsupported reuse boundary")
    if report.get("run_identity") != expected_run_identity:
        raise AssertionError("owner returned a different durable run identity")
    if report.get("compile_count") != 1:
        raise AssertionError("probe must compile exactly once")
    if report.get("invocation_count") != expected_invocations:
        raise AssertionError("probe returned the wrong invocation count")
    if report.get("max_in_flight") != 1 or report.get("max_buffered_results") != 1:
        raise AssertionError("probe admission limits changed")
    if report.get("teardown_completed") is not True:
        raise AssertionError("probe did not confirm explicit teardown")

    owner_pid = report.get("compiled_by_pid")
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        raise AssertionError("probe owner PID is invalid")

    results = report.get("results")
    if not isinstance(results, list) or len(results) != expected_invocations:
        raise AssertionError("probe results are incomplete")

    invocation_ids: set[str] = set()
    stage_pids: set[int] = set()
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise AssertionError("probe returned a non-object invocation result")
        if result.get("run_identity") != expected_run_identity:
            raise AssertionError("a graph stage returned a stale run identity")
        if result.get("invoked_by_pid") != owner_pid:
            raise AssertionError("graph invocation did not originate in the compiler process")
        if result.get("value") != index + 2:
            raise AssertionError("graph result parity check failed")

        invocation_id = result.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise AssertionError("invocation ID is missing")
        if invocation_id in invocation_ids:
            raise AssertionError("invocation IDs are not unique")
        invocation_ids.add(invocation_id)

        trace = result.get("stage_trace")
        if not isinstance(trace, list) or len(trace) != 2:
            raise AssertionError("graph did not execute both actor stages")
        if [entry.get("name") for entry in trace] != ["left", "right"]:
            raise AssertionError("graph stage order changed")
        if [entry.get("invocation_count") for entry in trace] != [index + 1, index + 1]:
            raise AssertionError("graph actors were not reused across invocations")
        for entry in trace:
            stage_pid = entry.get("pid")
            if not isinstance(stage_pid, int) or stage_pid <= 0:
                raise AssertionError("graph stage PID is invalid")
            stage_pids.add(stage_pid)

    if owner_pid in stage_pids:
        raise AssertionError("compiler owner and graph actors must be separate processes")
    if len(stage_pids) != 2:
        raise AssertionError("probe expected two dedicated actor processes")


def run_probe(config: ProbeConfig) -> dict[str, Any]:
    """Run the bounded native probe and return verified JSON-safe evidence."""
    config.validate()
    import ray

    try:
        from ray.util.client import ray as ray_client
    except ImportError:
        ray_client_connected = False
    else:
        ray_client_connected = bool(ray_client.is_connected())
    if ray_client_connected:
        raise ValueError(
            "an active Ray Client session requires the dedicated live-cluster topology probe"
        )

    started_ray = not ray.is_initialized()
    if started_ray:
        init_options: dict[str, Any] = {
            "include_dashboard": False,
            "logging_level": "ERROR",
            "namespace": config.namespace,
        }
        if config.address == "local":
            init_options["num_cpus"] = 2
        else:
            init_options["address"] = config.address
        ray.init(**init_options)

    owner_result = None
    try:
        run_identity = build_run_identity(str(uuid4()))
        owner = ray.remote(num_cpus=0, max_retries=0)(_run_in_owner_process)
        owner_result = owner.remote(run_identity, config.invocations)
        report = ray.get(owner_result, timeout=config.timeout_seconds)
        validate_report(
            report,
            expected_run_identity=run_identity,
            expected_invocations=config.invocations,
        )
        return report
    except Exception:
        if owner_result is not None:
            try:
                ray.cancel(owner_result, force=True, recursive=True)
            except Exception:
                pass
        raise
    finally:
        if started_ray:
            ray.shutdown()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address",
        default=os.environ.get("RAY_ADDRESS", "auto"),
        help=(
            "Direct Ray address, 'auto', or 'local'; ray:// is reserved for a separate "
            "live-cluster topology probe."
        ),
    )
    parser.add_argument("--invocations", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--namespace", default="django-ray-compiled-session-probe")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run only after an explicit native-beta opt-in."""
    if os.environ.get(OPT_IN_ENV) != "1":
        print(
            f"Refusing native probe: public CI must not set {OPT_IN_ENV}; "
            "promotion evidence must use the guarded local KubeRay pilot.",
            file=sys.stderr,
        )
        return 2

    arguments = _parser().parse_args(argv)
    config = ProbeConfig(
        address=arguments.address,
        invocations=arguments.invocations,
        timeout_seconds=arguments.timeout_seconds,
        namespace=arguments.namespace,
    )
    try:
        report = run_probe(config)
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": RESULT_SCHEMA_VERSION,
                    "topology": TOPOLOGY,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                sort_keys=True,
            )
        )
        return 1

    output = dict(report)
    output["status"] = "passed"
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
