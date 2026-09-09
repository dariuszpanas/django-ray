"""Inspect generic Ray nodes by value, without installing the project on them.

This is an assertion layer. The caller owns the namespace, Ray generation,
immutable application image, mounted archives and enclosing Job deadline.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
NODE_ID = re.compile(r"[0-9a-f]{56}\Z")
MAX_RECEIPT_BYTES = 16 * 1024


def archive_identity(path: Path) -> dict[str, int | str]:
    """Hash one bounded archive without retaining it in the receipt."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ValueError("RuntimeEnv archive exceeds its byte limit")
            digest.update(chunk)
    if not size:
        raise ValueError("RuntimeEnv archive is empty")
    return {"bytes": size, "sha256": digest.hexdigest()}


def make_node_probe():
    """A closure forces Ray cloudpickle to carry code, not a project import.

    All remote imports are standard library or Ray. Do not move this callable to
    module scope: the generic image intentionally has no qualification package.
    """

    def inspect_node(source_path, recovery_path, expected, remote_sha256):
        import hashlib
        import importlib.util
        import sys
        from zipfile import ZipFile

        import ray

        if importlib.util.find_spec("django_ray") is not None:
            raise ValueError("Generic Ray node already has django_ray installed")
        archives = {}
        for name, path, member in (
            ("source", source_path, "src/django_ray/runtime/remote.py"),
            ("recovery", recovery_path, "django_ray/runtime/remote.py"),
        ):
            digest = hashlib.sha256()
            size = 0
            with open(path, "rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise ValueError("RuntimeEnv archive exceeds its byte limit")
                    digest.update(chunk)
                identity = {"bytes": size, "sha256": digest.hexdigest()}
                if not size or identity != expected[name]:
                    raise ValueError("RuntimeEnv archive differs from the application image")
                # Check the same open file that was hashed. Reading a member is
                # bounded independently of the compressed archive's size.
                stream.seek(0)
                with ZipFile(stream) as archive:
                    required = {member, "testproject/apps/cluster_tasks/workflows.py"}
                    if name == "recovery":
                        required |= {
                            "cryptography/__init__.py",
                            "django/__init__.py",
                            "psycopg/__init__.py",
                            "unfold/__init__.py",
                        }
                    if not required <= set(archive.namelist()):
                        raise ValueError("RuntimeEnv archive lacks required task modules")
                    with archive.open(member) as source:
                        remote = source.read(1024 * 1024 + 1)
                    if (
                        len(remote) > 1024 * 1024
                        or hashlib.sha256(remote).hexdigest() != remote_sha256
                    ):
                        raise ValueError(
                            "RuntimeEnv remote source differs from the application image"
                        )
            archives[name] = identity
        return {
            "node_id": ray.get_runtime_context().get_node_id(),
            "python_minor": list(sys.version_info[:2]),
            "ray_version": ray.__version__,
            "django_ray_preinstalled": False,
            "remote_sha256": remote_sha256,
            "archives": archives,
        }

    return inspect_node


def live_node_ids(nodes: object, *, expected_count: int) -> set[str]:
    """Reject partial, duplicate or malformed live membership observations."""
    if type(nodes) is not list:
        raise ValueError("Ray membership is not a node list")
    live = []
    for node in nodes:
        if type(node) is not dict or type(node.get("Alive")) is not bool:
            raise ValueError("Ray membership contains a malformed node")
        if not node["Alive"]:
            continue
        identity = node.get("NodeID")
        if not isinstance(identity, str) or NODE_ID.fullmatch(identity) is None:
            raise ValueError("Ray membership contains an invalid node identity")
        live.append(identity)
    if len(live) != expected_count or len(set(live)) != expected_count:
        raise ValueError("Ray live membership differs from the required topology")
    return set(live)


def verify_generic_nodes(
    *,
    address: str,
    source_archive: Path,
    recovery_archive: Path,
    remote_source: Path,
    expected_count: int = 2,
    timeout: float = 120,
    previous_node_ids: set[str] | None = None,
) -> list[dict]:
    """Probe every live node, then require stable, optionally cold membership.

    No local Ray process is started. This function owns its client connection and
    submitted probe tasks; it never disconnects a pre-existing client. The caller
    must enforce a Job deadline as Ray Client connection/shutdown calls are not
    covered by the task-result timeout.
    """
    import sys

    parsed = urlsplit(address)
    if (
        not address.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in address)
        or "\\" in address
        or parsed.scheme != "ray"
        or not parsed.hostname
        or not parsed.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or "?" in address
        or "#" in address
    ):
        raise ValueError("The probe requires an explicit Ray Client host and port")
    if type(expected_count) is not int or not 1 <= expected_count <= 3:
        raise ValueError("The probe supports one to three generic nodes")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("The probe timeout must be positive and at most 300 seconds")
    if previous_node_ids is not None and (
        type(previous_node_ids) is not set
        or len(previous_node_ids) != expected_count
        or any(
            not isinstance(value, str) or NODE_ID.fullmatch(value) is None
            for value in previous_node_ids
        )
    ):
        raise ValueError("Previous Ray membership is malformed")
    expected = {
        "source": archive_identity(source_archive),
        "recovery": archive_identity(recovery_archive),
    }
    with remote_source.open("rb") as stream:
        source = stream.read(1024 * 1024 + 1)
    if not source or len(source) > 1024 * 1024:
        raise ValueError("Application remote source exceeds its byte limit or is empty")
    remote_sha256 = hashlib.sha256(source).hexdigest()

    import ray
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    if ray.is_initialized():
        raise ValueError("The generic-node probe requires its own Ray Client connection")
    refs = []
    try:
        ray.init(address=address, runtime_env={}, logging_level="ERROR")
        deadline = time.monotonic() + timeout
        membership = live_node_ids(ray.nodes(), expected_count=expected_count)
        if previous_node_ids is not None and membership & previous_node_ids:
            raise ValueError("Cold Ray replacement reused a previously observed node")
        probe = ray.remote(num_cpus=0, max_retries=0)(make_node_probe())
        for node_id in sorted(membership):
            refs.append(
                probe.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(node_id, soft=False),
                    runtime_env={},
                ).remote(str(source_archive), str(recovery_archive), expected, remote_sha256)
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Generic-node probe exhausted its deadline")
        results = ray.get(refs, timeout=remaining)
        if type(results) is not list or len(results) != expected_count:
            raise ValueError("Generic-node probe returned incomplete observations")
        for node_id, result in zip(sorted(membership), results, strict=True):
            if result != {
                "node_id": node_id,
                "python_minor": list(sys.version_info[:2]),
                "ray_version": ray.__version__,
                "django_ray_preinstalled": False,
                "remote_sha256": remote_sha256,
                "archives": expected,
            }:
                raise ValueError(
                    "Generic-node observation differs from the required source and runtime"
                )
        if live_node_ids(ray.nodes(), expected_count=expected_count) != membership:
            raise ValueError("Ray live membership changed during generic-node assertions")
        return results
    finally:
        for ref in refs:
            try:
                ray.cancel(ref, force=False)
            except Exception:
                pass
        ray.shutdown()


def read_previous_receipt(path: Path, *, expected_count: int) -> tuple[list[dict], str]:
    """Read an exact passing predecessor from this run's disposable receipt PVC."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("Previous generic-node receipt exceeds its byte limit")
    value = json.loads(raw)
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema_version",
            "layer",
            "status",
            "complete_application_gate",
            "cold_replacement",
            "previous_receipt_sha256",
            "observations",
        }
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["layer"] != "generic_ray_nodes"
        or value["status"] != "passed"
        or value["complete_application_gate"] is not False
        or value["cold_replacement"] is not False
        or value["previous_receipt_sha256"] is not None
        or type(value["observations"]) is not list
        or len(value["observations"]) != expected_count
        or any(type(node) is not dict for node in value["observations"])
    ):
        raise ValueError("Previous generic-node receipt is not a passing first generation")
    live_node_ids(
        [{"Alive": True, "NodeID": node.get("node_id")} for node in value["observations"]],
        expected_count=expected_count,
    )
    return value["observations"], hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    """Run one Job layer; print bounded JSON, never raw dependency exceptions.

    The receipt file is created exclusively and becomes a predecessor only when
    the command exits zero. The executor must require Job success, transport the
    declared receipts and prove their source/image and disposable-volume binding.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--source-archive", required=True, type=Path)
    parser.add_argument("--recovery-archive", required=True, type=Path)
    parser.add_argument("--remote-source", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--previous-receipt", type=Path)
    parser.add_argument("--node-count", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    receipt = {
        "schema_version": 1,
        "layer": "generic_ray_nodes",
        "status": "failed",
        "complete_application_gate": False,
        "cold_replacement": False,
        "previous_receipt_sha256": None,
        "observations": [],
    }
    passed = False
    try:
        previous = None
        if args.previous_receipt is not None:
            previous, receipt["previous_receipt_sha256"] = read_previous_receipt(
                args.previous_receipt, expected_count=args.node_count
            )
        observations = verify_generic_nodes(
            address=args.address,
            source_archive=args.source_archive,
            recovery_archive=args.recovery_archive,
            remote_source=args.remote_source,
            expected_count=args.node_count,
            timeout=args.timeout,
            previous_node_ids={node["node_id"] for node in previous} if previous else None,
        )
        if previous is not None:
            expected = {key: value for key, value in observations[0].items() if key != "node_id"}
            if any(
                {key: value for key, value in node.items() if key != "node_id"} != expected
                for node in previous
            ):
                raise ValueError("Cold replacement changed the source, archives or runtime")
        receipt.update(
            status="passed", observations=observations, cold_replacement=previous is not None
        )
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise ValueError("Generic-node receipt exceeds its byte limit")
        with args.receipt.open("xb") as stream:
            stream.write(encoded)
        passed = True
    except Exception:
        # Raw Ray/archive exceptions can retain paths or arbitrary workload data.
        receipt.update(status="failed", observations=[], cold_replacement=False)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
