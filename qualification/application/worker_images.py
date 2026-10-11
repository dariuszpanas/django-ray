"""Sequential source-derived sync/ML managers in the owned application image."""

from __future__ import annotations

import copy
import json
import secrets
import subprocess
import time
from pathlib import Path

# One allocation covers original quiescence and both sequential worker lifecycles.
PHASE_SECONDS = 180
CLEANUP_SECONDS = 45
MODES = ("sync", "ml")
FACTS = (
    "source_cli",
    "current_image",
    "ready",
    "result_five",
    "one_attempt",
    "sentinel_unchanged",
    "inactive_lease",
    "pod_removed",
)
SUBSTITUTIONS = (
    "owned-namespace-and-database-secrets",
    "qualification-settings-and-offline-runtime-archive",
    "existing-ray-client-dns-and-auth-profile",
    "existing-250m-cpu-768Mi-manager-footprint",
    "existing-volume-security-and-lease-readiness-profile",
    "owned-pod-sigint-graceful-stop",
)
FAILURE_CATEGORIES = ("timeout", "command", "validation", "unavailable")
TIMEOUT_SOURCES = (
    "deadline_before_command",
    "poll_reserve_reached",
    "remaining_budget_limit",
    "command_limit",
    "total_phase_limit",
    "unclassified",
)
FAILURE_PHASES = (
    "source_render",
    "original_observation",
    "scale_down",
    "quiescence",
    "initialize",
    *(
        f"{phase}_{mode}"
        for mode in MODES
        for phase in ("prepare", "create", "ready", "process", "observe", "remove", "stopped")
    ),
    "cleanup_remove_sync",
    "cleanup_remove_ml",
    "cleanup_rows",
    "cleanup_restore",
    "receipt",
    "deadline",
)
CLEANUP_PHASES = tuple(phase for phase in FAILURE_PHASES if phase.startswith("cleanup_"))
CLEANUP_FACTS = ("temporary_removed", "rows_removed", "history_preserved", "original_restored")


def validate_failure_receipt(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "phase",
            "category",
            "timeout_source",
            "cleanup_phase",
            "cleanup_category",
            "cleanup_timeout_source",
            *CLEANUP_FACTS,
        }
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or value["phase"] not in FAILURE_PHASES
        or value["category"] not in FAILURE_CATEGORIES
        or not (
            value["timeout_source"] in TIMEOUT_SOURCES
            if value["category"] == "timeout"
            else value["timeout_source"] is None
        )
        or any(type(value[key]) is not bool for key in CLEANUP_FACTS)
        or not (
            value["cleanup_phase"] is value["cleanup_category"] is None
            or (
                value["cleanup_phase"] in CLEANUP_PHASES
                and value["cleanup_category"] in FAILURE_CATEGORIES
            )
        )
        or not (
            value["cleanup_timeout_source"] in TIMEOUT_SOURCES
            if value["cleanup_category"] == "timeout"
            else value["cleanup_timeout_source"] is None
        )
    ):
        raise ValueError("Invalid worker image failure receipt")


class WorkerImageObservationError(ValueError):
    def __init__(self, receipt: dict):
        validate_failure_receipt(receipt)
        self._receipt = dict(receipt)
        super().__init__("Application image worker observation failed")

    def diagnostic(self) -> dict:
        validate_failure_receipt(self._receipt)
        return dict(self._receipt)


class _WorkerImageTimeoutError(TimeoutError):
    def __init__(self, source: str):
        self.source = source
        super().__init__("Application image worker time limit reached")


class _Diagnostics:
    def __init__(self):
        self.phase = "source_render"
        self.failure: tuple[str, str, str | None] | None = None
        self.cleanup_failure: tuple[str, str, str | None] | None = None
        self.facts = dict.fromkeys(CLEANUP_FACTS, False)

    def capture(self, error: Exception, *, cleanup: bool = False) -> None:
        timeout_source = None
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            category = "timeout"
            timeout_source = (
                error.source if isinstance(error, _WorkerImageTimeoutError) else "unclassified"
            )
        elif isinstance(error, (RuntimeError, subprocess.SubprocessError)):
            category = "command"
        elif isinstance(error, (ValueError, KeyError, TypeError)):
            category = "validation"
        else:
            category = "unavailable"
        observed = (self.phase, category, timeout_source)
        self.failure = self.failure or observed
        if cleanup:
            self.cleanup_failure = self.cleanup_failure or observed

    def receipt(self) -> dict:
        assert self.failure is not None
        cleanup_phase, cleanup_category, cleanup_timeout_source = self.cleanup_failure or (
            None,
            None,
            None,
        )
        return {
            "schema_version": 2,
            "phase": self.failure[0],
            "category": self.failure[1],
            "timeout_source": self.failure[2],
            "cleanup_phase": cleanup_phase,
            "cleanup_category": cleanup_category,
            "cleanup_timeout_source": cleanup_timeout_source,
            **self.facts,
        }


def source_commands(raw: bytes) -> dict:
    # Host help and receipt validation must remain standard-library-only.
    import yaml

    if len(raw) > 1024 * 1024:
        raise ValueError("Oversized rendered worker source")
    deployments = {
        item.get("metadata", {}).get("name"): item
        for item in yaml.safe_load_all(raw)
        if isinstance(item, dict) and item.get("kind") == "Deployment"
    }
    result = {}
    for mode in MODES:
        item = deployments[f"django-ray-worker-{mode}"]
        containers = item["spec"]["template"]["spec"]["containers"]
        if len(containers) != 1:
            raise ValueError("Unexpected source worker container count")
        container = containers[0]
        expected = ["--sync"] if mode == "sync" else []
        expected += ["--queue", mode]
        if mode == "ml":
            expected += ["--cluster", "ray://ray-head-svc:10001"]
        expected += ["--concurrency", "20"]
        command = ["python", "testproject/manage.py", "django_ray_worker"]
        if container["command"] != command or container["args"] != expected:
            raise ValueError("Shipped worker CLI changed; review the qualification contract")
        result[mode] = (command, expected)
    return result


def deployment(original: dict, commands: dict, owner: str, mode: str) -> dict:
    template = copy.deepcopy(original["spec"]["template"])
    labels = {"app": "worker-image", "image-owner": owner, "image-mode": mode}
    template["metadata"] = {"labels": labels}
    pod = template["spec"]
    containers = pod["containers"]
    if len(containers) != 1 or containers[0]["name"] != "manager":
        raise ValueError("Unexpected admitted manager shape")
    container = containers[0]
    required = {"cpu": "250m", "memory": "768Mi", "ephemeral-storage": "256Mi"}
    if container["resources"] != {"limits": required, "requests": required}:
        raise ValueError("Admitted manager footprint changed")
    command, args = commands[mode]
    container["command"] = command
    container["args"] = [
        "ray://ray-head:10001" if value == "ray://ray-head-svc:10001" else value for value in args
    ]
    container["env"] = [
        value for value in container.get("env", []) if value["name"] != "DJANGO_RAY_QUEUES"
    ] + [{"name": "DJANGO_RAY_QUEUES", "value": mode}]
    # This fixture owns PID1 in this exact Pod. Kubernetes executes the hook
    # before TERM; Ray may otherwise replace the manager's TERM handler.
    container["lifecycle"] = {
        "preStop": {
            "exec": {"command": ["python", "-c", "import os,signal; os.kill(1,signal.SIGINT)"]}
        }
    }
    pod["terminationGracePeriodSeconds"] = 10
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"mode-{owner}-{mode}", "labels": labels},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": template,
        },
    }


def validate_receipt(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "modes",
            "substitutions",
            "rows_removed",
            "history_preserved",
            "original_restored",
        }
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["substitutions"] != list(SUBSTITUTIONS)
        or not isinstance(value["modes"], dict)
        or set(value["modes"]) != set(MODES)
        or any(
            value[key] is not True
            for key in ("rows_removed", "history_preserved", "original_restored")
        )
    ):
        raise ValueError("Invalid worker image receipt")
    for mode in MODES:
        facts = value["modes"][mode]
        if (
            not isinstance(facts, dict)
            or set(facts) != set(FACTS)
            or any(facts[key] is not True for key in FACTS)
        ):
            raise ValueError("Incomplete worker image mode receipt")


class Observer:
    def __init__(self, checked, kubectl: list[str], namespace: str, deadline: float):
        self.checked = checked
        self.kubectl = kubectl
        self.namespace = namespace
        self.deadline = deadline

    def call(self, *args: str, data: dict | None = None) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _WorkerImageTimeoutError("deadline_before_command")
        try:
            return self.checked(
                [*self.kubectl, "-n", self.namespace, *args],
                data=None if data is None else json.dumps(data).encode(),
                timeout=min(10, remaining),
            )
        except subprocess.TimeoutExpired:
            # Equality belongs to the remaining-budget bound. This records the
            # selected local limit, not the remote cause of the timeout.
            source = "remaining_budget_limit" if remaining <= 10 else "command_limit"
            raise _WorkerImageTimeoutError(source) from None

    def get(self, kind: str, name: str) -> dict:
        return json.loads(self.call("get", kind, name, "-o", "json"))

    def pods(self, selector: str) -> list:
        items = json.loads(self.call("get", "pods", "-l", selector, "-o", "json"))["items"]
        if len(items) > 4:
            raise ValueError("Unexpected owned worker Pod count")
        return items

    def wait(self, predicate):
        while True:
            result = predicate()
            if result:
                return result
            if self.deadline - time.monotonic() <= 1:
                raise _WorkerImageTimeoutError("poll_reserve_reached")
            time.sleep(0.25)

    def data(
        self,
        web: str,
        owner: str,
        action: str,
        mode: str | None = None,
        hostname: str | None = None,
    ) -> dict:
        args = [
            "exec",
            web,
            "-c",
            "web",
            "--",
            "python",
            "-m",
            "qualification.application.worker_image_data",
            owner,
            action,
        ]
        if mode:
            args += ["--mode", mode]
        if hostname:
            args += ["--hostname", hostname]
        raw = self.call(*args)
        if len(raw) > 4096:
            raise ValueError("Oversized worker image data receipt")
        value = json.loads(raw)
        if not isinstance(value, dict) or any(type(item) is not bool for item in value.values()):
            raise ValueError("Invalid worker image data receipt")
        return value

    def remove(self, name: str, owner: str, expected_uid: str | None = None) -> None:
        # Reconcile an ambiguous create response using the unique owned name
        # and label; never delete a same-named foreign Deployment.
        raw = self.call("get", "deployment", name, "--ignore-not-found", "-o", "json")
        if raw.strip():
            item = json.loads(raw)
            if item["metadata"].get("labels", {}).get("image-owner") != owner:
                raise ValueError("Worker image Deployment ownership changed")
            if expected_uid is not None and item["metadata"].get("uid") != expected_uid:
                raise ValueError("Worker image Deployment incarnation changed")
            self.call("delete", "deployment", name, "--cascade=foreground", "--wait=false")
        selector = f"image-owner={owner},image-mode={name.rsplit('-', 1)[-1]}"

        def absent():
            controller = self.call("get", "deployment", name, "--ignore-not-found", "-o", "json")
            replicas = json.loads(self.call("get", "replicasets", "-l", selector, "-o", "json"))[
                "items"
            ]
            return not controller.strip() and not replicas and not self.pods(selector)

        self.wait(absent)


def ready(pods: list, image: str) -> dict | None:
    if len(pods) != 1:
        return None
    pod = pods[0]
    if pod["metadata"].get("deletionTimestamp"):
        return None
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if len(statuses) != 1 or len(pod["spec"]["containers"]) != 1:
        return None
    status = statuses[0]
    if (
        pod["spec"]["containers"][0]["image"] != image
        or type(status.get("restartCount")) is not int
        or status.get("restartCount") != 0
    ):
        raise ValueError("Worker image identity or restart count differs")
    image_id = status.get("imageID")
    if image_id in (None, ""):
        # ContainerCreating has no observed image identity yet. Keep the same
        # bounded poll; a running/ready container still needs its actual digest.
        if status.get("ready") is False and set(status.get("state", {})) == {"waiting"}:
            return None
        raise ValueError("Started worker container lacks its image identity")
    if (
        not isinstance(image_id, str)
        or image_id.removeprefix("containerd://").rsplit("@", 1)[-1] != image.rsplit("@", 1)[-1]
    ):
        raise ValueError("Worker image identity differs")
    if status.get("ready") is True and "running" in status.get("state", {}):
        return pod
    return None


def run(*, checked, kubectl: list[str], namespace: str, image: str, root: Path) -> dict:
    """Own a fresh bounded phase after the separate cold workload succeeds."""
    diagnostics = _Diagnostics()
    try:
        return _observe(
            checked=checked,
            kubectl=kubectl,
            namespace=namespace,
            image=image,
            root=root,
            diagnostics=diagnostics,
        )
    except Exception as error:
        diagnostics.capture(error)
        raise WorkerImageObservationError(diagnostics.receipt()) from None


def _observe(*, checked, kubectl, namespace, image, root, diagnostics):
    started = time.monotonic()
    phase_end = started + PHASE_SECONDS
    observer = Observer(checked, kubectl, namespace, phase_end - CLEANUP_SECONDS)
    try:
        rendered_source = checked(
            [*kubectl, "kustomize", str(root / "k8s/overlays/kuberay-kind")], timeout=10
        )
    except subprocess.TimeoutExpired:
        raise _WorkerImageTimeoutError("command_limit") from None
    commands = source_commands(rendered_source)
    diagnostics.phase = "original_observation"
    original = observer.get("deployment", "django-manager")
    if original["spec"].get("replicas") != 1:
        raise ValueError("Original manager is not a single replica")
    original_uid = original["metadata"]["uid"]
    original_pod = ready(observer.pods("app=django-manager"), image)
    web_pods = observer.pods("app=django-web")
    if original_pod is None or len(web_pods) != 1:
        raise ValueError("Original application manager or web Pod is unavailable")
    web = web_pods[0]["metadata"]["name"]
    owner = secrets.token_hex(12)
    manifests = {mode: deployment(original, commands, owner, mode) for mode in MODES}
    completed = {}
    created_uids = {}
    cleanup = {"rows_removed": False, "history_preserved": False}
    restored = False
    failure = None
    try:
        diagnostics.phase = "scale_down"
        observer.call("scale", "deployment", "django-manager", "--replicas=0")
        # The controller can no longer restart this manager. Signal only the
        # still-present exact Pod/container; namespace deletion owns fallback.
        remaining = observer.pods("app=django-manager")
        if remaining and remaining[0]["metadata"]["uid"] == original_pod["metadata"]["uid"]:
            try:
                observer.call(
                    "exec",
                    original_pod["metadata"]["name"],
                    "-c",
                    "manager",
                    "--",
                    "python",
                    "-c",
                    "import os,signal; os.kill(1,signal.SIGINT)",
                )
            except RuntimeError:
                pass  # An already-exited container must still pass lease checks.

        def original_stopped():
            current = observer.get("deployment", "django-manager")
            if current["metadata"]["uid"] != original_uid:
                raise ValueError("Original manager incarnation changed")
            sets = json.loads(
                observer.call("get", "replicasets", "-l", "app=django-manager", "-o", "json")
            )["items"]
            if len(sets) > 4:
                raise ValueError("Unexpected original manager controller count")
            return (
                current["spec"].get("replicas") == 0
                and current.get("status", {}).get("observedGeneration", 0)
                >= current["metadata"]["generation"]
                and not any(item["spec"].get("replicas", 0) for item in sets)
                and not observer.pods("app=django-manager")
            )

        diagnostics.phase = "quiescence"
        observer.wait(original_stopped)
        diagnostics.phase = "initialize"
        if observer.data(web, owner, "initialize") != {"initialized": True}:
            raise ValueError("Worker image state was not initialized")
        for mode in MODES:
            diagnostics.phase = f"prepare_{mode}"
            if observer.data(web, owner, "prepare", mode) != {"prepared": True}:
                raise ValueError("Image task was not prepared")
            manifest = manifests[mode]
            diagnostics.phase = f"create_{mode}"
            created = json.loads(observer.call("create", "-f", "-", "-o", "json", data=manifest))
            created_uid = created["metadata"]["uid"]
            if (
                not isinstance(created_uid, str)
                or not 1 <= len(created_uid) <= 64
                or created["metadata"]["name"] != manifest["metadata"]["name"]
                or created["metadata"].get("labels", {}).get("image-owner") != owner
            ):
                raise ValueError("Created image Deployment identity differs")
            created_uids[mode] = created_uid
            diagnostics.phase = f"ready_{mode}"
            pod = observer.wait(
                lambda mode=mode: ready(
                    observer.pods(f"image-owner={owner},image-mode={mode}"), image
                )
            )
            container = pod["spec"]["containers"][0]
            diagnostics.phase = f"process_{mode}"
            wanted = manifest["spec"]["template"]["spec"]["containers"][0]
            if container["command"] != wanted["command"] or container["args"] != wanted["args"]:
                raise ValueError("Running source worker command differs")
            raw = observer.call(
                "exec",
                pod["metadata"]["name"],
                "-c",
                "manager",
                "--",
                "python",
                "-c",
                "from pathlib import Path; import json; print(json.dumps(Path('/proc/1/cmdline').read_bytes().decode().strip('\\0').split('\\0')))",
            )
            if len(raw) > 4096 or json.loads(raw) != wanted["command"] + wanted["args"]:
                raise ValueError("Running worker process is not the shipped CLI")

            def finished(mode=mode, hostname=pod["metadata"]["name"]):
                value = observer.data(web, owner, "observe", mode, hostname)
                if value == {"complete": False}:
                    return False
                if value != {"complete": True, "one_attempt": True, "sentinel_unchanged": True}:
                    raise ValueError("Invalid image task completion receipt")
                return True

            diagnostics.phase = f"observe_{mode}"
            observer.wait(finished)
            current_pod = ready(observer.pods(f"image-owner={owner},image-mode={mode}"), image)
            if current_pod is None or any(
                current_pod["metadata"][key] != pod["metadata"][key] for key in ("name", "uid")
            ):
                raise ValueError("Task completion belongs to a replaced image Pod")
            diagnostics.phase = f"remove_{mode}"
            observer.remove(manifest["metadata"]["name"], owner, created_uid)
            diagnostics.phase = f"stopped_{mode}"
            if observer.data(web, owner, "stopped", mode, pod["metadata"]["name"]) != {
                "inactive_lease": True,
                "sentinel_unchanged": True,
            }:
                raise ValueError("Image manager did not stop gracefully")
            completed[mode] = dict.fromkeys(FACTS, True)
    except Exception as error:
        failure = error
        diagnostics.capture(error)
    finally:
        observer.deadline = phase_end
        # Independent cleanup attempts preserve safe restoration only when
        # temporary controllers are absent. The owned namespace is fallback.
        quiescent = True
        for mode in MODES:
            diagnostics.phase = f"cleanup_remove_{mode}"
            observer.deadline = min(phase_end - 35, time.monotonic() + 5)
            try:
                observer.remove(manifests[mode]["metadata"]["name"], owner, created_uids.get(mode))
            except Exception as error:
                quiescent = False
                failure = failure or error
                diagnostics.capture(error, cleanup=True)
        diagnostics.facts["temporary_removed"] = quiescent
        try:
            diagnostics.phase = "cleanup_rows"
            observer.deadline = phase_end - 25
            cleanup = observer.data(web, owner, "cleanup")
            if set(cleanup) != {"rows_removed", "history_preserved"} or any(
                type(value) is not bool for value in cleanup.values()
            ):
                raise ValueError("Image cleanup receipt shape differs")
            for key in ("rows_removed", "history_preserved"):
                diagnostics.facts[key] = cleanup[key]
            if cleanup != {"rows_removed": True, "history_preserved": True}:
                raise ValueError("Image cleanup or preservation is incomplete")
        except Exception as error:
            failure = failure or error
            diagnostics.capture(error, cleanup=True)
        try:
            diagnostics.phase = "cleanup_restore"
            observer.deadline = phase_end
            if not quiescent:
                raise ValueError("Temporary manager removal is unconfirmed; refusing overlap")
            current = observer.get("deployment", "django-manager")
            if (
                current["metadata"]["uid"] != original_uid
                or current["spec"]["template"] != original["spec"]["template"]
            ):
                raise ValueError("Original manager ownership or template changed")
            observer.call("scale", "deployment", "django-manager", "--replicas=1")
            observer.wait(lambda: ready(observer.pods("app=django-manager"), image))
            restored = True
            diagnostics.facts["original_restored"] = True
        except Exception as error:
            failure = failure or error
            diagnostics.capture(error, cleanup=True)
    if failure is not None:
        raise ValueError("Application image worker observation failed") from None
    if time.monotonic() > phase_end:
        diagnostics.phase = "deadline"
        raise _WorkerImageTimeoutError("total_phase_limit")
    receipt = {
        "schema_version": 1,
        "modes": completed,
        "substitutions": list(SUBSTITUTIONS),
        **cleanup,
        "original_restored": restored,
    }
    diagnostics.phase = "receipt"
    validate_receipt(receipt)
    return receipt
