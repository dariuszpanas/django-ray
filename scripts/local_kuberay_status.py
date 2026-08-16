"""Inspect bounded local KubeRay image references without mutating the stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Literal, cast

from scripts.local_kuberay_gate import (
    K8S_CONTEXT_ENV,
    K8S_NAMESPACE_ENV,
    KUBECTL_ENVIRONMENT_KEYS,
    LOCAL_CONTEXT_PATTERN,
    MAKE_RECURSION_ENVIRONMENT_KEYS,
    inspect_kubeconfig_snapshot,
    sanitized_environment,
    validate_namespace,
)
from scripts.local_resource_coordinator import (
    LOCAL_RESOURCE_INHERITANCE_ENV_KEYS,
    read_local_resource_status,
    render_local_resource_status,
)

_MAX_COMMAND_OUTPUT_BYTES: Final = 64 * 1024
_MAX_IMAGE_REFERENCES: Final = 64
_MAX_IMAGE_REFERENCE_BYTES: Final = 512
_MAX_RENDERED_STATUS_BYTES: Final = 128 * 1024
_KUBECTL_TIMEOUT_SECONDS: Final = 15
_KUBECTL_REQUEST_TIMEOUT: Final = "10s"
_KUBECONFIG_FILENAME: Final = "kubeconfig.json"
K8S_STATUS_FORMAT_ENV: Final = "DJANGO_RAY_INTERNAL_KUBERAY_STATUS_FORMAT"
K8S_STATUS_WRAPPER_ENV_KEYS: Final = (
    K8S_CONTEXT_ENV,
    K8S_NAMESPACE_ENV,
    K8S_STATUS_FORMAT_ENV,
)
_DEPLOYMENT_IMAGE_PROJECTION: Final = (
    'jsonpath={range .items[*].spec.template.spec.initContainers[*]}{.image}{"\\n"}{end}'
    '{range .items[*].spec.template.spec.containers[*]}{.image}{"\\n"}{end}'
)
_RAY_IMAGE_PROJECTION: Final = (
    "jsonpath={range .items[*].spec.headGroupSpec.template.spec.initContainers[*]}"
    '{.image}{"\\n"}{end}'
    "{range .items[*].spec.headGroupSpec.template.spec.containers[*]}"
    '{.image}{"\\n"}{end}'
    "{range .items[*].spec.workerGroupSpecs[*].template.spec.initContainers[*]}"
    '{.image}{"\\n"}{end}'
    "{range .items[*].spec.workerGroupSpecs[*].template.spec.containers[*]}"
    '{.image}{"\\n"}{end}'
)
_IMAGE_PROJECTIONS: Final = {
    "deployments.apps": _DEPLOYMENT_IMAGE_PROJECTION,
    "rayclusters.ray.io": _RAY_IMAGE_PROJECTION,
}
_KUBECTL_SCRUB_KEYS: Final = frozenset(
    {
        *KUBECTL_ENVIRONMENT_KEYS,
        *LOCAL_RESOURCE_INHERITANCE_ENV_KEYS,
        K8S_STATUS_FORMAT_ENV,
    }
)


class LocalKubeRayStatusError(RuntimeError):
    """Raised when one bounded read cannot be trusted."""


@dataclass(frozen=True, slots=True)
class KubectlReadResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class LocalKubernetesRoute:
    context: str
    server: str


@dataclass(frozen=True, slots=True)
class PrivateKubeconfigSnapshot:
    path: Path
    resolved_path: Path
    file_identity: tuple[int, int]
    digest: str
    route: LocalKubernetesRoute


def _bounded_cli_value(value: str, *, field_name: str) -> str:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise LocalKubeRayStatusError(f"{field_name} must be bounded printable ASCII") from error
    if not encoded or len(encoded) > 512 or any(not 0x21 <= byte <= 0x7E for byte in encoded):
        raise LocalKubeRayStatusError(f"{field_name} must be bounded printable ASCII")
    return value


def _pop_make_status_arguments() -> tuple[str, ...]:
    """Pop and validate one complete Make-owned status argument bundle."""

    values = {key: os.environ.pop(key) for key in K8S_STATUS_WRAPPER_ENV_KEYS if key in os.environ}
    if not values:
        return ()
    if len(values) != len(K8S_STATUS_WRAPPER_ENV_KEYS):
        raise LocalKubeRayStatusError(
            "Make-provided KubeRay status inputs must be present as one complete bundle"
        )
    context = _bounded_cli_value(values[K8S_CONTEXT_ENV], field_name="K8S_CONTEXT")
    namespace = _bounded_cli_value(values[K8S_NAMESPACE_ENV], field_name="K8S_NAMESPACE")
    output_format = _bounded_cli_value(
        values[K8S_STATUS_FORMAT_ENV], field_name="K8S_FINAL_GATE_STATUS_FORMAT"
    )
    if LOCAL_CONTEXT_PATTERN.fullmatch(context) is None:
        raise LocalKubeRayStatusError(
            "K8S_CONTEXT must select docker-desktop or a named kind-* context"
        )
    try:
        validate_namespace(namespace)
    except ValueError as error:
        raise LocalKubeRayStatusError("K8S_NAMESPACE must be exactly django-ray") from error
    if output_format not in {"text", "json"}:
        raise LocalKubeRayStatusError("K8S_FINAL_GATE_STATUS_FORMAT must be text or json")
    return (
        "--context",
        context,
        "--namespace",
        namespace,
        "--format",
        output_format,
    )


def _config_command(context: str) -> tuple[str, ...]:
    return (
        "kubectl",
        "--context",
        context,
        "config",
        "view",
        "--minify",
        "--raw",
        "--flatten",
        "-o",
        "json",
    )


def _image_command(
    *,
    snapshot: PrivateKubeconfigSnapshot,
    namespace: str,
    resource: str,
) -> tuple[str, ...]:
    return (
        "kubectl",
        "--kubeconfig",
        str(snapshot.path),
        "--context",
        snapshot.route.context,
        "--server",
        snapshot.route.server,
        f"--request-timeout={_KUBECTL_REQUEST_TIMEOUT}",
        "--namespace",
        namespace,
        "get",
        resource,
        "-o",
        _IMAGE_PROJECTIONS[resource],
    )


def _validate_allowlisted_command(
    command: Sequence[str],
    *,
    snapshot: PrivateKubeconfigSnapshot | None = None,
) -> None:
    args = tuple(command)
    if snapshot is None and len(args) == 10 and args[:2] == ("kubectl", "--context"):
        if args[3:] == (
            "config",
            "view",
            "--minify",
            "--raw",
            "--flatten",
            "-o",
            "json",
        ):
            return
    if len(args) == 14 and args[:2] == ("kubectl", "--kubeconfig"):
        context = args[4]
        server = args[6]
        namespace = args[9]
        resource = args[11]
        if (
            snapshot is not None
            and args[2] == str(snapshot.path)
            and context == snapshot.route.context
            and server == snapshot.route.server
            and args[3] == "--context"
            and args[5] == "--server"
            and args[7] == f"--request-timeout={_KUBECTL_REQUEST_TIMEOUT}"
            and args[8] == "--namespace"
            and args[10] == "get"
            and resource in _IMAGE_PROJECTIONS
            and args[12:] == ("-o", _IMAGE_PROJECTIONS[resource])
        ):
            try:
                validate_namespace(namespace)
            except ValueError:
                pass
            else:
                return
    raise LocalKubeRayStatusError("kubectl status command is outside the read-only allowlist")


def _run_kubectl(
    command: Sequence[str],
    *,
    snapshot: PrivateKubeconfigSnapshot | None = None,
) -> KubectlReadResult:
    _validate_allowlisted_command(command, snapshot=snapshot)
    if snapshot is not None:
        _verify_private_kubeconfig_snapshot(snapshot)
    completed: subprocess.CompletedProcess[bytes] | None = None
    stdout = b""
    stderr = b""
    capture_failed = False
    try:
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_stream,
            tempfile.TemporaryFile(mode="w+b") as stderr_stream,
        ):
            completed = subprocess.run(
                list(command),
                cwd=Path(__file__).resolve().parents[1],
                env=sanitized_environment(_KUBECTL_SCRUB_KEYS),
                check=False,
                stdout=stdout_stream,
                stderr=stderr_stream,
                timeout=_KUBECTL_TIMEOUT_SECONDS,
                shell=False,
            )
            if snapshot is not None:
                _verify_private_kubeconfig_snapshot(snapshot)
            stdout = _read_bounded_command_stream(stdout_stream)
            stderr = _read_bounded_command_stream(stderr_stream)
    except LocalKubeRayStatusError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        capture_failed = True
    if capture_failed or completed is None:
        # Raise outside the child/capture error handler because those exceptions
        # may retain sensitive diagnostics or private temporary paths.
        raise LocalKubeRayStatusError("bounded kubectl read failed")
    if len(stdout) > _MAX_COMMAND_OUTPUT_BYTES or len(stderr) > _MAX_COMMAND_OUTPUT_BYTES:
        raise LocalKubeRayStatusError("bounded kubectl read exceeded its output limit")
    return KubectlReadResult(completed.returncode, stdout, stderr)


def _read_bounded_command_stream(stream: BinaryIO) -> bytes:
    stream.flush()
    stream.seek(0)
    return stream.read(_MAX_COMMAND_OUTPUT_BYTES + 1)


def _successful_output(result: KubectlReadResult, *, operation: str) -> str:
    if result.returncode != 0:
        raise LocalKubeRayStatusError(f"{operation} returned a nonzero status")
    try:
        return result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LocalKubeRayStatusError(f"{operation} was not UTF-8") from error


def _inspect_kubeconfig_bytes(payload: bytes, *, context: str) -> LocalKubernetesRoute:
    parsed: object = None
    parsed_ok = False
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, RecursionError):
        pass
    else:
        parsed_ok = True
    if not parsed_ok:
        # Raise outside the parser handler because its exception may retain the
        # complete private input in ``doc`` or ``object`` attributes.
        raise LocalKubeRayStatusError("private kubeconfig snapshot is invalid")
    server: str | None = None
    try:
        server = inspect_kubeconfig_snapshot(parsed, expected_context=context)
    except (TypeError, ValueError):
        pass
    if server is None:
        raise LocalKubeRayStatusError("private kubeconfig snapshot is invalid")
    return LocalKubernetesRoute(context=context, server=server)


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _verify_private_kubeconfig_snapshot(snapshot: PrivateKubeconfigSnapshot) -> None:
    """Verify the private path, file identity, digest, and route without emitting them."""

    read_failed = False
    try:
        if snapshot.path.resolve(strict=True) != snapshot.resolved_path:
            raise LocalKubeRayStatusError("private kubeconfig snapshot path changed")
        if stat.S_ISLNK(snapshot.path.lstat().st_mode):
            raise LocalKubeRayStatusError("private kubeconfig snapshot path changed")
        with snapshot.path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise LocalKubeRayStatusError("private kubeconfig snapshot is not a regular file")
            if _file_identity(metadata) != snapshot.file_identity:
                raise LocalKubeRayStatusError("private kubeconfig snapshot file changed")
            payload = stream.read(_MAX_COMMAND_OUTPUT_BYTES + 1)
    except LocalKubeRayStatusError:
        raise
    except OSError:
        read_failed = True
    if read_failed:
        raise LocalKubeRayStatusError("private kubeconfig snapshot cannot be read")
    if len(payload) > _MAX_COMMAND_OUTPUT_BYTES:
        raise LocalKubeRayStatusError("private kubeconfig snapshot exceeds its size limit")
    if hashlib.sha256(payload).hexdigest() != snapshot.digest:
        raise LocalKubeRayStatusError("private kubeconfig snapshot content changed")
    if _inspect_kubeconfig_bytes(payload, context=snapshot.route.context) != snapshot.route:
        raise LocalKubeRayStatusError("private kubeconfig snapshot route changed")


def _create_private_kubeconfig_snapshot(
    *,
    context: str,
    temp_root: Path,
) -> PrivateKubeconfigSnapshot:
    """Capture one bounded private raw and flattened kubeconfig snapshot."""

    result = _run_kubectl(_config_command(context))
    if result.returncode != 0:
        raise LocalKubeRayStatusError("private kubeconfig capture returned a nonzero status")
    payload = result.stdout
    route = _inspect_kubeconfig_bytes(payload, context=context)
    path = temp_root / _KUBECONFIG_FILENAME
    descriptor: int | None = None
    creation_failed = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
        os.chmod(path, 0o600)
        resolved_path = path.resolve(strict=True)
        metadata = path.stat()
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        creation_failed = True
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    if creation_failed:
        raise LocalKubeRayStatusError("private kubeconfig snapshot could not be created")
    snapshot = PrivateKubeconfigSnapshot(
        path=path,
        resolved_path=resolved_path,
        file_identity=_file_identity(metadata),
        digest=hashlib.sha256(payload).hexdigest(),
        route=route,
    )
    _verify_private_kubeconfig_snapshot(snapshot)
    return snapshot


def _image_references(output: str) -> list[str]:
    images: set[str] = set()
    for line in output.splitlines():
        image = line.strip()
        if not image:
            continue
        encoded = image.encode("utf-8", errors="strict")
        if len(encoded) > _MAX_IMAGE_REFERENCE_BYTES or any(
            not 0x21 <= byte <= 0x7E for byte in encoded
        ):
            raise LocalKubeRayStatusError("deployed image reference is not bounded printable ASCII")
        images.add(image)
        if len(images) > _MAX_IMAGE_REFERENCES:
            raise LocalKubeRayStatusError("deployed image reference count exceeds the status limit")
    return sorted(images)


def _diagnostic_list(status: Mapping[str, object]) -> list[object]:
    diagnostics = status.get("diagnostics")
    if not isinstance(diagnostics, list):
        return []
    return list(diagnostics)


def read_local_kuberay_status(*, context: str, namespace: str) -> dict[str, object]:
    """Enrich host status using only local API routing and image projections."""

    status = dict(read_local_resource_status())
    diagnostics = _diagnostic_list(status)
    status["diagnostics"] = diagnostics
    mirror: dict[str, object] = {"state": "not-configured"}
    deployed: dict[str, object] = {
        "state": "unavailable",
        "provenance": "unavailable",
        "image_references": [],
    }
    status["kubernetes_mirror"] = mirror
    status["deployed_stack"] = deployed
    try:
        bounded_context = _bounded_cli_value(context, field_name="context")
        bounded_namespace = _bounded_cli_value(namespace, field_name="namespace")
        validate_namespace(bounded_namespace)
        mirror.update({"context": bounded_context, "namespace": bounded_namespace})
        deployed.update({"context": bounded_context, "namespace": bounded_namespace})
        with tempfile.TemporaryDirectory(prefix="django-ray-local-status-") as temporary:
            snapshot = _create_private_kubeconfig_snapshot(
                context=bounded_context,
                temp_root=Path(temporary),
            )
            mirror.update(
                {
                    "api_server": snapshot.route.server,
                    "observation": "local-api-validated",
                }
            )
            images: set[str] = set()
            for resource in _IMAGE_PROJECTIONS:
                output = _successful_output(
                    _run_kubectl(
                        _image_command(
                            snapshot=snapshot,
                            namespace=bounded_namespace,
                            resource=resource,
                        ),
                        snapshot=snapshot,
                    ),
                    operation=f"{resource} image projection",
                )
                images.update(_image_references(output))
                if len(images) > _MAX_IMAGE_REFERENCES:
                    raise LocalKubeRayStatusError(
                        "deployed image reference count exceeds the status limit"
                    )
        references = sorted(images)
        deployed.update(
            {
                "state": "observed" if references else "absent",
                "provenance": "image-references-only",
                "image_references": references,
            }
        )
    except (LocalKubeRayStatusError, OSError, ValueError, UnicodeError):
        mirror["observation"] = "unavailable"
        diagnostics.append(
            {
                "code": "kubernetes-status-unavailable",
                "message": "bounded local Kubernetes image-reference status is unavailable",
            }
        )
    return status


def render_local_kuberay_status(
    status: Mapping[str, object],
    *,
    output_format: Literal["text", "json"] = "text",
) -> str:
    """Render the enriched snapshot without another host or Kubernetes read."""

    if output_format == "json":
        rendered = render_local_resource_status(status, output_format="json")
    elif output_format == "text":
        rendered = render_local_resource_status(status, output_format="text")
        mirror = status.get("kubernetes_mirror")
        deployed = status.get("deployed_stack")
        mirror_mapping = cast(Mapping[str, object], mirror) if isinstance(mirror, Mapping) else {}
        deployed_mapping = (
            cast(Mapping[str, object], deployed) if isinstance(deployed, Mapping) else {}
        )
        lines = [
            rendered.rstrip("\n"),
            f"Kubernetes context: {mirror_mapping.get('context', 'unavailable')}",
            f"Kubernetes namespace: {mirror_mapping.get('namespace', 'unavailable')}",
            f"Kubernetes API observation: {mirror_mapping.get('observation', 'unavailable')}",
            f"Deployed stack: {deployed_mapping.get('state', 'unavailable')}",
            f"Deployed provenance: {deployed_mapping.get('provenance', 'unavailable')}",
        ]
        references = deployed_mapping.get("image_references")
        if isinstance(references, list):
            lines.extend(
                f"Image reference: {value}" for value in references if isinstance(value, str)
            )
        rendered = "\n".join(lines) + "\n"
    else:
        raise ValueError("local KubeRay status format must be 'text' or 'json'")
    if len(rendered.encode("utf-8")) > _MAX_RENDERED_STATUS_BYTES:
        raise LocalKubeRayStatusError("rendered local KubeRay status exceeds its output limit")
    return rendered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        make_arguments = _pop_make_status_arguments()
    except LocalKubeRayStatusError as error:
        print(error, file=sys.stderr)
        return 3
    for key in MAKE_RECURSION_ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args([*arguments, *make_arguments])
    status = read_local_kuberay_status(context=args.context, namespace=args.namespace)
    try:
        print(render_local_kuberay_status(status, output_format=args.format), end="")
    except LocalKubeRayStatusError:
        print(
            "FAILED [local-resources]: bounded local KubeRay status could not be rendered",
            file=sys.stderr,
        )
        return 3
    deployed = status.get("deployed_stack")
    deployed_state = deployed.get("state") if isinstance(deployed, Mapping) else "unavailable"
    return 3 if status.get("state") == "unknown" or deployed_state == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
