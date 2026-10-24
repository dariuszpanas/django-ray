"""Fixed Linux RuntimeEnv attribution experiment; not a production storage budget."""

from __future__ import annotations

import argparse
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.investigate_pytest_shutdown import cleanup_owned, enable_subreaper  # noqa: E402
from scripts.require_linux import require_linux  # noqa: E402
from scripts.runtime_env_growth.evidence import (  # noqa: E402
    MAX_EVENTS,
    MAX_REPORT,
    MEMORY_BYTES,
    SCRATCH_BYTES,
    SHM_BYTES,
    TOTAL_SECONDS,
    GrowthError,
    decode,
    encode,
    validate_value,
)
from scripts.runtime_env_growth.identity import source_identity  # noqa: E402
from scripts.runtime_env_growth.report import complete_trace  # noqa: E402


class CommandError(GrowthError):
    """Retain the primary refusal independently of failed descendant cleanup."""

    def __init__(self, reason: str, *, cleanup_complete: bool) -> None:
        super().__init__(reason)
        self.cleanup_complete = cleanup_complete


def bounded_command(
    command: list[str], seconds: float, *, limit: int = MAX_REPORT, discard: bool = False
) -> bytes:
    """Own one trusted CLI tree in this otherwise child-free Linux supervisor.

    A subreaper retains detached descendants after the launcher exits. The
    Docker daemon is external to this tree; its exact container has a separate
    owner below. Never call this helper from a process owning other children.
    """
    require_linux()
    import psutil

    if psutil.Process().children(recursive=True):
        raise CommandError("command_ownership", cleanup_complete=False)
    try:
        enable_subreaper()
    except RuntimeError:
        raise CommandError("command_ownership", cleanup_complete=False) from None
    environment = dict(os.environ)
    if command[0] == "docker":
        # The fixed experiment has no remote daemon/context input.
        command = ["docker", "--host=unix:///var/run/docker.sock", *command[1:]]
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            environment.pop(key, None)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=environment,
    )
    assert process.stdout is not None
    data = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + seconds
    exited_deadline = deadline
    failure = None
    clean = False
    terminated = 0
    try:
        eof = False
        while not eof or process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise GrowthError("command_timeout")
            if process.poll() is not None:
                exited_deadline = min(exited_deadline, now + 1)
                if now >= exited_deadline:
                    raise GrowthError("command_descendants")
            for key, _ in selector.select(min(0.05, deadline - now)):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    eof = True
                    selector.unregister(key.fileobj)
                elif not discard:
                    if len(data) + len(chunk) > limit:
                        raise GrowthError("output_limit")
                    data.extend(chunk)
        if process.returncode:
            raise GrowthError("command_failed")
    except GrowthError as exc:
        failure = str(exc)
    except (OSError, psutil.Error):
        failure = "command_error"
    finally:
        try:
            clean, terminated = cleanup_owned(process)
        except (OSError, psutil.Error, subprocess.SubprocessError):
            clean = False
        finally:
            selector.close()
            process.stdout.close()
    if failure or not clean or terminated:
        raise CommandError(failure or "command_descendants", cleanup_complete=clean)
    return bytes(data)


def validate_report(report: object) -> dict[str, Any]:
    if (
        not isinstance(report, dict)
        or set(report)
        - {"schema_version", "status", "admission", "arms", "root_removed", "failure", "producer"}
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("status") not in {"complete", "failed"}
    ):
        raise GrowthError("report_invalid")
    if set(report.get("admission", {})) != {
        "memory_bytes",
        "pids",
        "cpu_quota",
        "cpu_period",
        "scratch_bytes",
        "scratch_inodes",
        "shm_bytes",
    } or any(
        type(value) is not int or not 0 < value <= 7 * 1024**3
        for value in report["admission"].values()
    ):
        raise GrowthError("report_invalid")
    if type(report.get("root_removed")) is not bool or report.get("failure") not in {
        None,
        "container_error",
    }:
        raise GrowthError("report_invalid")
    admitted = report["admission"]
    if (
        admitted["memory_bytes"] > MEMORY_BYTES
        or admitted["pids"] > 256
        or admitted["cpu_quota"] > 2 * admitted["cpu_period"]
        or admitted["scratch_bytes"] > SCRATCH_BYTES
        or admitted["scratch_inodes"] > 50000
        or admitted["shm_bytes"] > SHM_BYTES
    ):
        raise GrowthError("report_invalid")
    arms = report.get("arms")
    if not isinstance(arms, list) or len(arms) > 2:
        raise GrowthError("report_invalid")
    count = 0
    for index, arm in enumerate(arms):
        if (
            not isinstance(arm, dict)
            or set(arm)
            != {
                "arm",
                "completed_sessions",
                "failure",
                "records",
                "owned_processes_stopped",
                "forced_cleanup_count",
                "boundary_unchanged",
                "passed",
            }
            or arm["arm"] != ("raw", "django")[index]
        ):
            raise GrowthError("report_invalid")
        if arm["failure"] not in {
            None,
            "protocol",
            "deadline",
            "driver_exit",
            "output_limit",
            "driver_failure",
            "residue",
            "supervisor_error",
        }:
            raise GrowthError("report_invalid")
        if type(arm["completed_sessions"]) is not int or not 0 <= arm["completed_sessions"] <= 3:
            raise GrowthError("report_invalid")
        if (
            type(arm["forced_cleanup_count"]) is not int
            or not 0 <= arm["forced_cleanup_count"] <= 256
        ):
            raise GrowthError("report_invalid")
        if any(
            type(arm[key]) is not bool
            for key in ("owned_processes_stopped", "boundary_unchanged", "passed")
        ):
            raise GrowthError("report_invalid")
        if not isinstance(arm["records"], list):
            raise GrowthError("report_invalid")
        count += len(arm["records"])
        if count > MAX_EVENTS:
            raise GrowthError("output_limit")
        for event in arm["records"]:
            validate_value(event)
        eligible = (
            arm["failure"] is None
            and arm["completed_sessions"] == 3
            and arm["owned_processes_stopped"]
            and arm["boundary_unchanged"]
            and arm["forced_cleanup_count"] == 0
        )
        if arm["passed"] != eligible:
            raise GrowthError("report_invalid")
        if eligible:
            complete_trace(arm["records"], arm["arm"])
    if report["status"] == "complete" and not (
        len(arms) == 2
        and all(arm["passed"] for arm in arms)
        and report["root_removed"]
        and report.get("failure") is None
    ):
        raise GrowthError("report_invalid")
    encode(report)
    return report


def check_source_clean(*, artifacts_created: bool) -> None:
    command = ["git", "status", "--porcelain", "--untracked-files=all", "--", "."]
    if artifacts_created:
        command.append(":(exclude)artifacts/runtime-env-growth")
    if bounded_command(command, 10):
        raise GrowthError("source_identity")


def inspect_image(image: str, expected: dict[str, str]) -> str:
    payload = bounded_command(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .Id}}\n{{json .Config.Labels}}",
            image,
        ],
        10,
        limit=4096,
    ).splitlines()
    if len(payload) != 2:
        raise GrowthError("container_identity")
    try:
        image_id, labels = map(decode, payload)
    except ValueError:
        raise GrowthError("container_identity") from None
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise GrowthError("container_identity")
    if not isinstance(labels, dict) or any(
        labels.get("django-ray.growth." + label) != expected[key]
        for label, key in (
            ("source-commit", "source_commit"),
            ("source-tree", "source_tree"),
            ("copied-source", "copied_source_sha256"),
        )
    ):
        raise GrowthError("container_identity")
    return image_id


def inspect_owned_container(name: str, image_id: str) -> str:
    # Also resolves an ambiguous create outcome. Never remove a name alone.
    payload = bounded_command(
        [
            "docker",
            "inspect",
            "--type=container",
            "--format",
            '{{json .Id}}\n{{json .Image}}\n{{json (index .Config.Labels "django-ray.runtime-env-growth")}}',
            name,
        ],
        10,
        limit=1024,
    ).splitlines()
    if len(payload) != 3:
        raise GrowthError("container_identity")
    try:
        identity, actual_image, owner = map(decode, payload)
    except ValueError:
        raise GrowthError("container_identity") from None
    if (
        not isinstance(identity, str)
        or not re.fullmatch(r"[a-f0-9]{64}", identity)
        or actual_image != image_id
        or owner != name
    ):
        raise GrowthError("container_identity")
    return identity


def host() -> int:
    require_linux()
    import psutil

    if (
        len(os.sched_getaffinity(0)) < 4
        or psutil.virtual_memory().total < 14_000_000_000
        or (shutil.disk_usage(ROOT).free < 5_000_000_000)
    ):
        raise GrowthError("host_admission")
    check_source_clean(artifacts_created=False)
    source = (
        bounded_command(["git", "rev-parse", "HEAD", "HEAD^{tree}"], 10, limit=100)
        .decode()
        .splitlines()
    )
    if len(source) != 2 or any(not re.fullmatch(r"[a-f0-9]{40}", item) for item in source):
        raise GrowthError("source_identity")
    expected = {"source_commit": source[0], "source_tree": source[1], **source_identity(ROOT)}
    output = ROOT / "artifacts/runtime-env-growth"
    output.mkdir(parents=True, exist_ok=False)
    name = "django-ray-growth-" + uuid.uuid4().hex
    image = name + ":experiment"
    container_id = None
    image_id = None
    create_started = False
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "source_commit": source[0],
        "source_tree": source[1],
        "producer": expected,
        "image_id": None,
        "container_id": None,
        "experiment": None,
        "container_removed": False,
        "failure": None,
        "command_cleanup_complete": True,
        "cleanup_failures": [],
    }
    try:
        bounded_command(
            [
                "docker",
                "build",
                "--quiet",
                "-f",
                "benchmarks/runtime-env-growth/Dockerfile",
                "-t",
                image,
                "--build-arg",
                "SOURCE_COMMIT=" + source[0],
                "--build-arg",
                "SOURCE_TREE=" + source[1],
                "--build-arg",
                "COPIED_SOURCE_SHA256=" + expected["copied_source_sha256"],
                ".",
            ],
            300,
            discard=True,
        )
        image_id = inspect_image(image, expected)
        report["image_id"] = image_id
        create_started = True
        created = (
            bounded_command(
                [
                    "docker",
                    "create",
                    "--name",
                    name,
                    "--network=none",
                    "--cpus=2",
                    "--memory=6g",
                    "--memory-swap=6g",
                    "--pids-limit=256",
                    "--ulimit=core=0",
                    "--shm-size=256m",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--cgroupns=private",
                    "--user=10001:10001",
                    "--tmpfs",
                    "/scratch:rw,nosuid,nodev,size=1g,nr_inodes=50000,uid=10001,gid=10001,mode=0700",
                    "--label",
                    "django-ray.runtime-env-growth=" + name,
                    image_id,
                ],
                30,
                limit=128,
            )
            .decode()
            .strip()
        )
        container_id = inspect_owned_container(name, image_id)
        report["container_id"] = container_id
        if created != container_id:
            raise GrowthError("container_identity")
        payload = bounded_command(["docker", "start", "--attach", container_id], TOTAL_SECONDS)
        try:
            report["experiment"] = validate_report(decode(payload))
        except (ValueError, TypeError):
            raise GrowthError("report_invalid") from None
        producer = report["experiment"].get("producer")
        if (
            not isinstance(producer, dict)
            or set(producer) != {*expected, "django_ray_version"}
            or any(producer.get(key) != value for key, value in expected.items())
            or not isinstance(producer["django_ray_version"], str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", producer["django_ray_version"])
        ):
            raise GrowthError("source_identity")
        check_source_clean(artifacts_created=True)
        observed = source_identity(ROOT)
        if observed != {key: expected[key] for key in observed}:
            raise GrowthError("source_identity")
        if report["experiment"]["status"] == "complete":
            report["status"] = "complete"
    except GrowthError as exc:
        report["failure"] = str(exc)
        if isinstance(exc, CommandError):
            report["command_cleanup_complete"] = exc.cleanup_complete
    except OSError:
        report["failure"] = "host_error"
    finally:
        if container_id is None and create_started and image_id is not None:
            try:
                container_id = inspect_owned_container(name, image_id)
                report["container_id"] = container_id
            except (OSError, ValueError, GrowthError):
                report["cleanup_failures"].append("container_identity")
        if container_id is not None and re.fullmatch(r"[0-9a-f]{64}", container_id):
            try:
                bounded_command(["docker", "rm", "--force", container_id], 20, discard=True)
                report["container_removed"] = True
            except (OSError, GrowthError):
                report["cleanup_failures"].append("container_cleanup")
        # Only the unique experiment image is eligible for removal.
        try:
            bounded_command(["docker", "image", "rm", image], 20, discard=True)
        except (OSError, GrowthError):
            report["image_cleanup"] = False
            report["cleanup_failures"].append("image_cleanup")
        else:
            report["image_cleanup"] = True
        if report["cleanup_failures"] or not report["container_removed"]:
            report["status"] = "failed"
        (output / "report.json").write_bytes(encode(report) + b"\n")
    return 0 if report["status"] == "complete" and report["container_removed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    require_linux()
    if options.container:
        from scripts.runtime_env_growth.supervisor import run_container

        try:
            report = run_container()
            sys.stdout.buffer.write(encode(report) + b"\n")
            # Preserve failed reports through docker attach; outer owner decides status.
            return 0
        except BaseException:
            sys.stdout.buffer.write(b'{"schema_version":1,"status":"failed"}\n')
            return 1
    try:
        return host()
    except (OSError, GrowthError):
        print("RuntimeEnv growth experiment refused before producing evidence.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
