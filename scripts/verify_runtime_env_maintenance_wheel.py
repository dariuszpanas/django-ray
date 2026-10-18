"""Exercise installed RuntimeEnv maintenance in one owned Linux SQLite fixture."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

MARKER = "installed-maintenance-private-value"
TASK_PREFIX = "installed-maintenance-private-task"
KEYS = {
    "old": base64.urlsafe_b64encode(b"o" * 32).rstrip(b"=").decode(),
    "new": base64.urlsafe_b64encode(b"n" * 32).rstrip(b"=").decode(),
}
ATTENTION = b"CommandError: RuntimeEnv maintenance requires attention; inspect the report.\n"


def _configure(directory: Path, *, mode: str, active: str) -> None:
    import django
    from django.conf import settings

    settings.configure(
        SECRET_KEY="owned-installed-maintenance-fixture",
        USE_TZ=True,
        DEBUG=True,
        LOGGING_CONFIG=None,
        INSTALLED_APPS=["django_ray.doctor.probe.DoctorAppConfig"],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(directory / "owned.sqlite3"),
            }
        },
        DJANGO_RAY={
            "RUNNER": "ray_core",
            "RUNTIME_ENV_STORAGE_MODE": mode,
            "RUNTIME_ENV_ENCRYPTION_KEYS": KEYS,
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": active,
        },
    )
    django.setup()


def _rows() -> list[dict]:
    from django.core.serializers.json import DjangoJSONEncoder

    from django_ray.models import RayTaskExecution

    return json.loads(
        json.dumps(list(RayTaskExecution.objects.order_by("pk").values()), cls=DjangoJSONEncoder)
    )


def _child(directory: Path, phase: str, arguments: list[str]) -> None:
    _configure(directory, mode="encrypted" if phase == "setup" else "required", active="new")
    from django.core.management import call_command, execute_from_command_line

    from django_ray.conf.settings import get_settings
    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.runtime.runtime_env import (
        RuntimeEnvSnapshotError,
        normalize_runtime_env,
        runtime_env_for_execution,
        runtime_env_for_storage,
    )

    baseline = directory / "baseline.json"
    if phase == "setup":
        call_command("migrate", verbosity=0, interactive=False)
        resolved = normalize_runtime_env({"env_vars": {"OWNED_VALUE": MARKER}})
        for index in range(3):
            config = {
                **get_settings(),
                "RUNTIME_ENV_STORAGE_MODE": "plaintext" if index < 2 else "encrypted",
                "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "old",
            }
            task_id = f"{TASK_PREFIX}-{index}"
            stored = runtime_env_for_storage(resolved, task_id=task_id, config=config)
            RayTaskExecution.objects.create(
                task_id=task_id,
                callable_path="must_not_be_imported_by_maintenance",
                state=TaskState.SUCCEEDED,
                runtime_env_profile=stored.profile,
                runtime_env_json=stored.serialized,
                runtime_env_hash=stored.digest,
            )
        baseline.write_text(json.dumps(_rows()), encoding="utf-8")
    elif phase == "command":
        # A Django debug cursor must not expose plaintext CAS parameters.
        logger = logging.getLogger("django.db.backends")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(logging.StreamHandler(sys.stderr))
        execute_from_command_line(["owned-fixture", "django_ray_runtime_env", *arguments])
    elif phase == "verify-rewrite":
        before = json.loads(baseline.read_text(encoding="utf-8"))
        after = _rows()
        if len(before) != 3 or len(after) != 3:
            raise RuntimeError("Installed maintenance changed its fixture inventory")
        current_only = {**get_settings(), "RUNTIME_ENV_ENCRYPTION_KEYS": {"new": KEYS["new"]}}
        for old, new in zip(before, after, strict=True):
            if {k: v for k, v in old.items() if k != "runtime_env_json"} != {
                k: v for k, v in new.items() if k != "runtime_env_json"
            }:
                raise RuntimeError("Installed maintenance changed captured identity or lifecycle")
            raw = new["runtime_env_json"]
            if (
                raw == old["runtime_env_json"]
                or MARKER in raw
                or json.loads(raw)["key_id"] != "new"
            ):
                raise RuntimeError("Installed maintenance did not replace the expected envelope")
            resolved = runtime_env_for_execution(
                RayTaskExecution.objects.get(pk=new["id"]), config=current_only
            )
            if resolved.spec != {"env_vars": {"OWNED_VALUE": MARKER}}:
                raise RuntimeError("Installed maintenance changed the captured RuntimeEnv")
        baseline.write_text(json.dumps(after), encoding="utf-8")
    elif phase == "add-plaintext":
        resolved = normalize_runtime_env({"env_vars": {"OWNED_VALUE": MARKER}})
        task_id = f"{TASK_PREFIX}-plaintext"
        stored = runtime_env_for_storage(
            resolved,
            task_id=task_id,
            config={**get_settings(), "RUNTIME_ENV_STORAGE_MODE": "plaintext"},
        )
        row = RayTaskExecution.objects.create(
            task_id=task_id,
            callable_path="must_not_be_imported_by_maintenance",
            state=TaskState.SUCCEEDED,
            runtime_env_profile=stored.profile,
            runtime_env_json=stored.serialized,
            runtime_env_hash=stored.digest,
        )
        try:
            runtime_env_for_execution(row)
        except RuntimeEnvSnapshotError as error:
            if (
                str(error)
                != "django-ray: Persisted RuntimeEnv plaintext is forbidden by required encryption"
            ):
                raise RuntimeError(
                    "Installed required mode returned an unexpected refusal"
                ) from None
        else:
            raise RuntimeError("Installed required mode accepted plaintext")
        row.delete()
    elif phase == "tamper":
        row = RayTaskExecution.objects.order_by("pk").first()
        envelope = json.loads(row.runtime_env_json)
        value = envelope["ciphertext"]
        envelope["ciphertext"] = ("A" if value[0] != "A" else "B") + value[1:]
        row.runtime_env_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        row.save(update_fields=["runtime_env_json"])
        baseline.write_text(json.dumps(_rows()), encoding="utf-8")
    elif phase == "verify-preserved":
        if _rows() != json.loads(baseline.read_text(encoding="utf-8")):
            raise RuntimeError("Installed maintenance altered preserved rows")
    else:
        raise RuntimeError("Unknown installed maintenance fixture phase")


def _capture(process: subprocess.Popen) -> tuple[bytes, bytes]:
    """Drain both pipes within byte and wall-clock bounds before retaining output."""
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + 25
    with selectors.DefaultSelector() as selector:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Installed maintenance exceeded its process deadline")
            for key, _ in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                retained = streams[key.fileobj]
                if len(retained) + len(chunk) > 32768:
                    raise RuntimeError("Installed maintenance exceeded its output budget")
                retained.extend(chunk)
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RuntimeError("Installed maintenance exceeded its process deadline") from None
    return bytes(streams[process.stdout]), bytes(streams[process.stderr])


def _run(directory: Path, phase: str, *arguments: str) -> tuple[int, bytes]:
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), str(directory), phase, *arguments],
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = _capture(process)
        if any(
            value.encode() in stdout + stderr for value in (MARKER, TASK_PREFIX, *KEYS.values())
        ):
            raise RuntimeError("Installed maintenance exposed protected fixture data")
        expected_error = ATTENTION if phase == "command" and process.returncode in {1, 2} else b""
        if stderr != expected_error:
            raise RuntimeError("Installed maintenance emitted unexpected diagnostics")
        if phase != "command" and (process.returncode or stdout):
            raise RuntimeError("Installed maintenance fixture phase failed")
        return process.returncode, stdout
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def _command(directory: Path, mode: str, *arguments: str) -> dict:
    status, output = _run(directory, "command", "--format", "json", "--mode", mode, *arguments)
    report = json.loads(output)
    if not (
        report["schema"] == "django-ray.runtime-env-maintenance"
        and report["schema_version"] == 1
        and report["mode"] == mode
        and report["exit_code"] == status
        and report["current_table_only"] is True
        and report["cleanup_confirmed"] is True
        and report["limits"]["time_contract"] == "cooperative"
        and report["backup_obligations"] == "not_verified"
        and report["key_retirement_authorized"] is False
    ):
        raise RuntimeError("Installed maintenance did not retain its evidence boundary")
    return report


def verify() -> None:
    if not sys.platform.startswith("linux"):
        return
    with TemporaryDirectory(prefix="django-ray-maintenance-wheel-") as temporary:
        directory = Path(temporary)
        database = directory / "owned.sqlite3"
        _run(directory, "setup")
        before = hashlib.sha256(database.read_bytes()).digest()
        dry = _command(directory, "dry-run")
        if not (
            dry["exit_code"] == 2
            and dry["counts"]["plaintext"] == 2
            and dry["counts"]["old_key"] == 1
        ):
            raise RuntimeError("Installed maintenance did not identify required rewrites")
        if hashlib.sha256(database.read_bytes()).digest() != before:
            raise RuntimeError("Installed maintenance dry-run changed its database")
        cursor = None
        totals = {"rewritten": 0, "rewrapped": 0}
        for index in range(3):
            arguments = ["--confirm", "--max-rows", "1"]
            if cursor is not None:
                arguments.append(f"--cursor={cursor}")
            report = _command(directory, "rewrite", *arguments)
            if report["exit_code"] != (2 if index < 2 else 0) or report["processed"] != 1:
                raise RuntimeError("Installed maintenance page continuation failed")
            for name in totals:
                totals[name] += report["counts"][name]
            cursor = report["next_cursor"]
            if (cursor is None) != (index == 2):
                raise RuntimeError("Installed maintenance cursor boundary failed")
        if totals != {"rewritten": 2, "rewrapped": 1}:
            raise RuntimeError("Installed maintenance did not rewrite and rewrap its fixture")
        _run(directory, "verify-rewrite")
        before = hashlib.sha256(database.read_bytes()).digest()
        checked = _command(directory, "check")
        repeated = _command(directory, "rewrite", "--confirm")
        if checked["exit_code"] or repeated["exit_code"] or repeated["counts"]["current_key"] != 3:
            raise RuntimeError("Installed maintenance repeat was not idempotent")
        if hashlib.sha256(database.read_bytes()).digest() != before:
            raise RuntimeError("Installed maintenance check or idempotent repeat changed rows")
        _run(directory, "add-plaintext")
        _run(directory, "tamper")
        rejected = _command(directory, "rewrite", "--confirm")
        if rejected["exit_code"] != 1 or rejected["counts"]["invalid_snapshot"] != 1:
            raise RuntimeError("Installed maintenance did not preserve a tampered snapshot")
        _run(directory, "verify-preserved")
    if directory.exists():
        raise RuntimeError("Installed maintenance fixture was not removed")
    print(
        "Installed RuntimeEnv maintenance: bounded continuation, rewrite/rewrap, required refusal, "
        "identity preservation, fixed diagnostics and cleanup passed; backup keys remain unverified"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _child(Path(sys.argv[1]), sys.argv[2], sys.argv[3:])
    else:
        verify()
