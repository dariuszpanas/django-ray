"""Run the public Chainsaw core test on explicitly admitted Kubernetes capacity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from qualification.application.budgets import APPLICATION_SECONDS
from qualification.application.receipt_limits import WORKFLOW_RECEIPT_MAX_BYTES
from qualification.application.resource_profiles import (
    RESOURCE_PROFILES,
    chainsaw_values,
    select_profile,
)
from qualification.application.result_presentation import (
    validate_evidence as validate_result_evidence,
)
from qualification.application.run_first_workflows import (
    LAYER as FIRST_WORKFLOW_LAYER,
)
from qualification.application.run_first_workflows import (
    validate_receipt as validate_first_workflow_receipt,
)
from qualification.application.run_reporting_benchmark import (
    LAYER as REPORTING_LAYER,
)
from qualification.application.run_reporting_benchmark import (
    MAX_RECEIPT_BYTES,
)
from qualification.application.run_reporting_benchmark import (
    validate_receipt as validate_reporting_receipt,
)
from qualification.application.run_showcase_failure import (
    LAYER as SHOWCASE_LAYER,
)
from qualification.application.run_showcase_failure import (
    RECEIPT_MAX_BYTES as SHOWCASE_RECEIPT_MAX_BYTES,
)
from qualification.application.run_showcase_failure import (
    validate_receipt as validate_showcase_receipt,
)
from qualification.application.worker_images import WorkerImageObservationError
from qualification.application.worker_images import run as observe_worker_images
from qualification.application.worker_images import (
    validate_failure_receipt as validate_worker_image_failure,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "qualification/application"
CREDENTIAL_KEYS = (
    "DJANGO_API_TOKEN",
    "DJANGO_SECRET_KEY_PART_ONE",
    "DJANGO_SECRET_KEY_PART_TWO",
    "DATABASE_PASSWORD",
    "DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY",
    "DJANGO_SUPERUSER_PASSWORD",
)
RECEIPTS = {
    "django-web": ("setup", ("setup",)),
    "assert-before": (
        "assertions",
        (
            "before-first",
            "before-nodes",
            "before-core",
            "before-workflows",
            "before-rq2",
            "before-showcase",
            "before-protocol",
        ),
    ),
    "assert-after": (
        "assertions",
        (
            "after-first",
            "after-nodes",
            "after-core",
            "after-workflows",
            "after-rq2",
            "after-showcase",
            "after-protocol",
        ),
    ),
    "reporting-benchmark": ("assertions", ("reporting",)),
}
LAYERS = {
    "reporting": REPORTING_LAYER,
    "setup": "application_setup",
    "nodes": "generic_ray_nodes",
    "core": "application_core",
    "workflows": "workflow_api_admin",
    "first": FIRST_WORKFLOW_LAYER,
    "rq2": "application_rq2",
    "showcase": SHOWCASE_LAYER,
    "protocol": "application_protocol",
}


def checked(argv, *, data=None, timeout=40):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise RuntimeError("Public qualification command failed; raw responses suppressed")
    return result.stdout


def parse_receipts(
    raw: bytes, names: tuple[str, ...], *, expected_profile: dict | None = None
) -> dict[str, bytes]:
    """Accept complete source receipts, never a truncated log or partial success."""
    log_limit = MAX_RECEIPT_BYTES + 4096 if names == ("reporting",) else 65536
    if len(raw) > log_limit:
        raise ValueError("Receipt log exceeds its byte limit")
    parsed = []
    for line in raw.splitlines():
        if not line.startswith(b"{"):
            continue
        value = json.loads(line)
        if (
            not isinstance(value, dict)
            or type(value.get("layer")) is not str
            or value["layer"] not in LAYERS.values()
        ):
            continue
        receipt_limit = 16384
        if value["layer"] == "workflow_api_admin":
            receipt_limit = WORKFLOW_RECEIPT_MAX_BYTES
        elif value["layer"] == REPORTING_LAYER:
            receipt_limit = MAX_RECEIPT_BYTES
        elif value["layer"] == SHOWCASE_LAYER:
            receipt_limit = SHOWCASE_RECEIPT_MAX_BYTES
        if (
            len(line) > receipt_limit
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("status") != "passed"
            or value.get("complete_application_gate") is not False
        ):
            raise ValueError("Missing or failed application receipt")
        if value["layer"] == REPORTING_LAYER:
            validate_reporting_receipt(value)
        elif value["layer"] == FIRST_WORKFLOW_LAYER:
            validate_first_workflow_receipt(value)
            if expected_profile is not None and value["qualification_profile"] != expected_profile:
                raise ValueError("First-workflow receipt belongs to another resource profile")
        elif value["layer"] == "application_core":
            from qualification.application.lease_policy import validate_evidence as validate_lease
            from qualification.application.protected_runtime import validate as validate_protected
            from qualification.application.runtime_env_maintenance import (
                validate_evidence as validate_maintenance,
            )
            from qualification.application.runtime_env_refusal import validate_evidence

            validate_evidence(value.get("runtime_env_refusals"))
            validate_lease(value.get("lease_policy"))
            validate_maintenance(value.get("runtime_env_maintenance"))
            validate_result_evidence(value.get("ordinary_results"))
            validate_protected(value.get("protected_runtime"))
        elif value["layer"] == "application_rq2":
            from qualification.application.run_rq2 import validate_receipt

            validate_receipt(value)
        elif value["layer"] == SHOWCASE_LAYER:
            validate_showcase_receipt(value)
        elif value["layer"] == "application_protocol":
            from qualification.application.run_protocol import validate_receipt

            validate_receipt(value)
        from qualification.application.safe_diagnostics import passed_receipt

        passed_receipt(value)
        parsed.append((value, line))
    if len(parsed) != len(names):
        raise ValueError("Receipt count differs from the required assertions")
    result = {}
    for name, (value, line) in zip(names, parsed, strict=True):
        if value["layer"] != LAYERS[name.rsplit("-", 1)[-1]]:
            raise ValueError("Receipt order differs from the required assertions")
        if (
            value["layer"] in {SHOWCASE_LAYER, "application_protocol"}
            and value.get("generation") != name.split("-", 1)[0]
        ):
            raise ValueError("Receipt belongs to another Ray generation")
        if value["layer"] == "application_rq2" and value["generation"] != name.split("-")[0]:
            raise ValueError("Request-reference receipt belongs to another generation")
        result[name] = line
    return result


def collect(
    kubectl, namespace: str, output: Path, image: str, *, expected_profile: dict | None = None
) -> dict:
    pods = json.loads(checked([*kubectl, "get", "pods", "-n", namespace, "-o", "json"]))["items"]
    identities = []
    collected = {}
    (web,) = [pod for pod in pods if pod["metadata"].get("labels", {}).get("app") == "django-web"]
    web_runtime = observe_web_runtime(web, image)
    for app, (container, names) in RECEIPTS.items():
        (pod,) = [pod for pod in pods if pod["metadata"].get("labels", {}).get("app") == app]
        statuses = pod["status"].get("containerStatuses", []) + pod["status"].get(
            "initContainerStatuses", []
        )
        (status,) = [item for item in statuses if item["name"] == container]
        (spec,) = [
            item
            for item in pod["spec"].get("containers", []) + pod["spec"].get("initContainers", [])
            if item["name"] == container
        ]
        log_limit = MAX_RECEIPT_BYTES + 4096 if names == ("reporting",) else 65536
        raw = checked(
            [
                *kubectl,
                "logs",
                pod["metadata"]["name"],
                "-n",
                namespace,
                "-c",
                container,
                f"--limit-bytes={log_limit + 1}",
            ]
        )
        if len(raw) > log_limit:
            raise ValueError("Receipt log exceeds its byte limit")
        from qualification.application.safe_diagnostics import lines as safe_lines

        diagnostics = safe_lines(raw)
        if diagnostics:
            (output / f"{app}-diagnostics.json").write_text(
                json.dumps(diagnostics), encoding="utf-8"
            )
        if (
            spec["image"] != image
            or status.get("imageID", "").removeprefix("containerd://").rsplit("@", 1)[-1]
            != image.rsplit("@", 1)[-1]
            or status["restartCount"] != 0
            or status.get("state", {}).get("terminated", {}).get("exitCode") != 0
        ):
            raise ValueError("Receipt producer is not the completed candidate container")
        for name, data in parse_receipts(raw, names, expected_profile=expected_profile).items():
            (output / f"{name}.json").write_bytes(data)
            collected[name] = data
        identities.append(
            {
                "pod": pod["metadata"]["name"],
                "uid": pod["metadata"]["uid"],
                "container": container,
                "image_id": status["imageID"],
            }
        )
    before = (output / "before-nodes.json").read_bytes()
    after = json.loads((output / "after-nodes.json").read_bytes())
    if (
        after["cold_replacement"] is not True
        or after["previous_receipt_sha256"] != hashlib.sha256(before).hexdigest()
    ):
        raise ValueError("Cold generation is not bound to the original receipt")
    (output / "producers.json").write_text(json.dumps(identities, indent=2), encoding="utf-8")
    from qualification.application.protected_logs import validate_receipt as validate_log_receipt

    for generation in ("before", "after"):
        with (output / f"{generation}-protected-logs.json").open("rb") as stream:
            raw = stream.read(2049)
        if len(raw) > 2048:
            raise ValueError("Protected log receipt exceeds its byte limit")
        validate_log_receipt(json.loads(raw), generation)
    validate_live_admin_assets(collected)
    return web_runtime


def validate_live_admin_assets(receipts: dict[str, bytes]) -> None:
    """Require both identities on every current browser case in both generations."""
    from qualification.application.workflow_fixtures import workflow_cases

    baseline = None
    try:
        for generation in ("before", "after"):
            first = json.loads(receipts[f"{generation}-first"])
            trials = first["trials"]
            if not isinstance(trials, list) or len(trials) != 2:
                raise ValueError("Incomplete live Admin asset observations")
            groups = [trial["observations"] for trial in trials]
            if any(not isinstance(group, list) or len(group) != 1 for group in groups):
                raise ValueError("Incomplete live Admin asset observations")
            workflows = json.loads(receipts[f"{generation}-workflows"])["observations"]
            cases = workflow_cases()
            if not isinstance(workflows, dict) or set(workflows) != {case.name for case in cases}:
                raise ValueError("Incomplete live Admin asset observations")
            for case in cases:
                attempts = workflows[case.name]
                if (
                    not isinstance(attempts, list)
                    or len(attempts) != len(case.states)
                    or any("browser_contract" in attempt for attempt in attempts[:-1])
                ):
                    raise ValueError("Incomplete live Admin asset observations")
                groups.append(attempts)
            showcase = json.loads(receipts[f"{generation}-showcase"])["observation"]
            groups.append([showcase])
            for group in groups:
                contract = group[-1]["browser_contract"]
                assets = contract["admin_assets"]
                if (
                    contract["status"] != "passed"
                    or not isinstance(assets, dict)
                    or set(assets) != {"task_live.js", "workflow_diagnostics.js"}
                ):
                    raise ValueError("Incomplete live Admin asset observations")
                for identity in assets.values():
                    if (
                        not isinstance(identity, dict)
                        or set(identity) != {"bytes", "sha256"}
                        or type(identity["bytes"]) is not int
                        or not 0 < identity["bytes"] <= 64 * 1024
                        or not isinstance(identity["sha256"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None
                    ):
                        raise ValueError("Incomplete live Admin asset observations")
                if baseline is None:
                    baseline = assets
                elif assets != baseline:
                    raise ValueError("Live Admin asset identities differ across observations")
    except (KeyError, TypeError, IndexError):
        raise ValueError("Incomplete live Admin asset observations") from None


def observe_web_runtime(pod: dict, image: str) -> dict:
    """Inspect the regular web container separately from its setup producer."""
    (spec,) = [item for item in pod["spec"]["containers"] if item["name"] == "web"]
    (status,) = [item for item in pod["status"]["containerStatuses"] if item["name"] == "web"]
    image_id = status.get("imageID")
    state = status.get("state")
    if (
        pod["metadata"].get("deletionTimestamp")
        or spec["image"] != image
        or runtime_image_digest(image_id) != image.rsplit("@", 1)[-1]
        or status.get("ready") is not True
        or type(status.get("restartCount")) is not int
        or status["restartCount"] != 0
        or not isinstance(state, dict)
        or set(state) != {"running"}
        or not isinstance(state["running"], dict)
    ):
        raise ValueError("Regular web runtime does not match the admitted image")
    result = {
        "pod": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "container": "web",
        "image": image,
        "image_id": image_id,
        "ready": True,
        "restarts": 0,
    }
    validate_web_runtime(result, image)
    return result


def runtime_image_digest(value: object) -> str | None:
    """Accept anchored repository/bare digests and the two runtime prefixes."""
    if not isinstance(value, str):
        return None
    for prefix in ("containerd://", "docker-pullable://"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix)
            break
    if "://" in value:
        return None
    match = re.fullmatch(r"(?:[a-zA-Z0-9][a-zA-Z0-9./:_-]{0,239}@)?(sha256:[0-9a-f]{64})", value)
    return match[1] if match is not None else None


def validate_web_runtime(value: object, image: str) -> None:
    """Require the complete typed snapshot before retaining a passing summary."""
    if (
        not isinstance(value, dict)
        or set(value) != {"pod", "uid", "container", "image", "image_id", "ready", "restarts"}
        or not isinstance(value["pod"], str)
        or re.fullmatch(r"django-web-[a-z0-9-]{1,63}", value["pod"]) is None
        or not isinstance(value["uid"], str)
        or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["uid"]) is None
        or value["container"] != "web"
        or value["image"] != image
        or runtime_image_digest(value["image_id"]) != image.rsplit("@", 1)[-1]
        or value["ready"] is not True
        or type(value["restarts"]) is not int
        or value["restarts"] != 0
    ):
        raise ValueError("Regular web runtime observation is incomplete")


def diagnose(kubectl, namespace: str, output: Path) -> None:
    """Retain fixed status/event fields; raw Pod logs are never public artifacts."""
    for resource in ("rayclusters", "pods", "events"):
        try:
            items = json.loads(checked([*kubectl, "get", resource, "-n", namespace, "-o", "json"]))[
                "items"
            ]
            from qualification.application.safe_diagnostics import resources as safe_resources

            raw = json.dumps(safe_resources(resource, items), indent=2).encode()
            if len(raw) <= 65536:
                (output / f"diagnostic-{resource}.json").write_bytes(raw)
            if resource == "pods":
                diagnose_progress_actors(kubectl, namespace, output, items)
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            print(f"Could not retain bounded {resource} diagnostics", flush=True)


def diagnose_progress_actors(kubectl, namespace: str, output: Path, pods: list) -> None:
    """Read bounded State data only after failure, before owned cleanup."""
    heads = [
        pod
        for pod in pods
        if pod["metadata"].get("labels", {}).get("app") == "ray-head"
        and not pod["metadata"].get("deletionTimestamp")
    ]
    if len(heads) != 1:
        return
    try:
        source = (PROFILE / "actor_diagnostics.py").read_text(encoding="utf-8")
        raw = checked(
            [
                *kubectl,
                "exec",
                "--request-timeout=5s",
                "-n",
                namespace,
                heads[0]["metadata"]["name"],
                "-c",
                "ray-head",
                "--",
                "timeout",
                "--signal=TERM",
                "--kill-after=1s",
                "12s",
                "python",
                "-c",
                source,
            ],
            timeout=15,
        )
        if len(raw) > 16384:
            raise ValueError("Oversized actor diagnostics")
        from qualification.application.safe_diagnostics import actor as safe_actor

        receipt = safe_actor(json.loads(raw))
        (output / "diagnostic-progress-actors.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Could not retain bounded actor diagnostics", flush=True)


def run_test(command, kubectl, namespace: str) -> None:
    """Keep progress visible and stop on crashed containers instead of readiness timeout."""
    process = subprocess.Popen(command, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + APPLICATION_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Application core exceeded its accounted wall-clock limit")
            try:
                code = process.wait(timeout=min(15, remaining))
            except subprocess.TimeoutExpired:
                pods = json.loads(
                    checked([*kubectl, "get", "pods", "-n", namespace, "-o", "json"])
                )["items"]
                clusters = json.loads(
                    checked([*kubectl, "get", "rayclusters", "-n", namespace, "-o", "json"])
                )["items"]
                active_clusters = {
                    cluster["metadata"]["uid"]
                    for cluster in clusters
                    if not cluster["metadata"].get("deletionTimestamp")
                }
                for pod in pods:
                    if pod["metadata"].get("deletionTimestamp"):
                        continue
                    if any(
                        owner.get("kind") == "RayCluster"
                        and owner.get("uid") not in active_clusters
                        for owner in pod["metadata"].get("ownerReferences", [])
                    ):
                        continue
                    status = pod.get("status", {})
                    containers = status.get("containerStatuses", []) + status.get(
                        "initContainerStatuses", []
                    )
                    for container in containers:
                        for state in (container.get("state", {}), container.get("lastState", {})):
                            terminated = state.get("terminated", {})
                            if terminated.get("exitCode", 0) != 0:
                                exit_code = terminated.get("exitCode")
                                print(
                                    json.dumps(
                                        {
                                            "application_container_failed": True,
                                            "exit_code": exit_code
                                            if type(exit_code) is int and 0 <= exit_code <= 255
                                            else None,
                                        }
                                    ),
                                    flush=True,
                                )
                                raise RuntimeError("Application container failed") from None
                continue
            if code:
                raise RuntimeError("Chainsaw failed; inspect the foreground output")
            return
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def caller_kubeconfig_paths() -> str:
    """Preserve path selection, never read or copy kubeconfig credential content."""
    configured = os.environ.get("KUBECONFIG", "")
    paths = [item for item in configured.split(os.pathsep) if item]
    if not paths:
        paths = [str(Path.home() / ".kube" / "config")]
    return os.pathsep.join(str(Path(item).expanduser().resolve()) for item in paths)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resource-profile", choices=RESOURCE_PROFILES, default="standard")
    parser.add_argument(
        "--validation-intent", choices=("acceptance", "diagnostic"), default="acceptance"
    )
    args = parser.parse_args(argv)
    try:
        profile = select_profile(args.resource_profile, args.validation_intent)
    except ValueError as error:
        parser.error(str(error))
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be a digest-pinned repository reference")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", args.storage_class):
        parser.error("--storage-class must name the admitted StorageClass")
    git = ["git", "-C", str(ROOT)]
    if checked([*git, "status", "--porcelain"]).strip():
        parser.error("Run the committed candidate from a clean checkout")
    source_tree = checked([*git, "rev-parse", "HEAD^{tree}"]).decode().strip()
    if checked(["chainsaw", "version"]).splitlines()[0] != b"Version: 0.2.15":
        parser.error("Chainsaw 0.2.15 is required")
    args.output = args.output.resolve()
    if args.output.is_relative_to(ROOT):
        parser.error("--output must be outside the committed checkout")
    args.output.mkdir(parents=True, exist_ok=False)
    values = args.output / "values.json"
    values.write_text(
        json.dumps(
            {
                "applicationImage": args.image,
                "storageClass": args.storage_class,
                "protectedLogOutput": str(args.output),
                "protectedLogContext": args.context,
                "protectedLogKubeconfigPaths": caller_kubeconfig_paths(),
                **chainsaw_values(profile),
            }
        )
    )
    namespace = "django-ray-core-" + secrets.token_hex(8)
    kubectl = ["kubectl", "--context", args.context, "--request-timeout=30s"]
    uid = None
    passed = cleaned = False
    worker_images = None
    worker_images_failure = None
    web_runtime = None
    try:
        created = json.loads(checked([*kubectl, "create", "namespace", namespace, "-o", "json"]))
        uid = created["metadata"]["uid"]
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "application-credentials", "namespace": namespace},
            "immutable": True,
            "stringData": {key: secrets.token_urlsafe(32) for key in CREDENTIAL_KEYS},
        }
        checked([*kubectl, "create", "-f", "-"], data=json.dumps(secret).encode())
        command = ["chainsaw", "test", "--config", str(PROFILE / "chainsaw.yaml")]
        command += ["--kube-context", args.context, "--namespace", namespace]
        command += ["--kube-request-timeout=30s", "--report-format=XML", "--no-color"]
        command += ["--values", str(values), "--report-path", str(args.output), str(PROFILE)]
        print(f"Running application core in owned namespace {namespace}", flush=True)
        run_test(command, kubectl, namespace)
        worker_images = observe_worker_images(
            checked=checked,
            kubectl=kubectl,
            namespace=namespace,
            image=args.image,
            root=ROOT,
        )
        passed = True
    except WorkerImageObservationError as error:
        try:
            diagnostic = error.diagnostic()
            validate_worker_image_failure(diagnostic)
            worker_images_failure = diagnostic
        except (KeyError, TypeError, ValueError):
            pass
        print("Application image worker observation failed; retained evidence is incomplete.")
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        print("Application core failed; retained evidence is incomplete.")
    finally:
        if uid is not None:
            print("Collecting application receipts and removing the owned namespace", flush=True)
            try:
                web_runtime = collect(
                    kubectl, namespace, args.output, args.image, expected_profile=profile
                )
                validate_web_runtime(web_runtime, args.image)
                current_tree = checked([*git, "rev-parse", "HEAD^{tree}"]).decode().strip()
                passed = passed and current_tree == source_tree
                passed = passed and not checked([*git, "status", "--porcelain"]).strip()
            except (
                OSError,
                ImportError,
                ValueError,
                RuntimeError,
                KeyError,
                TypeError,
                AttributeError,
                subprocess.SubprocessError,
            ):
                passed = False
            if not passed:
                diagnose(kubectl, namespace, args.output)
            try:
                current = json.loads(
                    checked([*kubectl, "get", "namespace", namespace, "-o", "json"])
                )
                if current["metadata"]["uid"] != uid:
                    raise ValueError("Namespace ownership changed")
                checked(
                    [*kubectl, "delete", "namespace", namespace, "--wait=true", "--timeout=180s"],
                    timeout=190,
                )
                cleaned = not checked(
                    [*kubectl, "get", "namespace", namespace, "--ignore-not-found", "-o", "name"]
                ).strip()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                print(f"Owned namespace cleanup failed: {namespace}")
        summary = {
            "status": "passed" if passed and cleaned else "failed",
            "namespace": namespace,
            "namespace_uid": uid,
            "namespace_removed": cleaned,
            "image": args.image,
            "source_tree": source_tree,
            "cold_ray": "required",
            "qualification_profile": profile,
            "complete_application_gate": False,
            "worker_images": worker_images,
            "web_runtime": web_runtime,
        }
        if worker_images_failure is not None:
            summary["worker_images_failure"] = worker_images_failure
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if passed and cleaned else 1


if __name__ == "__main__":
    raise SystemExit(main())
