#!/usr/bin/env python
"""A bounded Docker setup-only control; never an application acceptance gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qualification.startup import probe  # noqa: E402

LABEL = "org.django-ray.startup-profile"
ORDER = ("baseline", "precompiled", "precompiled", "baseline")
TOTAL_SECONDS = 450
CLEANUP_RESERVE = 45
MAX_OUTPUT = 512 * 1024
ENVIRONMENT = {
    "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "RAY_USAGE_STATS_ENABLED": "0",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "DJANGO_SETTINGS_MODULE": "testproject.settings_qualification",
    "DJANGO_DEPLOYMENT_MODE": "production",
    "DJANGO_DEBUG": "false",
    "DJANGO_ALLOWED_HOSTS": "django-web,localhost,127.0.0.1",
    "DATABASE_ENGINE": "django.db.backends.postgresql",
    "DATABASE_NAME": "django_ray",
    "DATABASE_USER": "django_ray",
    "DATABASE_HOST": "postgres",
    "DATABASE_PORT": "5432",
    "DATABASE_PASSWORD": "diagnostic-password-only",
    "RAY_ADDRESS": "ray://ray-head:10001",
    "RAY_STATE_API_ADDRESS": "http://ray-head:8265",
    "RAY_DASHBOARD_URL": "http://127.0.0.1:8765/ray",
    "DJANGO_RAY_RECOVERY_WORKING_DIR": "/runtime/recovery.zip",
    "DJANGO_STATIC_ROOT": "/static",
    "DJANGO_SECRET_KEY": "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_=+",
    "DJANGO_API_TOKEN": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ",
    "DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    "DJANGO_RAY_QUEUES": "default",
    "DJANGO_RAY_CONCURRENCY": "1",
    "DJANGO_RAY_POD_NAME": "startup-profile",
}


class RefusedError(RuntimeError):
    """Only fixed reason codes may leave the controller."""


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RefusedError("invalid_json")
        result[key] = value
    return result


def parse_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, TypeError):
        raise RefusedError("invalid_json") from None


def peek_exit(process):
    """Observe exit without releasing the owned group leader's numeric identity."""
    if process.returncode is not None:
        raise ChildProcessError("command_ownership_lost")
    state = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if state is None:
        return None
    return state.si_status if state.si_code == os.CLD_EXITED else -state.si_status


def command(args, seconds, maximum=MAX_OUTPUT):
    """Drain a bounded pipe; never retain or print ambient stderr or build logs."""
    if seconds <= 0:
        raise RefusedError("budget_exhausted")
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )
    data = bytearray()
    deadline = time.monotonic() + seconds
    selector = None
    eof = False
    try:
        selector = selectors.DefaultSelector()
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RefusedError("command_timeout")
            for _key, _event in selector.select(min(0.1, remaining)):
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    eof = True
                    selector.unregister(process.stdout)
                elif len(data) + len(chunk) > maximum:
                    raise RefusedError("command_output_limit")
                else:
                    data.extend(chunk)
            code = peek_exit(process)
            if eof and code is not None:
                return code, bytes(data)
    finally:
        # Own the CLI session too: buildx descendants must not retain the pipe.
        try:
            try:
                peek_exit(process)
            except ChildProcessError:
                raise RefusedError("command_ownership_lost") from None
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise RefusedError("command_cleanup_failed") from None
        finally:
            if selector is not None:
                selector.close()
            process.stdout.close()


def valid_number(value, maximum=120_000):
    return type(value) is int and 0 <= value <= maximum


def validate_record(raw, kind):
    if not 0 < len(raw) <= probe.MAX_REPORT:
        raise RefusedError("invalid_record")
    value = parse_json(raw)
    common = {"schema_version", "kind"}
    if (
        type(value) is not dict
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("kind") != kind
    ):
        raise RefusedError("invalid_record")
    if kind == "census":
        keys = {
            "source_sha256",
            "source_files",
            "python_files",
            "checked_hash_caches",
            "cache_files",
            "python_version",
            "cache_tag",
            "optimize",
        }
        if (
            set(value) != common | keys
            or type(value["source_sha256"]) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", value["source_sha256"])
        ):
            raise RefusedError("invalid_record")
        if not all(
            valid_number(value[key], probe.MAX_FILES)
            for key in ("source_files", "python_files", "checked_hash_caches", "cache_files")
        ):
            raise RefusedError("invalid_record")
        if (
            not 0 < value["python_files"] <= value["source_files"]
            or value["checked_hash_caches"] > value["python_files"]
        ):
            raise RefusedError("invalid_record")
        version = value["python_version"]
        if (
            type(version) is not list
            or len(version) != 3
            or not all(valid_number(part, 100) for part in version)
            or version[:2] != [3, 12]
            or value["cache_tag"] != "cpython-312"
            or type(value["optimize"]) is not int
            or value["optimize"] != 0
        ):
            raise RefusedError("invalid_record")
    else:
        keys = {
            "outcome",
            "import_setup_wall_ms",
            "import_setup_cpu_ms",
            "imported_source_modules",
            "imported_checked_caches",
            "child_wall_ms",
            "child_cpu_ms",
        }
        if (
            set(value) != common | keys
            or type(value["outcome"]) is not str
            or value["outcome"]
            not in {
                "complete",
                "guard_refused",
                "setup_error",
                "timeout",
                "child_failed",
            }
        ):
            raise RefusedError("invalid_record")
        for key in (
            "child_wall_ms",
            "child_cpu_ms",
            "imported_source_modules",
            "imported_checked_caches",
        ):
            if not valid_number(value[key]):
                raise RefusedError("invalid_record")
        for key in ("import_setup_wall_ms", "import_setup_cpu_ms"):
            if value[key] is not None and not valid_number(value[key]):
                raise RefusedError("invalid_record")
        if value["imported_checked_caches"] > value["imported_source_modules"]:
            raise RefusedError("invalid_record")
        if value["outcome"] == "complete" and (
            value["import_setup_wall_ms"] is None
            or value["import_setup_cpu_ms"] is None
            or value["imported_source_modules"] == 0
        ):
            raise RefusedError("invalid_record")
    return value


def create_args(name, owner, image, helper, mode):
    args = [
        "docker",
        "create",
        "--name",
        name,
        "--label",
        f"{LABEL}={owner}",
        "--network",
        "none",
        "--read-only",
        "--cpus",
        "0.25",
        "--memory",
        "768m",
        "--memory-swap",
        "768m",
        "--pids-limit",
        "64",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--no-healthcheck",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m,uid=1000,gid=1000",
        "--workdir",
        "/app",
        "--entrypoint",
        "/usr/bin/env",
        "--mount",
        f"type=bind,src={helper},dst=/diagnostic/probe.py,readonly",
        image,
        "-i",
    ]
    args += [f"{key}={value}" for key, value in ENVIRONMENT.items()]
    return [*args, "/app/.venv/bin/python", "/diagnostic/probe.py", mode]


class Controller:
    def __init__(self, image, source, output, owner):
        self.image, self.source, self.output, self.owner = image, source, output, owner
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.names = []
        self.tags = []
        self.admitted = False

    def run(self, args, seconds=15, *, cleanup=False, maximum=MAX_OUTPUT):
        # command() may need at most two additional seconds to reap its CLI.
        remaining = self.deadline - time.monotonic() - (0 if cleanup else CLEANUP_RESERVE) - 2
        return command(args, min(seconds, remaining), maximum)

    def checked(self, args, seconds=15, **kwargs):
        code, raw = self.run(args, seconds, **kwargs)
        if code:
            raise RefusedError("docker_command_failed")
        return raw

    def inspect_image(self, image, *, cleanup=False):
        value = parse_json(self.checked(["docker", "image", "inspect", image], cleanup=cleanup))
        if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
            raise RefusedError("image_identity")
        data = value[0]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(data.get("Id"))):
            raise RefusedError("image_identity")
        return data

    def inspect_container(self, name):
        value = parse_json(self.checked(["docker", "inspect", name], cleanup=True))
        if type(value) is not list or len(value) != 1:
            raise RefusedError("container_identity")
        data = value[0]
        if data.get("Config", {}).get("Labels", {}).get(LABEL) != self.owner:
            raise RefusedError("container_identity")
        return data

    def sample(self, image, kind, index):
        name = f"django-ray-startup-{self.owner}-{index}"
        code, _ = self.run(["docker", "inspect", name])
        if code == 0:
            raise RefusedError("existing_container")
        self.names.append(name)
        helper = (self.source / "qualification/startup/probe.py").resolve()
        self.checked(create_args(name, self.owner, image, helper, kind))
        before = self.inspect_container(name)
        host = before["HostConfig"]
        if (
            before["Image"] != image
            or host["NanoCpus"] != 250_000_000
            or host["Memory"] != 768 * 1024 * 1024
            or host["MemorySwap"] != 768 * 1024 * 1024
            or host["PidsLimit"] != 64
            or not host["ReadonlyRootfs"]
            or host["NetworkMode"] != "none"
            or before["Config"]["User"] != "1000:1000"
            or before["Config"]["Healthcheck"]["Test"] != ["NONE"]
        ):
            raise RefusedError("container_admission")
        started = time.monotonic()
        outer_timeout = False
        try:
            code, raw = self.run(
                ["docker", "start", "--attach", name],
                60 if kind == "census" else 20,
                maximum=probe.MAX_REPORT,
            )
        except RefusedError as error:
            if str(error) != "command_timeout" or kind == "census":
                raise
            self.checked(["docker", "kill", name], 5, cleanup=True)
            code, raw = None, b""
            outer_timeout = True
        elapsed = round((time.monotonic() - started) * 1000)
        state = self.inspect_container(name)["State"]
        if state["Running"] or state["Pid"] != 0:
            raise RefusedError("container_still_running")
        if kind == "census":
            if code != 0 or state["ExitCode"] != 0 or state["OOMKilled"]:
                raise RefusedError("census_failed")
            result = validate_record(raw, kind)
        else:
            result: dict = (
                {"outcome": "outer_timeout"} if outer_timeout else validate_record(raw, kind)
            )
            if (code != 0 or state["ExitCode"] != 0 or state["OOMKilled"]) and result[
                "outcome"
            ] == "complete":
                raise RefusedError("contradictory_sample")
            result.update(
                {
                    "docker_attach_exit_code": code,
                    "container_exit_code": state["ExitCode"],
                    "oom_killed": state["OOMKilled"],
                    "attach_wall_ms": elapsed,
                }
            )
        self.checked(["docker", "rm", name], 5, cleanup=True)
        return result

    def prepare(self):
        existing = self.checked(["docker", "ps", "-aq", "--filter", f"label={LABEL}={self.owner}"])
        if existing.strip():
            raise RefusedError("existing_owner")
        baseline = self.inspect_image(self.image)
        if baseline["Id"] != self.image:
            raise RefusedError("image_identity")
        base_tag = f"django-ray-startup-base:{self.owner}"
        derived_tag = f"django-ray-startup-compiled:{self.owner}"
        for tag in (base_tag, derived_tag):
            code, _ = self.run(["docker", "image", "inspect", tag])
            if code == 0:
                raise RefusedError("existing_owned_tag")
        self.admitted = True
        self.checked(["docker", "tag", self.image, base_tag])
        self.tags.append(base_tag)
        with tempfile.TemporaryDirectory(prefix="django-ray-startup-") as directory:
            dockerfile = Path(directory) / "Dockerfile"
            dockerfile.write_text(
                f'FROM {base_tag}\nLABEL {LABEL}="{self.owner}"\n'
                "RUN /app/.venv/bin/python -m compileall -q -f --invalidation-mode checked-hash /app/src /app/testproject\n",
                encoding="utf-8",
            )
            self.tags.append(derived_tag)
            self.checked(
                [
                    "docker",
                    "build",
                    "--network=none",
                    "--pull=false",
                    "--quiet",
                    "--tag",
                    derived_tag,
                    directory,
                ],
                180,
            )
        derived = self.inspect_image(derived_tag)
        layers = baseline["RootFS"]["Layers"]
        if derived["RootFS"]["Layers"][:-1] != layers:
            raise RefusedError("image_ancestry")
        for key in ("Env", "User", "WorkingDir", "Entrypoint", "Cmd", "Healthcheck"):
            if baseline["Config"].get(key) != derived["Config"].get(key):
                raise RefusedError("image_config_changed")
        return derived["Id"]

    def cleanup(self):
        if not self.admitted:
            return True
        okay = True
        try:
            raw = self.checked(
                ["docker", "ps", "-aq", "--filter", f"label={LABEL}={self.owner}"], cleanup=True
            )
            ids = raw.decode("ascii").splitlines()
            if len(ids) > 6 or any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in ids):
                raise RefusedError("cleanup_inventory")
            for item in ids:
                data = self.inspect_container(item)
                if data.get("Name", "").removeprefix("/") not in self.names:
                    raise RefusedError("cleanup_foreign_container")
                self.checked(["docker", "rm", "--force", item], 5, cleanup=True)
            for tag in reversed(self.tags):
                code, _ = self.run(["docker", "image", "inspect", tag], cleanup=True)
                if code == 0:
                    data = self.inspect_image(tag, cleanup=True)
                    if tag.startswith("django-ray-startup-base:"):
                        if data["Id"] != self.image:
                            raise RefusedError("cleanup_foreign_image")
                    elif data.get("Config", {}).get("Labels", {}).get(LABEL) != self.owner:
                        raise RefusedError("cleanup_foreign_image")
                    self.checked(["docker", "image", "rm", tag], 5, cleanup=True)
                    code, _ = self.run(["docker", "image", "inspect", tag], cleanup=True)
                    if code == 0:
                        raise RefusedError("cleanup_image_retained")
            raw = self.checked(
                ["docker", "ps", "-aq", "--filter", f"label={LABEL}={self.owner}"], cleanup=True
            )
            if raw.strip():
                okay = False
        except (RefusedError, OSError, ValueError, KeyError, TypeError):
            okay = False
        return okay

    def investigate(self):
        report = {
            "schema_version": 1,
            "acceptance": False,
            "status": "incomplete",
            "reason": "unavailable",
            "samples": [],
            "cleanup_confirmed": False,
            "baseline_image": self.image,
            "host_page_cache": "uncontrolled",
            "source_control": "runtime_base_without_application_browser_layer",
        }
        try:
            if sys.platform != "linux":
                raise RefusedError("linux_required")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image) or not re.fullmatch(
                r"[a-z0-9][a-z0-9.-]{0,63}", self.owner
            ):
                raise RefusedError("invalid_identity")
            helper = self.source / "qualification/startup/probe.py"
            own_helper = Path(probe.__file__)
            if helper.is_symlink() or helper.read_bytes() != own_helper.read_bytes():
                raise RefusedError("source_helper_mismatch")
            expected, _ = probe.source_manifest((self.source / "src", self.source / "testproject"))
            report["source_sha256"] = expected
            report["input_sha256"] = {
                path: hashlib.sha256((self.source / path).read_bytes()).hexdigest()
                for path in (
                    "Dockerfile",
                    "uv.lock",
                    "pyproject.toml",
                    "qualification/startup/probe.py",
                )
            }
            derived = self.prepare()
            report["precompiled_image"] = derived
            census = {
                "baseline": self.sample(self.image, "census", "census-a"),
                "precompiled": self.sample(derived, "census", "census-b"),
            }
            report["census"] = census
            for item in census.values():
                if item["source_sha256"] != expected:
                    raise RefusedError("source_mismatch")
            for key in ("source_files", "python_files", "python_version", "cache_tag", "optimize"):
                if census["baseline"][key] != census["precompiled"][key]:
                    raise RefusedError("census_mismatch")
            if census["baseline"]["cache_files"] != 0:
                raise RefusedError("baseline_already_cached")
            if (
                census["precompiled"]["checked_hash_caches"]
                != census["precompiled"]["python_files"]
            ):
                raise RefusedError("incomplete_precompile")
            for index, variant in enumerate(ORDER):
                item = self.sample(
                    self.image if variant == "baseline" else derived, "sample", str(index)
                )
                report["samples"].append({"variant": variant, **item})
                if item["outcome"] == "guard_refused":
                    raise RefusedError("guard_refused")
            complete = all(item["outcome"] == "complete" for item in report["samples"])
            report.update(
                status="complete" if complete else "incomplete",
                reason="observations_complete" if complete else "startup_not_complete",
            )
        except RefusedError as error:
            report["reason"] = str(error)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            report["reason"] = "controller_error"
        finally:
            report["cleanup_confirmed"] = self.cleanup()
            if not report["cleanup_confirmed"]:
                report.update(status="incomplete", reason="cleanup_unconfirmed")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        return 2
    args.output.mkdir(parents=True)
    report = Controller(
        args.image, args.source_dir.resolve(), args.output, args.owner
    ).investigate()
    (args.output / "startup-profile.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
