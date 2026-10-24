"""Read only owned log surfaces; retain fixed facts rather than private bytes."""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from qualification.application import protected_log_diagnostics as diagnostics
from qualification.application import worker_readiness_diagnostics as readiness
from qualification.application import worker_resource_diagnostics as resources
from qualification.application.protected_database import read_owned_rows
from qualification.application.protected_runtime import (
    assert_clear,
    validate,
)

SECONDS = 30
STATUS_CHILD_SECONDS = 4
# The host also covers exec transport, initial Python module loading and receipt
# return outside the child alarm. Spend two seconds of the existing observer
# slack here; the seven command caps total 29 seconds and do not reset SECONDS.
STATUS_HOST_SECONDS = 7
LOG_BYTES = 1024 * 1024
PACKET_BYTES = 3 * 1024 * 1024
FACTS = (
    "source_pods_bound",
    "observation_interval_complete",
    "web_logs_clear",
    "manager_logs_clear",
    "refusal_api_clear",
)


def _alarm(_signal, _frame):
    raise TimeoutError("Protected log observation deadline expired")


@diagnostics.tracked("child")
def inspect_stdin(*, status_only: bool = False) -> None:
    """Runs only in the owned web Pod; stdin never becomes a command argument."""
    diagnostics.phase("input")
    if (
        sys.platform != "linux"
        or os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
    ):
        raise ValueError("Protected log observation requires its owned application")
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(STATUS_CHILD_SECONDS if status_only else 5)
    raw = sys.stdin.buffer.read(PACKET_BYTES + 1)
    if len(raw) > PACKET_BYTES:
        raise ValueError("Protected log input exceeds its bound")
    packet = json.loads(raw)
    expected = {"protected_runtime"} if status_only else {"protected_runtime", "logs"}
    if not isinstance(packet, dict) or set(packet) != expected:
        raise ValueError("Protected log input is invalid")
    validate(packet["protected_runtime"])
    logs = [] if status_only else packet["logs"]
    if not isinstance(logs, list) or len(logs) != (0 if status_only else 2):
        raise ValueError("Protected log input requires both owned surfaces")
    surfaces = []
    for item in logs:
        if not isinstance(item, str):
            raise ValueError("Protected log input has an invalid surface")
        surface = base64.b64decode(item, validate=True)
        if not surface or len(surface) > LOG_BYTES:
            raise ValueError("Protected log surface is empty or oversized")
        surfaces.append(surface)
    diagnostics.phase("read_only_db")
    values, tasks = read_owned_rows(packet["protected_runtime"]["row_ids"], status_only=status_only)
    if status_only:
        diagnostics.phase("status_api")
        from qualification.application.run_api import ApplicationHttp

        verify_refusal_statuses(
            ApplicationHttp("http://127.0.0.1:8000", request_timeout=1),
            token=os.environ["DJANGO_API_TOKEN"],
            tasks=tasks,
            values=values,
        )
        print('{"refusal_api_clear":true}', flush=True)
    else:
        diagnostics.phase("private_compare")
        assert_clear(surfaces, values)
        print('{"web_logs_clear":true,"manager_logs_clear":true}', flush=True)


def verify_refusal_statuses(request, *, token: str, tasks: list[str], values: list[str]) -> None:
    from qualification.application.api import validate_task_status_payload

    if len(tasks) != 2 or len(set(tasks)) != 2:
        raise ValueError("Protected status observation requires both refused tasks")
    for task_id in tasks:
        status, body = request(
            f"/api/tasks/{task_id}",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
            response_limit=64 * 1024,
            required_response_headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
        if status != 200 or not isinstance(body, bytes) or len(body) > 64 * 1024:
            raise ValueError("Protected status response is absent or oversized")
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or validate_task_status_payload(payload, task_id=task_id) != "FAILED"
            or payload.get("attempt_number") != 1
            or payload.get("execution_generation") != 1
        ):
            raise ValueError("Protected status response has the wrong owned execution")
        assert_clear([body], values)


def validate_receipt(value: object, generation: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "layer",
            "status",
            "complete_application_gate",
            "generation",
            "assertions",
        }
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["layer"] != "application_protected_logs"
        or value["status"] != "passed"
        or value["complete_application_gate"] is not False
        or value["generation"] != generation
        or not isinstance(value["assertions"], dict)
        or set(value["assertions"]) != set(FACTS)
        or any(value["assertions"][name] is not True for name in FACTS)
    ):
        raise ValueError("Protected log receipt is incomplete")


@diagnostics.tracked("host")
def observe(
    *,
    context: str,
    kubeconfig_paths: str,
    namespace: str,
    image: str,
    generation: str,
    run=subprocess.run,
) -> dict:
    deadline = time.monotonic() + SECONDS
    # Chainsaw replaces KUBECONFIG for command operations with a one-context file.
    # Restore the runner's original paths so its explicit context remains authoritative.
    environment = {**os.environ, "KUBECONFIG": kubeconfig_paths}

    def command(
        arguments, *, data=None, maximum=LOG_BYTES, limit=5, private=False, log_surface=False
    ):
        if log_surface:
            diagnostics.log("command")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Protected log observation deadline expired")
        result = run(
            ["kubectl", "--context", context, "--request-timeout=5s", "-n", namespace, *arguments],
            input=data,
            capture_output=True,
            timeout=min(limit, remaining),
            check=False,
            env=environment,
        )
        if result.returncode and private:
            failure = diagnostics.parse_child(result.stdout)
            if failure is not None:
                raise failure
        for reason, invalid in (
            ("command_exit", lambda: result.returncode),
            ("output_bound", lambda: len(result.stdout) > maximum),
            ("command_stderr", lambda: result.stderr),
        ):
            if log_surface:
                diagnostics.log(reason)
            if invalid():
                raise ValueError("Protected log command did not return complete bounded evidence")
        return result.stdout

    def inventory():
        diagnostics.inventory(None, "inventory_read")
        items = json.loads(command(["get", "pods", "-o", "json"], maximum=128 * 1024, limit=2))[
            "items"
        ]
        diagnostics.inventory(None, "inventory_bound")
        if not isinstance(items, list) or len(items) > 16:
            raise ValueError("Protected log Pod inventory exceeds its bound")
        found = {}
        for app, container in (
            ("django-web", "web"),
            ("django-manager", "manager"),
            (f"assert-{generation}", "assertions"),
        ):
            diagnostics.inventory(container, "pod_count")
            (pod,) = [
                item for item in items if item["metadata"].get("labels", {}).get("app") == app
            ]
            diagnostics.inventory(container, "container_spec_count")
            (spec,) = [item for item in pod["spec"]["containers"] if item["name"] == container]
            diagnostics.inventory(container, "container_status_count")
            (state,) = [
                item for item in pod["status"]["containerStatuses"] if item["name"] == container
            ]
            diagnostics.inventory(container, "pod_deleting")
            if pod["metadata"].get("deletionTimestamp"):
                raise ValueError("Protected log source identity is not the current candidate")
            diagnostics.inventory(container, "declared_image")
            if spec["image"] != image:
                raise ValueError("Protected log source identity is not the current candidate")
            diagnostics.inventory(container, "observed_image")
            if state.get("imageID", "").rsplit("@", 1)[-1] != image.rsplit("@", 1)[-1]:
                raise ValueError("Protected log source identity is not the current candidate")
            diagnostics.inventory(container, "container_restarted")
            if type(state["restartCount"]) is not int or state["restartCount"] != 0:
                raise ValueError("Protected log source identity is not the current candidate")
            diagnostics.inventory(
                container,
                "generation_exit" if app.startswith("assert-") else "container_not_running",
            )
            live = state["state"].get("running")
            done = state["state"].get("terminated")
            if app.startswith("assert-"):
                if not done or done["exitCode"] != 0:
                    raise ValueError("Protected log generation did not finish successfully")
            else:
                if not live:
                    raise ValueError("Protected log application source is not live and ready")
                diagnostics.inventory(container, "container_not_ready")
                if state.get("ready") is not True:
                    if container == "manager":
                        # The failed inventory stays authoritative even if a
                        # later command observes a ready lease. Never read logs
                        # or extend this observer's deadline to obtain a report.
                        report = readiness.empty()
                        try:
                            report = readiness.observe(
                                prefix=[
                                    "kubectl",
                                    "--context",
                                    context,
                                    "--request-timeout=5s",
                                    "-n",
                                    namespace,
                                ],
                                pod=pod["metadata"]["name"],
                                environment=environment,
                                deadline=deadline,
                                on_resources=diagnostics.worker_resources,
                            )
                        except Exception:
                            pass  # Optional diagnostics cannot replace this failure.
                        if readiness.valid(report):
                            diagnostics.worker_readiness(report)
                    raise ValueError("Protected log application source is not live and ready")
            diagnostics.inventory(container, "source_coordinates")
            found[app] = (
                pod["metadata"]["name"],
                pod["metadata"]["uid"],
                container,
                live["startedAt"] if live else done["startedAt"],
            )
        diagnostics.inventory(None, None)
        return found

    diagnostics.phase("inventory")
    before = inventory()
    diagnostics.phase("core_receipt")
    producer = before[f"assert-{generation}"]
    raw = command(
        ["logs", producer[0], "-c", producer[2], "--limit-bytes=65537"], maximum=65536, limit=2
    )
    cores = [json.loads(line) for line in raw.splitlines() if line.startswith(b"{")]
    (core,) = [item for item in cores if item.get("layer") == "application_core"]
    if core.get("status") != "passed":
        raise ValueError("Protected log observation has no successful core receipt")
    evidence = core.get("protected_runtime")
    validate(evidence)
    start = datetime.fromisoformat(evidence["started_at"])
    now = datetime.now(UTC)
    if not 0 <= (now - start).total_seconds() <= 1800:
        raise ValueError("Protected log observation interval is invalid")
    web = before["django-web"]

    def inspect(packet, *, status_only):
        payload = json.dumps(packet, separators=(",", ":")).encode()
        if len(payload) > PACKET_BYTES:
            raise ValueError("Protected log packet exceeds its bound")
        raw = command(
            [
                "exec",
                "-i",
                web[0],
                "-c",
                web[2],
                "--",
                "python",
                "-m",
                "qualification.application.protected_logs",
                "--inspect-status" if status_only else "--inspect-stdin",
            ],
            data=payload,
            maximum=512,
            limit=STATUS_HOST_SECONDS if status_only else 6,
            private=True,
        )
        observed = json.loads(raw)
        expected = (
            {"refusal_api_clear"} if status_only else {"web_logs_clear", "manager_logs_clear"}
        )
        if (
            not isinstance(observed, dict)
            or set(observed) != expected
            or any(value is not True for value in observed.values())
        ):
            raise ValueError("Protected log private comparison did not pass")

    # These two requests must precede both snapshots; the final checker is log-only.
    diagnostics.phase("status_check")
    inspect({"protected_runtime": evidence}, status_only=True)
    logs = []
    for app in ("django-web", "django-manager"):
        diagnostics.phase("web_log" if app == "django-web" else "manager_log")
        name, _, container, started = before[app]
        diagnostics.log("source_timestamp")
        source_started = datetime.fromisoformat(started)
        diagnostics.log("source_interval")
        if source_started > start:
            raise ValueError("Protected log source was replaced during the observation interval")
        raw = command(
            [
                "logs",
                name,
                "-c",
                container,
                "--timestamps=true",
                "--tail=-1",
                f"--limit-bytes={LOG_BYTES + 1}",
            ],
            log_surface=True,
        )
        diagnostics.log("empty_log")
        if not raw:
            raise ValueError("Protected log source has no interval evidence")
        diagnostics.log("first_timestamp")
        first = raw.split(b" ", 1)[0].decode()
        if len(first) > 40:
            raise ValueError("Protected log source does not cover the observation start")
        first_observed = datetime.fromisoformat(first)
        diagnostics.log("start_coverage")
        if first_observed > start:
            raise ValueError("Protected log source does not cover the observation start")
        logs.append(base64.b64encode(raw).decode())
    diagnostics.phase("private_compare")
    inspect({"protected_runtime": evidence, "logs": logs}, status_only=False)
    diagnostics.phase("final_inventory")
    after = inventory()
    for app, original in before.items():
        diagnostics.inventory(original[2], "source_changed")
        if after[app] != original:
            raise ValueError("Protected log source changed or exceeded its deadline")
    diagnostics.inventory(None, "deadline")
    if time.monotonic() > deadline:
        raise ValueError("Protected log source changed or exceeded its deadline")
    return {
        "schema_version": 1,
        "layer": "application_protected_logs",
        "status": "passed",
        "complete_application_gate": False,
        "generation": generation,
        "assertions": dict.fromkeys(FACTS, True),
    }


def _retain_host_failure(output: Path, diagnostic: str) -> bool:
    """Best-effort retention cannot replace the original observation failure."""
    try:
        raw = (diagnostic + "\n").encode("utf-8")
        if len(raw) > diagnostics.MAX_BYTES:
            return False
        value = json.loads(diagnostic)
        if not diagnostics._valid(value, "host") or diagnostic != json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ):
            return False
        try:
            output.with_name(output.name + ".failure-resources.json").lstat()
        except FileNotFoundError:
            pass
        else:
            # Do not pair this failure with orphaned earlier resource evidence.
            return False
        sidecar = output.with_name(output.name + ".failure.json")
        with sidecar.open("xb") as stream:
            if stream.write(raw) != len(raw):
                return False
        return True
    except Exception:
        # Never render storage errors or overwrite earlier evidence, including
        # an existing symlink. The original stderr diagnostic and exit survive.
        return False


def _retain_resources(output: Path, value: dict | None) -> None:
    """Optional closed prefix facts cannot change the failed host observation."""
    try:
        if not resources.valid_record(value):
            return
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > resources.MAX_BYTES:
            return
        sidecar = output.with_name(output.name + ".failure-resources.json")
        with sidecar.open("xb") as stream:
            stream.write(raw)
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    private = parser.add_mutually_exclusive_group()
    private.add_argument("--inspect-stdin", action="store_true")
    private.add_argument("--inspect-status", action="store_true")
    parser.add_argument("--namespace")
    parser.add_argument("--context")
    parser.add_argument("--kubeconfig-paths")
    parser.add_argument("--image")
    parser.add_argument("--generation", choices=("before", "after"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    failure_output = None
    try:
        if args.inspect_stdin or args.inspect_status:
            inspect_stdin(status_only=args.inspect_status)
        else:
            if (
                not args.namespace
                or not args.context
                or not args.kubeconfig_paths
                or not args.namespace.startswith("django-ray-core-")
                or not args.image
                or "@sha256:" not in args.image
                or not args.generation
                or args.output is None
            ):
                raise ValueError("Protected log observer requires its owned namespace and image")
            failure_output = args.output
            receipt = observe(
                context=args.context,
                kubeconfig_paths=args.kubeconfig_paths,
                namespace=args.namespace,
                image=args.image,
                generation=args.generation,
            )
            validate_receipt(receipt, args.generation)
            with args.output.open("x") as stream:
                stream.write(json.dumps(receipt, sort_keys=True))
    except Exception as error:
        private = args.inspect_stdin or args.inspect_status
        diagnostic = diagnostics.diagnostic(error, "child" if private else "host")
        if failure_output is not None and _retain_host_failure(failure_output, diagnostic):
            _retain_resources(failure_output, diagnostics.resource_diagnostic(error))
        print(
            diagnostic,
            file=sys.stdout if private else sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
