"""Corroborate one completed native observer against owned Kubernetes snapshots.

The host must fetch these snapshots and logs from its admitted cluster and
recheck namespace/resource ownership. This parser does not authenticate caller
supplied data and does not establish any upgrade phase by itself.
"""

from __future__ import annotations

import json
import platform
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_LOG_BYTES = 256 * 1024


class ObserverError(ValueError):
    """Fixed refusal without raw logs, resource names, or provider responses."""


def _require(condition: bool) -> None:
    if not condition:
        raise ObserverError("upgrade-observer-refused")


def _matches(expected, actual) -> bool:
    if type(expected) is dict:
        return type(actual) is dict and all(
            key in actual and _matches(value, actual[key]) for key, value in expected.items()
        )
    if type(expected) is list:
        return (
            type(actual) is list
            and len(expected) == len(actual)
            and all(_matches(left, right) for left, right in zip(expected, actual, strict=True))
        )
    return type(expected) is type(actual) and expected == actual


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _time(value):
    _require(type(value) is str and len(value) <= 32)
    parsed = datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed))
    return parsed


def _constant(_value):
    raise ObserverError("upgrade-observer-refused")


def corroborate_observer(expected: dict, job: dict, pod: dict, raw: bytes, *, job_uid: str) -> dict:
    """Bind a single successful observer result to its expected Job and image.

    Expected is the source renderer's observer manifest, not an arbitrary
    workload supplied by the observer. job_uid comes from the host's successful
    create response. No retries, sidecars, init containers, or partial logs are
    accepted. Kubernetes defaulted fields may supplement the submitted spec.
    """
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_LOG_BYTES)
        _require(type(job_uid) is str and 0 < len(job_uid) <= 128)
        _require(expected["kind"] == job["kind"] == "Job" and pod["kind"] == "Pod")
        _require(_matches(expected["metadata"], job["metadata"]))
        _require(job["metadata"]["uid"] == job_uid)
        _require(_matches(expected["spec"], job["spec"]))
        _require(type(job["status"].get("succeeded")) is int and job["status"]["succeeded"] == 1)
        _require(job["status"].get("failed", 0) == job["status"].get("active", 0) == 0)
        _require(
            any(
                c.get("type") == "Complete" and c.get("status") == "True"
                for c in job["status"].get("conditions", [])
            )
        )
        _require(
            not any(
                c.get("type") in {"Failed", "FailureTarget"} and c.get("status") == "True"
                for c in job["status"].get("conditions", [])
            )
        )
        _require(type(pod["metadata"].get("uid")) is str and 0 < len(pod["metadata"]["uid"]) <= 128)
        _require(pod["metadata"]["namespace"] == expected["metadata"]["namespace"])
        owners = pod["metadata"].get("ownerReferences", [])
        _require(
            len(owners) == 1
            and _matches(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": expected["metadata"]["name"],
                    "uid": job_uid,
                    "controller": True,
                },
                owners[0],
            )
        )
        template = expected["spec"]["template"]
        _require(_matches(template["metadata"], pod["metadata"]))
        _require(_matches(template["spec"], pod["spec"]))
        _require(
            not pod["spec"].get("initContainers") and not pod["spec"].get("ephemeralContainers")
        )
        _require(pod["status"]["phase"] == "Succeeded")
        (container,) = template["spec"]["containers"]
        (status,) = pod["status"]["containerStatuses"]
        _require(container["name"] == status["name"] == "observer")
        _require(type(status["restartCount"]) is int and status["restartCount"] == 0)
        digest = container["image"].rsplit("@", 1)[1]
        _require(
            status["imageID"]
            .removeprefix("containerd://")
            .removeprefix("docker-pullable://")
            .rsplit("@", 1)[-1]
            == digest
        )
        terminated = status["state"]["terminated"]
        _require(type(terminated["exitCode"]) is int and terminated["exitCode"] == 0)
        start, finish = _time(terminated["startedAt"]), _time(terminated["finishedAt"])
        candidates = [line for line in raw.splitlines() if line.startswith(b"{")]
        _require(len(candidates) == 1)
        result = json.loads(candidates[0], object_pairs_hook=_object, parse_constant=_constant)
        _require(
            set(result)
            == {
                "schema",
                "action",
                "pid",
                "python",
                "observed_at",
                "observations",
                "complete_upgrade_gate",
            }
        )
        _require(type(result["schema"]) is int and result["schema"] == 1)
        _require(result["action"] == container["args"][0])
        _require(type(result["pid"]) is int and 0 < result["pid"] < 2**31)
        environment = {item["name"]: item.get("value") for item in container["env"]}
        _require(result["python"] == environment["DJANGO_RAY_UPGRADE_PYTHON_VERSION"])
        # Kubernetes metav1.Time serializes whole seconds. A result emitted in
        # the final second may have a later fractional timestamp than finishedAt.
        observed = _time(result["observed_at"])
        _require(start <= finish and start <= observed)
        _require(
            observed < finish + timedelta(seconds=1)
            if "." not in terminated["finishedAt"]
            else observed <= finish
        )
        _require(type(result["observations"]) is dict and result["complete_upgrade_gate"] is False)
        return {
            "pod_uid": pod["metadata"]["uid"],
            "job_uid": job_uid,
            "started_at": terminated["startedAt"],
            "finished_at": terminated["finishedAt"],
            "result": result,
            "complete_upgrade_gate": False,
        }
    except Exception:
        raise ObserverError("upgrade-observer-refused") from None


def collect_observer(
    read, *, context: str, namespace_uid: str, expected: dict, job_uid: str
) -> dict:
    """Read one completed observer with the host's bounded kubectl transport.

    read accepts a tuple of kubectl arguments and returns bounded stdout bytes;
    it must use a fixed executable, a finite timeout, and a live output limit.
    This read-only collector never creates, deletes, retries, or adopts a Job.
    """
    try:
        _require(
            type(context) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", context) is not None
        )
        _require(type(namespace_uid) is str and 0 < len(namespace_uid) <= 128)
        namespace, name = expected["metadata"]["namespace"], expected["metadata"]["name"]
        for value in (namespace, name):
            _require(
                type(value) is str
                and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value) is not None
            )
        prefix = ("--context", context, "--request-timeout=15s")

        def document(*arguments):
            raw = read((*prefix, *arguments, "-o", "json"))
            _require(type(raw) is bytes and 0 < len(raw) <= MAX_LOG_BYTES)
            return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)

        def owned_namespace():
            value = document("get", "namespace", namespace)
            _require(value["metadata"]["uid"] == namespace_uid)
            _require(
                value["metadata"]["name"] == namespace
                and not value["metadata"].get("deletionTimestamp")
            )

        owned_namespace()
        job = document("get", "job", name, "-n", namespace)
        _require(job["metadata"]["uid"] == job_uid)
        listing = document(
            "get", "pods", "-n", namespace, "-l", "batch.kubernetes.io/job-name=" + name
        )
        (pod,) = listing["items"]
        pod_name = pod["metadata"]["name"]
        _require(
            type(pod_name) is str
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?", pod_name) is not None
        )
        raw = read(
            (
                *prefix,
                "logs",
                pod_name,
                "-n",
                namespace,
                "-c",
                "observer",
                "--limit-bytes=" + str(MAX_LOG_BYTES + 1),
            )
        )
        result = corroborate_observer(expected, job, pod, raw, job_uid=job_uid)
        # Re-fetch the selected pod as well as its owner. A same-name replacement
        # during logs collection cannot inherit the earlier successful snapshot.
        final_pod = document("get", "pod", pod_name, "-n", namespace)
        final_job = document("get", "job", name, "-n", namespace)
        _require(
            corroborate_observer(expected, final_job, final_pod, raw, job_uid=job_uid) == result
        )
        owned_namespace()
        return result
    except Exception:
        raise ObserverError("upgrade-observer-collection-refused") from None


def read_owned_observer(
    *,
    kubectl: Path,
    directory: Path,
    environment: dict[str, str],
    context: str,
    namespace_uid: str,
    expected: dict,
    job_uid: str,
) -> dict:
    """Use a fixed Linux executable with live output and process limits.

    The host supplies its admitted kubeconfig and owned work directory.
    Only the collector's get/logs calls execute; no resources are mutated.
    """
    try:
        _require(platform.system() == "Linux")
        _require(kubectl.is_absolute() and kubectl.is_file())
        _require(directory.is_absolute() and directory.is_dir())
        _require(
            type(environment) is dict
            and all(type(key) is str and type(value) is str for key, value in environment.items())
        )
        executable = str(kubectl.resolve(strict=True))
        working = directory.resolve(strict=True)
        inherited = environment.copy()
        from qualification.upgrade.runtime_database import _run_client

        def read(arguments):
            return _run_client(
                (executable, *arguments),
                directory=working,
                environment=inherited,
                timeout=20,
                maximum=MAX_LOG_BYTES + 1,
            )

        return collect_observer(
            read, context=context, namespace_uid=namespace_uid, expected=expected, job_uid=job_uid
        )
    except Exception:
        raise ObserverError("upgrade-observer-transport-refused") from None
