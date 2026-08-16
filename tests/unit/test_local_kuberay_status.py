from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from scripts import local_kuberay_status as status_module
from scripts.local_resource_coordinator import LOCAL_RESOURCE_INHERITANCE_ENV_KEYS


def _write_child_output(
    run_kwargs: dict[str, object],
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> None:
    cast(BinaryIO, run_kwargs["stdout"]).write(stdout)
    cast(BinaryIO, run_kwargs["stderr"]).write(stderr)


def _host_status() -> dict[str, object]:
    return {
        "schema": "django-ray-local-resource-status/v1",
        "state": "active",
        "safe_action": "wait",
        "termination_authority": "none",
        "local_liveness": "active-owner-live",
        "active": None,
        "queue": [],
        "orphaned": None,
        "last_completed": None,
        "diagnostics": [],
        "kubernetes_mirror": {"state": "not-configured"},
        "deployed_stack": {
            "state": "unavailable",
            "provenance": "unavailable",
        },
    }


def _kubeconfig_bytes(
    *,
    proxy_url: str | None = None,
    token: str = "private-kubeconfig-token",
) -> bytes:
    cluster: dict[str, object] = {
        "server": "https://kubernetes.docker.internal:6443",
        "certificate-authority-data": "private-certificate-data",
    }
    if proxy_url is not None:
        cluster["proxy-url"] = proxy_url
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "docker-desktop",
            "clusters": [{"name": "docker-desktop", "cluster": cluster}],
            "contexts": [
                {
                    "name": "docker-desktop",
                    "context": {
                        "cluster": "docker-desktop",
                        "user": "docker-desktop",
                    },
                }
            ],
            "users": [
                {
                    "name": "docker-desktop",
                    "user": {"token": token},
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _snapshot_for_test(
    temp_root: Path,
    *,
    payload: bytes | None = None,
) -> status_module.PrivateKubeconfigSnapshot:
    content = _kubeconfig_bytes() if payload is None else payload
    path = temp_root / status_module._KUBECONFIG_FILENAME
    path.write_bytes(content)
    os.chmod(path, 0o600)
    metadata = path.stat()
    return status_module.PrivateKubeconfigSnapshot(
        path=path,
        resolved_path=path.resolve(strict=True),
        file_identity=status_module._file_identity(metadata),
        digest=hashlib.sha256(content).hexdigest(),
        route=status_module.LocalKubernetesRoute(
            context="docker-desktop",
            server="https://kubernetes.docker.internal:6443",
        ),
    )


def test_status_uses_only_local_route_and_image_reference_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    image_responses = iter(
        (
            b"registry.example/django-ray:tree\npostgres:16\n",
            b"registry.example/django-ray:tree\nrayproject/ray:2.56.0-py312\n",
        )
    )
    snapshot_paths: list[Path] = []
    monkeypatch.setattr(status_module, "read_local_resource_status", _host_status)

    def run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        captured = tuple(command)
        commands.append(captured)
        if "config" in command:
            _write_child_output(kwargs, stdout=_kubeconfig_bytes())
            return subprocess.CompletedProcess(command, 0)
        snapshot_path = Path(command[2])
        assert snapshot_path.is_file()
        snapshot_paths.append(snapshot_path)
        _write_child_output(kwargs, stdout=next(image_responses))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(status_module.subprocess, "run", run)

    observed = status_module.read_local_kuberay_status(
        context="docker-desktop",
        namespace="django-ray",
    )

    assert observed["safe_action"] == "wait"
    assert observed["termination_authority"] == "none"
    assert observed["kubernetes_mirror"] == {
        "state": "not-configured",
        "context": "docker-desktop",
        "namespace": "django-ray",
        "api_server": "https://kubernetes.docker.internal:6443",
        "observation": "local-api-validated",
    }
    assert observed["deployed_stack"] == {
        "state": "observed",
        "provenance": "image-references-only",
        "image_references": [
            "postgres:16",
            "rayproject/ray:2.56.0-py312",
            "registry.example/django-ray:tree",
        ],
        "context": "docker-desktop",
        "namespace": "django-ray",
    }
    assert commands[0] == status_module._config_command("docker-desktop")
    get_commands = [command for command in commands if "get" in command]
    assert len(get_commands) == 2
    assert len(commands) == 3
    assert all(command[1] == "--kubeconfig" for command in get_commands)
    assert all(command[2] == str(snapshot_paths[0]) for command in get_commands)
    assert all(
        command[5:7] == ("--server", "https://kubernetes.docker.internal:6443")
        for command in get_commands
    )
    rendered_commands = "\n".join(" ".join(command) for command in commands)
    assert " get deployments.apps " in rendered_commands
    assert " get rayclusters.ray.io " in rendered_commands
    assert " get pods " not in rendered_commands
    assert " -o json " not in rendered_commands
    assert ".metadata.name" not in rendered_commands
    assert ".metadata.uid" not in rendered_commands
    assert ".imageID" not in rendered_commands
    assert snapshot_paths
    serialized = json.dumps(observed)
    assert "private-kubeconfig-token" not in serialized
    assert "private-certificate-data" not in serialized
    assert all(str(path) not in serialized for path in snapshot_paths)
    assert all(not path.exists() and not path.parent.exists() for path in snapshot_paths)


def test_unavailable_kubernetes_read_is_bounded_and_preserves_host_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_module, "read_local_resource_status", _host_status)

    def fail(
        _command: Sequence[str],
        *,
        snapshot: status_module.PrivateKubeconfigSnapshot | None = None,
    ) -> status_module.KubectlReadResult:
        del snapshot
        raise status_module.LocalKubeRayStatusError("secret-shaped child diagnostic")

    monkeypatch.setattr(status_module, "_run_kubectl", fail)

    observed = status_module.read_local_kuberay_status(
        context="docker-desktop",
        namespace="django-ray",
    )

    assert observed["safe_action"] == "wait"
    assert observed["termination_authority"] == "none"
    assert observed["kubernetes_mirror"] == {
        "state": "not-configured",
        "context": "docker-desktop",
        "namespace": "django-ray",
        "observation": "unavailable",
    }
    assert observed["deployed_stack"] == {
        "state": "unavailable",
        "provenance": "unavailable",
        "image_references": [],
        "context": "docker-desktop",
        "namespace": "django-ray",
    }
    assert observed["diagnostics"] == [
        {
            "code": "kubernetes-status-unavailable",
            "message": "bounded local Kubernetes image-reference status is unavailable",
        }
    ]
    assert "secret-shaped" not in json.dumps(observed)


@pytest.mark.parametrize(
    "command",
    (
        ("kubectl", "--context", "docker-desktop", "get", "pods", "-o", "json"),
        ("kubectl", "--context", "docker-desktop", "apply", "-f", "workloads.yaml"),
        (
            "kubectl",
            "--context",
            "docker-desktop",
            "config",
            "view",
            "--minify",
            "-o",
            "json",
        ),
    ),
)
def test_status_rejects_every_non_allowlisted_kubectl_shape(
    command: tuple[str, ...],
) -> None:
    with pytest.raises(
        status_module.LocalKubeRayStatusError,
        match="outside the read-only allowlist",
    ):
        status_module._validate_allowlisted_command(command)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda command: (*command[:2], "different-kubeconfig", *command[3:]),
        lambda command: (*command[:6], "https://127.0.0.1:7443", *command[7:]),
        lambda command: (*command[:7], "--proxy-url=http://127.0.0.1:8080", *command[7:]),
        lambda command: (*command[:-1], "json"),
    ),
)
def test_status_rejects_adversarial_get_route_argv(
    mutate: Callable[[tuple[str, ...]], tuple[str, ...]],
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_for_test(tmp_path)
    command = status_module._image_command(
        snapshot=snapshot,
        namespace="django-ray",
        resource="deployments.apps",
    )
    adversarial = mutate(command)

    with pytest.raises(
        status_module.LocalKubeRayStatusError,
        match="outside the read-only allowlist",
    ):
        status_module._validate_allowlisted_command(
            adversarial,
            snapshot=snapshot,
        )


def test_status_rejects_proxy_route_in_captured_snapshot_without_secret_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "proxy-snapshot-secret"
    payload = _kubeconfig_bytes(
        proxy_url="http://127.0.0.1:8080",
        token=secret,
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(status_module, "read_local_resource_status", _host_status)

    def run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(tuple(command))
        _write_child_output(kwargs, stdout=payload)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(status_module.subprocess, "run", run)

    observed = status_module.read_local_kuberay_status(
        context="docker-desktop",
        namespace="django-ray",
    )

    assert observed["safe_action"] == "wait"
    assert observed["termination_authority"] == "none"
    assert observed["deployed_stack"] == {
        "state": "unavailable",
        "provenance": "unavailable",
        "image_references": [],
        "context": "docker-desktop",
        "namespace": "django-ray",
    }
    assert len(commands) == 1
    serialized = json.dumps(observed)
    assert secret not in serialized
    assert "127.0.0.1:8080" not in serialized


def test_status_fails_closed_when_private_snapshot_gains_proxy_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "drifted-proxy-secret"
    commands: list[tuple[str, ...]] = []
    snapshot_paths: list[Path] = []
    monkeypatch.setattr(status_module, "read_local_resource_status", _host_status)

    def run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(tuple(command))
        if "config" in command:
            _write_child_output(kwargs, stdout=_kubeconfig_bytes())
            return subprocess.CompletedProcess(command, 0)
        snapshot_path = Path(command[2])
        snapshot_paths.append(snapshot_path)
        snapshot_path.write_bytes(
            _kubeconfig_bytes(
                proxy_url="http://127.0.0.1:8080",
                token=secret,
            )
        )
        _write_child_output(kwargs, stdout=b"registry.example/django-ray:tree\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(status_module.subprocess, "run", run)

    observed = status_module.read_local_kuberay_status(
        context="docker-desktop",
        namespace="django-ray",
    )

    assert observed["safe_action"] == "wait"
    assert observed["termination_authority"] == "none"
    assert observed["deployed_stack"] == {
        "state": "unavailable",
        "provenance": "unavailable",
        "image_references": [],
        "context": "docker-desktop",
        "namespace": "django-ray",
    }
    assert len(commands) == 2
    assert commands[1][10:12] == ("get", "deployments.apps")
    serialized = json.dumps(observed)
    assert secret not in serialized
    assert all(str(path) not in serialized for path in snapshot_paths)
    assert all(not path.exists() and not path.parent.exists() for path in snapshot_paths)


def test_status_rejects_same_content_snapshot_path_replacement_before_get(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _kubeconfig_bytes()
    snapshot = _snapshot_for_test(tmp_path, payload=payload)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(payload)
    os.chmod(replacement, 0o600)
    os.replace(replacement, snapshot.path)
    assert status_module._file_identity(snapshot.path.stat()) != snapshot.file_identity
    child_called = False

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal child_called
        child_called = True
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(status_module.subprocess, "run", run)

    with pytest.raises(status_module.LocalKubeRayStatusError, match="file changed"):
        status_module._run_kubectl(
            status_module._image_command(
                snapshot=snapshot,
                namespace="django-ray",
                resource="deployments.apps",
            ),
            snapshot=snapshot,
        )

    assert child_called is False


@pytest.mark.parametrize("failure_type", (KeyboardInterrupt, SystemExit))
def test_private_snapshot_is_cleaned_after_every_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    snapshot_paths: list[Path] = []
    monkeypatch.setattr(status_module, "read_local_resource_status", _host_status)

    def run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if "config" in command:
            _write_child_output(kwargs, stdout=_kubeconfig_bytes())
            return subprocess.CompletedProcess(command, 0)
        snapshot_paths.append(Path(command[2]))
        raise failure_type()

    monkeypatch.setattr(status_module.subprocess, "run", run)

    with pytest.raises(failure_type):
        status_module.read_local_kuberay_status(
            context="docker-desktop",
            namespace="django-ray",
        )

    assert snapshot_paths
    assert all(not path.exists() and not path.parent.exists() for path in snapshot_paths)


def test_kubectl_read_scrubs_routing_and_coordinator_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    scrubbed_keys = {
        *status_module.KUBECTL_ENVIRONMENT_KEYS,
        *LOCAL_RESOURCE_INHERITANCE_ENV_KEYS,
        status_module.K8S_STATUS_FORMAT_ENV,
    }
    for key in scrubbed_keys:
        monkeypatch.setenv(key, f"secret-{key}")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        _write_child_output(kwargs, stdout=b"projection\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(status_module.subprocess, "run", run)

    result = status_module._run_kubectl(status_module._config_command("docker-desktop"))

    assert result.stdout == b"projection\n"
    assert captured["shell"] is False
    assert "capture_output" not in captured
    assert callable(getattr(captured["stdout"], "write", None))
    assert callable(getattr(captured["stderr"], "write", None))
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert not ({key.upper() for key in environment} & scrubbed_keys)
    assert all(key in os.environ for key in LOCAL_RESOURCE_INHERITANCE_ENV_KEYS)


@pytest.mark.parametrize("stream_name", ("stdout", "stderr"))
def test_kubectl_read_rejects_unbounded_child_output(
    monkeypatch: pytest.MonkeyPatch,
    stream_name: str,
) -> None:
    payloads = {"stdout": b"", "stderr": b""}
    payloads[stream_name] = b"x" * (status_module._MAX_COMMAND_OUTPUT_BYTES + 1)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        _write_child_output(
            kwargs,
            stdout=payloads["stdout"],
            stderr=payloads["stderr"],
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(status_module.subprocess, "run", run)

    with pytest.raises(
        status_module.LocalKubeRayStatusError,
        match="exceeded its output limit",
    ):
        status_module._run_kubectl(status_module._config_command("docker-desktop"))


@pytest.mark.parametrize(
    ("outcome", "expected_exception"),
    (
        ("success", None),
        ("os-error", status_module.LocalKubeRayStatusError),
        ("keyboard-interrupt", KeyboardInterrupt),
        ("system-exit", SystemExit),
    ),
)
def test_kubectl_capture_streams_are_cleaned_for_every_child_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    expected_exception: type[BaseException] | None,
) -> None:
    real_temporary_file = status_module.tempfile.TemporaryFile
    streams: list[BinaryIO] = []

    def temporary_file(*, mode: str) -> BinaryIO:
        stream = cast(BinaryIO, real_temporary_file(mode=mode))
        streams.append(stream)
        return stream

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if outcome == "success":
            _write_child_output(kwargs, stdout=b"projection\n")
            return subprocess.CompletedProcess(command, 0)
        if outcome == "os-error":
            raise OSError(str(tmp_path / "secret-child-path"))
        if outcome == "keyboard-interrupt":
            raise KeyboardInterrupt
        raise SystemExit

    monkeypatch.setattr(status_module.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(status_module.tempfile, "TemporaryFile", temporary_file)
    monkeypatch.setattr(status_module.subprocess, "run", run)

    if expected_exception is None:
        result = status_module._run_kubectl(status_module._config_command("docker-desktop"))
        assert result.stdout == b"projection\n"
    else:
        with pytest.raises(expected_exception) as raised:
            status_module._run_kubectl(status_module._config_command("docker-desktop"))
        assert str(tmp_path) not in str(raised.value)

    assert len(streams) == 2
    assert all(stream.closed for stream in streams)
    assert not any(tmp_path.iterdir())


def test_kubectl_read_spools_large_real_child_output_without_buffering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    emitted_bytes = 16 * 1024 * 1024
    chunk_bytes = 64 * 1024
    child = (
        "import os\n"
        f"chunk = b'x' * {chunk_bytes}\n"
        f"for _ in range({emitted_bytes // chunk_bytes}):\n"
        "    os.write(1, chunk)\n"
    )
    monkeypatch.setattr(
        status_module,
        "_validate_allowlisted_command",
        lambda _command, *, snapshot=None: None,
    )
    monkeypatch.setattr(status_module.tempfile, "tempdir", str(tmp_path))

    tracemalloc.start()
    try:
        with pytest.raises(
            status_module.LocalKubeRayStatusError,
            match="exceeded its output limit",
        ):
            status_module._run_kubectl((sys.executable, "-I", "-S", "-c", child))
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < emitted_bytes // 4
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    "output",
    (
        "x" * (status_module._MAX_IMAGE_REFERENCE_BYTES + 1),
        "\n".join(
            f"registry.example/image:{index}"
            for index in range(status_module._MAX_IMAGE_REFERENCES + 1)
        ),
    ),
)
def test_image_reference_projection_is_bounded(output: str) -> None:
    with pytest.raises(status_module.LocalKubeRayStatusError):
        status_module._image_references(output)


def test_status_rendering_keeps_safe_action_and_no_termination_authority() -> None:
    observed = _host_status()
    observed["kubernetes_mirror"] = {
        "state": "not-configured",
        "context": "docker-desktop",
        "namespace": "django-ray",
        "observation": "local-api-validated",
    }
    observed["deployed_stack"] = {
        "state": "observed",
        "provenance": "image-references-only",
        "image_references": ["django-ray:tree"],
    }

    text = status_module.render_local_kuberay_status(observed)
    payload = json.loads(status_module.render_local_kuberay_status(observed, output_format="json"))

    assert "Safe action: wait" in text
    assert "Termination authority: none" in text
    assert "Kubernetes mirror: not-configured" in text
    assert text.count("Kubernetes mirror:") == 1
    assert "Deployed provenance: image-references-only" in text
    assert payload["safe_action"] == "wait"
    assert payload["termination_authority"] == "none"


def _set_make_status_bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    context: str = "docker-desktop",
    namespace: str = "django-ray",
    output_format: str = "text",
) -> None:
    values = {
        status_module.K8S_CONTEXT_ENV: context,
        status_module.K8S_NAMESPACE_ENV: namespace,
        status_module.K8S_STATUS_FORMAT_ENV: output_format,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_make_status_bundle_is_one_argv_per_value_and_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[str, str]] = []
    status = _host_status()
    status["deployed_stack"] = {"state": "observed"}

    def read_status(*, context: str, namespace: str) -> dict[str, object]:
        observed.append((context, namespace))
        return status

    _set_make_status_bundle(monkeypatch, output_format="json")
    for key in status_module.MAKE_RECURSION_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "K8S_CONTEXT=hostile-make-metadata")
    monkeypatch.setattr(status_module, "read_local_kuberay_status", read_status)
    monkeypatch.setattr(
        status_module,
        "render_local_kuberay_status",
        lambda _status, *, output_format: f"{output_format}\n",
    )

    assert status_module.main([]) == 0

    assert observed == [("docker-desktop", "django-ray")]
    assert capsys.readouterr().out == "json\n"
    assert all(key not in os.environ for key in status_module.K8S_STATUS_WRAPPER_ENV_KEYS)
    assert all(key not in os.environ for key in status_module.MAKE_RECURSION_ENVIRONMENT_KEYS)
    assert status_module.K8S_STATUS_FORMAT_ENV in status_module._KUBECTL_SCRUB_KEYS


def test_partial_make_status_bundle_fails_before_any_kubectl_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(status_module.K8S_CONTEXT_ENV, "docker-desktop")
    monkeypatch.setattr(
        status_module,
        "read_local_kuberay_status",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a partial Make bundle must fail before status reads")
        ),
    )

    assert status_module.main([]) == 3
    assert all(key not in os.environ for key in status_module.K8S_STATUS_WRAPPER_ENV_KEYS)


@pytest.mark.parametrize(
    ("field_name", "hostile"),
    (
        ("context", 'docker-desktop" ; true #'),
        ("namespace", 'django-ray" && true #'),
        ("output_format", "$(error must-not-expand)"),
        ("output_format", '"unterminated'),
        ("context", "\N{SNOWMAN}"),
        ("context", "x" * 513),
    ),
)
def test_make_status_shell_data_fails_before_any_kubectl_read(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    hostile: str,
) -> None:
    _set_make_status_bundle(monkeypatch, **{field_name: hostile})
    monkeypatch.setattr(
        status_module,
        "read_local_kuberay_status",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid Make status data must fail before status reads")
        ),
    )

    assert status_module.main([]) == 3
    assert all(key not in os.environ for key in status_module.K8S_STATUS_WRAPPER_ENV_KEYS)


def _write_kubectl_tripwire(*, directory: Path, marker: Path) -> None:
    if os.name == "nt":
        wrapper = directory / "kubectl.cmd"
        wrapper.write_text(
            f'@echo off\necho attempted>"{marker}"\nexit /b 97\n',
            encoding="utf-8",
        )
        return
    wrapper = directory / "kubectl"
    wrapper.write_text(
        f"#!/bin/sh\nprintf attempted > {shlex.quote(str(marker))}\nexit 97\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)


@pytest.mark.parametrize(
    ("variable", "hostile"),
    (
        ("K8S_CONTEXT", 'docker-desktop" ; true #'),
        ("K8S_NAMESPACE", 'django-ray" && true #'),
        ("K8S_FINAL_GATE_STATUS_FORMAT", "$(error must-not-expand)"),
        ("K8S_FINAL_GATE_STATUS_FORMAT", '"unterminated'),
    ),
)
def test_real_make_status_rejects_shell_data_before_any_kubectl_read(
    tmp_path: Path,
    variable: str,
    hostile: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository status target")
    values = {
        "K8S_CONTEXT": "docker-desktop",
        "K8S_NAMESPACE": "django-ray",
        "K8S_FINAL_GATE_STATUS_FORMAT": "text",
    }
    values[variable] = hostile
    marker = tmp_path / "kubectl-attempted"
    _write_kubectl_tripwire(directory=tmp_path, marker=marker)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment.get('PATH', '')}"

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "k8s-final-gate-status",
            *(f"{key}={value}" for key, value in values.items()),
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert hostile not in result.stdout
    assert hostile not in result.stderr
    assert not marker.exists()
    assert "kubectl" not in result.stdout.lower()
