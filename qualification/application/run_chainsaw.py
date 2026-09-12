"""Run the public Chainsaw core test on explicitly admitted Kubernetes capacity."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

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
    "assert-before": ("assertions", ("before-nodes", "before-core", "before-retirement")),
    "assert-after": ("assertions", ("after-nodes", "after-core")),
}
LAYERS = {
    "setup": "application_setup",
    "nodes": "generic_ray_nodes",
    "core": "application_core",
    "retirement": "application_retirement",
}


def checked(argv, *, data=None, timeout=40):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise RuntimeError("Public qualification command failed; raw responses suppressed")
    return result.stdout


def parse_receipts(raw: bytes, names: tuple[str, ...]) -> dict[str, bytes]:
    """Accept complete source receipts, never a truncated log or partial success."""
    if len(raw) > 65536:
        raise ValueError("Receipt log exceeds its byte limit")
    parsed = []
    for line in raw.splitlines():
        if not line.startswith(b"{"):
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("layer") not in LAYERS.values():
            continue
        if (
            len(line) > 16384
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("status") != "passed"
            or value.get("complete_application_gate") is not False
        ):
            raise ValueError("Missing or failed application receipt")
        parsed.append((value, line))
    if len(parsed) != len(names):
        raise ValueError("Receipt count differs from the required assertions")
    result = {}
    for name, (value, line) in zip(names, parsed, strict=True):
        if value["layer"] != LAYERS[name.rsplit("-", 1)[-1]]:
            raise ValueError("Receipt order differs from the required assertions")
        result[name] = line
    return result


def collect(kubectl, namespace: str, output: Path, image: str) -> None:
    pods = json.loads(checked([*kubectl, "get", "pods", "-n", namespace, "-o", "json"]))["items"]
    identities = []
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
        raw = checked(
            [
                *kubectl,
                "logs",
                pod["metadata"]["name"],
                "-n",
                namespace,
                "-c",
                container,
                "--limit-bytes=65537",
            ]
        )
        if len(raw) > 65536:
            raise ValueError("Receipt log exceeds its byte limit")
        (output / f"{app}.log").write_bytes(raw)
        if (
            spec["image"] != image
            or status.get("imageID", "").removeprefix("containerd://").rsplit("@", 1)[-1]
            != image.rsplit("@", 1)[-1]
            or status["restartCount"] != 0
            or status.get("state", {}).get("terminated", {}).get("exitCode") != 0
        ):
            raise ValueError("Receipt producer is not the completed candidate container")
        for name, data in parse_receipts(raw, names).items():
            (output / f"{name}.json").write_bytes(data)
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
    job = json.loads(
        checked([*kubectl, "get", "job", "django-manager", "-n", namespace, "-o", "json"])
    )
    transition = verify_manager_transition(
        pods,
        job,
        image,
        before_core=(output / "before-core.json").read_bytes(),
        retirement=(output / "before-retirement.json").read_bytes(),
        after_core=(output / "after-core.json").read_bytes(),
    )
    (output / "manager-transition.json").write_text(
        json.dumps(transition, sort_keys=True), encoding="utf-8"
    )
    (output / "producers.json").write_text(json.dumps(identities, indent=2), encoding="utf-8")


def verify_manager_transition(pods, job, image, *, before_core, retirement, after_core):
    """Correlate source receipts with actual replacement Job and Pod ownership.

    Foreground deletion in the serial profile already awaited the old Job and
    dependents. Require its name now belongs to a newly created Job, and that the
    old manager Pod is absent, independently of the worker's SQL cleanup receipt.
    """
    old = json.loads(retirement)
    new = json.loads(after_core)["replacement"]
    original = old["manager"]
    current = new["manager"]
    metadata = job["metadata"]
    confirmed = datetime.fromisoformat(old["cleanup_confirmed_at"])
    created = datetime.fromisoformat(metadata["creationTimestamp"])
    started = datetime.fromisoformat(current["started_at"])
    if (
        old["before_core_sha256"] != hashlib.sha256(before_core).hexdigest()
        or new["previous_retirement_sha256"] != hashlib.sha256(retirement).hexdigest()
        or new["original_history_preserved"] is not True
        or new["cluster_session"] == old["cluster_session"]
        or original["worker_id"] == current["worker_id"]
        or original["hostname"] == current["hostname"]
        # Kubernetes creation timestamps have second precision. The exact DB
        # incarnation must still start strictly after cleanup confirmation.
        or not confirmed.replace(microsecond=0) <= created <= started <= datetime.now(UTC)
        or started <= confirmed
        or created <= datetime.fromisoformat(original["started_at"])
        or metadata["name"] != "django-manager"
        or metadata.get("deletionTimestamp")
        or job["spec"]["backoffLimit"] != 0
        or job["spec"]["template"]["spec"]["restartPolicy"] != "Never"
        or any(pod["metadata"]["name"] == original["hostname"] for pod in pods)
    ):
        raise ValueError("Original manager was not reaped before its replacement")
    (pod,) = [p for p in pods if p["metadata"].get("labels", {}).get("app") == "django-manager"]
    (owner,) = [
        o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller") is True
    ]
    (container,) = pod["spec"]["containers"]
    (status,) = pod["status"]["containerStatuses"]
    if (
        pod["metadata"]["name"] != current["hostname"]
        or pod["metadata"].get("deletionTimestamp")
        or owner["kind"] != "Job"
        or owner["uid"] != metadata["uid"]
        or owner["name"] != metadata["name"]
        or container["name"] != "manager"
        or container["image"] != image
        or status["name"] != "manager"
        or status["restartCount"] != 0
        or "running" not in status["state"]
        or status.get("imageID", "").removeprefix("containerd://").rsplit("@", 1)[-1]
        != image.rsplit("@", 1)[-1]
    ):
        raise ValueError("New lease does not belong to the exact candidate manager Job")
    return {
        "schema_version": 1,
        "layer": "application_manager_transition",
        "status": "passed",
        "complete_application_gate": False,
        "original_manager_reaped": True,
        "original_history_preserved": True,
        "new_job_uid": metadata["uid"],
        "new_pod_uid": pod["metadata"]["uid"],
    }


def diagnose(kubectl, namespace: str, output: Path) -> None:
    """Retain bounded status and events before cleanup, excluding specs and Secrets."""
    for resource in ("rayclusters", "pods", "events"):
        try:
            items = json.loads(checked([*kubectl, "get", resource, "-n", namespace, "-o", "json"]))[
                "items"
            ]
            records = []
            for item in items[:100]:
                record = {"name": item["metadata"]["name"]}
                if resource == "events":
                    record.update(
                        {key: item.get(key) for key in ("type", "reason", "message", "count")}
                    )
                else:
                    record["status"] = item.get("status", {})
                records.append(record)
            raw = json.dumps(records, indent=2).encode()
            if len(raw) <= 65536:
                (output / f"diagnostic-{resource}.json").write_bytes(raw)
            if resource == "pods":
                for pod in [
                    p
                    for p in items
                    if p["metadata"]["name"].startswith(("ray-", "django-web-", "django-manager-"))
                ][:6]:
                    for container in pod.get("status", {}).get("containerStatuses", [])[:1]:
                        for previous in (
                            (False, True) if container.get("restartCount") else (False,)
                        ):
                            try:
                                raw = checked(
                                    [
                                        *kubectl,
                                        "logs",
                                        pod["metadata"]["name"],
                                        "-n",
                                        namespace,
                                        "-c",
                                        container["name"],
                                        "--tail=100",
                                        "--limit-bytes=32768",
                                        "--request-timeout=5s",
                                        f"--previous={str(previous).lower()}",
                                    ],
                                    timeout=10,
                                )
                                name = f"{pod['metadata']['name']}-previous-{previous}.log"
                                (output / name).write_bytes(raw[:32768])
                            except (OSError, RuntimeError, subprocess.SubprocessError):
                                pass
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
            print(f"Could not retain bounded {resource} diagnostics", flush=True)


def run_test(command, kubectl, namespace: str) -> None:
    """Keep progress visible and stop on crashed containers instead of readiness timeout."""
    process = subprocess.Popen(command, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 1800
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Application core exceeded 1800 seconds")
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
                                print(
                                    f"Container failed: {pod['metadata']['name']}/"
                                    f"{container['name']} ({terminated.get('reason', 'Error')})",
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
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
        json.dumps({"applicationImage": args.image, "storageClass": args.storage_class})
    )
    namespace = "django-ray-core-" + secrets.token_hex(8)
    kubectl = ["kubectl", "--context", args.context, "--request-timeout=30s"]
    uid = None
    passed = cleaned = False
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
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        print("Application core failed; retained evidence is incomplete.")
    finally:
        if uid is not None:
            print("Collecting application receipts and removing the owned namespace", flush=True)
            try:
                collect(kubectl, namespace, args.output, args.image)
                current_tree = checked([*git, "rev-parse", "HEAD^{tree}"]).decode().strip()
                passed = passed and current_tree == source_tree
                passed = passed and not checked([*git, "status", "--porcelain"]).strip()
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
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
            "complete_application_gate": False,
        }
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if passed and cleaned else 1


if __name__ == "__main__":
    raise SystemExit(main())
