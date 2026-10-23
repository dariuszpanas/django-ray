"""Persistent arm driver. Ray is imported only inside an admitted Linux container."""

from __future__ import annotations

import contextlib
import copy
import importlib.metadata
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

from scripts.require_linux import require_linux
from scripts.runtime_env_growth.evidence import (
    CANARIES,
    DEFAULT_EXCLUDES,
    EXCLUDES,
    MIB,
    PHASES,
    GrowthError,
    admission,
    archive_record,
    digest,
    encode,
    fixture_identity,
    make_fixture,
    scan_tree,
    validate_value,
)


def frozen_settings(defaults: list[str]) -> str:
    if (
        any(
            key in os.environ
            for key in (
                "RAY_ADDRESS",
                "RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES",
                "RAY_RUNTIME_ENV_IGNORE_GITIGNORE",
            )
        )
        or defaults != DEFAULT_EXCLUDES
    ):
        raise GrowthError("configuration")
    return digest(
        encode(
            {
                "defaults": defaults,
                "excludes": EXCLUDES,
                "gitignore": "build/\n",
                "rayignore": "dist/\n",
            }
        )
    )


@contextlib.contextmanager
def observe_archives() -> Generator[dict[str, Any]]:
    """Delegate actual Ray operations, observing ZIPs before Ray unlinks them."""
    from ray._private.runtime_env import packaging, working_dir

    original_create = packaging.create_package
    original_upload = working_dir.upload_package_if_needed
    result: dict[str, Any] = {"archives": [], "operation": "failed"}

    def create(*args: Any, **kwargs: Any) -> Any:
        returned = original_create(*args, **kwargs)
        target = Path(args[1] if len(args) > 1 else kwargs["target_path"])
        scope = (
            "snapshot" if any("-snapshot-" in part for part in target.parts) else "upload_scratch"
        )
        if len(result["archives"]) >= 2:
            raise GrowthError("archive_limit")
        result["archives"].append({"scope": scope, **archive_record(target)})
        return returned

    def upload(*args: Any, **kwargs: Any) -> Any:
        uploaded = original_upload(*args, **kwargs)
        result["operation"] = "uploaded" if uploaded else "cache_hit"
        return uploaded

    packaging.create_package = create
    working_dir.upload_package_if_needed = upload
    try:
        yield result
    finally:
        packaging.create_package = original_create
        working_dir.upload_package_if_needed = original_upload


def prepare(arm: str, source: Path, emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Use a fresh local spec each time; only explicit controls reuse a prepared URI."""
    template = {"working_dir": str(source), "excludes": list(EXCLUDES)}
    original = fixture_identity(source)
    with observe_archives() as observation:
        try:
            if arm == "raw":
                from ray._private.runtime_env.working_dir import upload_working_dir_if_needed

                with tempfile.TemporaryDirectory(prefix="django-ray-runtime-env-") as scratch:
                    prepared = upload_working_dir_if_needed(
                        copy.deepcopy(template), include_gitignore=True, scratch_dir=scratch
                    )
            else:
                from django_ray.runtime.runtime_env import (
                    normalize_runtime_env,
                    prepare_runtime_env_for_ray_core,
                    snapshot_local_runtime_env,
                )
                from django_ray.workflow.plans import runtime_env_plan_identity

                resolved = normalize_runtime_env(copy.deepcopy(template))
                trust = {"environment_revision": "runtime-env-growth-v1"}
                before = runtime_env_plan_identity(resolved, trust_identity=trust)
                with snapshot_local_runtime_env(resolved) as snapshot:
                    inside = runtime_env_plan_identity(snapshot, trust_identity=trust)
                    if before.manifest["digest"] != inside.manifest["digest"]:
                        raise GrowthError("identity")
                    snap_path = Path(snapshot.spec["working_dir"])
                    measured = scan_tree(snap_path.parent)
                    emit({"scope": "snapshot", "value": measured})
                    if measured["status"] != "complete":
                        raise GrowthError(str(measured["status"]))
                    prepared = prepare_runtime_env_for_ray_core(snapshot)
                after = runtime_env_plan_identity(resolved, trust_identity=trust)
                if after.manifest["digest"] != before.manifest["digest"]:
                    raise GrowthError("identity")
                emit(
                    {
                        "snapshot_removed": not snap_path.exists(),
                        "identity_sha256": before.manifest["digest"],
                    }
                )
                if snap_path.exists():
                    raise GrowthError("unsafe_root")
            if template["working_dir"] != str(source) or fixture_identity(source) != original:
                raise GrowthError("identity")
            if not re.fullmatch(r"gcs://_ray_pkg_[0-9a-f]+\.zip", prepared.get("working_dir", "")):
                raise GrowthError("identity")
            emit({**observation, "uri_sha256": digest(prepared["working_dir"].encode())})
            return prepared
        except BaseException:
            emit({**observation, "operation": "failed"})
            raise


def application_probe() -> dict[str, Any]:
    """Read the extracted worker CWD, never the driver's original source path."""
    from pathlib import Path

    root = Path.cwd()
    return {
        "variant": (root / "app_value.txt").read_text(encoding="ascii"),
        "included": (root / "included.txt").read_text(encoding="ascii") == "included",
        "entries": sum((root / name).exists() for name in CANARIES),
    }


def run_arm(
    arm: str, root: Path, emit: Callable[[dict[str, Any]], None], read_command: Callable[[], str]
) -> None:
    import ray
    import ray._private.worker
    from ray._private import ray_constants

    if ray.is_initialized() or importlib.metadata.version("ray") != "2.58.0":
        raise GrowthError("configuration")
    expected_settings = frozen_settings(ray_constants.get_runtime_env_default_excludes())
    sources = make_fixture(root / "source")
    emit(
        {
            "kind": "data",
            "value": {
                "ray_version": importlib.metadata.version("ray"),
                "django_ray_version": importlib.metadata.version("django-ray"),
                "python_version": ".".join(map(str, sys.version_info[:3])),
                "settings_sha256": expected_settings,
                "default_excludes": DEFAULT_EXCLUDES,
                "excludes": EXCLUDES,
                "gitignore_sha256": digest(b"build/\n"),
                "rayignore_sha256": digest(b"dist/\n"),
            },
        }
    )
    old_uri = None
    for session in range(1, 4):
        if read_command() != "start\n":
            raise GrowthError("runtime")
        phase_index = 0

        def phase(name: str, session: int = session) -> None:
            nonlocal phase_index
            if PHASES[phase_index] != name:
                raise GrowthError("runtime")
            emit({"kind": "phase", "session": session, "index": phase_index, "phase": name})
            phase_index += 1

        def data(value: dict[str, Any], session: int = session) -> None:
            emit({"kind": "data", "session": session, "value": value})

        phase("startup")
        ray.init(
            address="local",
            num_cpus=2,
            num_gpus=0,
            object_store_memory=128 * MIB,
            include_dashboard=False,
            log_to_driver=False,
            _temp_dir=str(root / "ray"),
        )
        session_path = Path(ray._private.worker._global_node.get_session_dir_path())
        if not session_path.resolve().is_relative_to((root / "ray").resolve()):
            raise GrowthError("unsafe_root")
        probe = ray.remote(application_probe)
        phase("scan")
        measured = scan_tree(root)
        data({"scope": "source", "sha256": fixture_identity(sources[0]), "value": measured})
        if measured["status"] != "complete":
            raise GrowthError(str(measured["status"]))
        first = None
        for control in range(5):
            phase("prepare")
            started = time.monotonic()
            if (
                frozen_settings(ray_constants.get_runtime_env_default_excludes())
                != expected_settings
            ):
                raise GrowthError("configuration")
            variant = "B" if control == 3 else "A"
            if control in (1, 4):
                assert first is not None
                prepared = copy.deepcopy(first)
                data(
                    {
                        "operation": "uri_reuse",
                        "archives": [],
                        "uri_sha256": digest(prepared["working_dir"].encode()),
                    }
                )
            else:
                prepared = prepare(arm, sources[1 if control == 3 else 0], data)
            if control == 0:
                first = copy.deepcopy(prepared)
                if old_uri is not None and prepared["working_dir"] != old_uri:
                    raise GrowthError("identity")
                old_uri = prepared["working_dir"]
            assert first is not None
            if (prepared["working_dir"] == first["working_dir"]) != (variant == "A"):
                raise GrowthError("identity")
            phase("task")
            result = ray.get(probe.options(runtime_env=prepared).remote(), timeout=15)
            if result != {"variant": variant, "included": True, "entries": 0}:
                raise GrowthError("application")
            data({"variant": variant, "elapsed_seconds": round(time.monotonic() - started, 6)})
            phase("scan")
            measured, extracted = scan_tree(root), scan_tree(session_path)
            data({"value": measured, "extracted": extracted})
            if measured["status"] != "complete" or extracted["status"] != "complete":
                raise GrowthError("source_limit")
        phase("shutdown")
        ray.shutdown(wait_for_processes=True)
        phase("scan")
        measured = scan_tree(root)
        data({"value": measured})
        if measured["status"] != "complete":
            raise GrowthError(str(measured["status"]))
        emit({"kind": "done", "session": session})
        # Parent establishes quiescence before admitting the next start command.


def main() -> int:
    require_linux()
    arm, token = sys.argv[1:]
    if arm not in {"raw", "django"} or not re.fullmatch(r"[0-9a-f]{12}", token):
        return 2
    root = Path("/scratch") / token / arm
    if not root.is_dir() or root.is_symlink():
        return 2
    descriptor = int(os.environ["DR_GROWTH_FD"])
    os.set_inheritable(descriptor, False)
    channel = os.fdopen(descriptor, "wb", buffering=0)

    def emit(event: dict[str, Any]) -> None:
        validate_value(event)
        channel.write(encode(event, 32768) + b"\n")

    try:
        admission()
        if arm == "django":
            from django.conf import settings

            settings.configure(INSTALLED_APPS=[], SECRET_KEY="synthetic-benchmark", DJANGO_RAY={})
        run_arm(arm, root, emit, sys.stdin.readline)
        return 0
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, GrowthError) else "runtime"
        emit({"kind": "failure", "reason": reason})
        return 1
    finally:
        channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
