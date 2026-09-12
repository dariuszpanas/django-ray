"""Private sample deployment and local credential preparation contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.forward_k8s_service import SERVICES, forward_command
from scripts.prepare_k8s_secrets import SECRET_NAMES, credential_bundle, prepare, provision
from testproject.bootstrap_superuser import bootstrap_superuser

ROOT = Path(__file__).resolve().parents[2]


def test_local_credentials_are_excluded_from_docker_build_contexts() -> None:
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".local/" in patterns
    assert not any(pattern.startswith("!") and ".local" in pattern for pattern in patterns)


def test_tls_bootstrap_preserves_existing_material_and_uses_the_named_context() -> None:
    makefile = (ROOT / "mk/tls.mk").read_text()
    recipe = makefile.split("k8s-create-tls-secret:", 1)[1]
    assert "k8s-require-local-context" in recipe.splitlines()[0]
    assert "--dry-run=client -o yaml" not in recipe
    assert "get secret/ray-tls-certs" in recipe
    for line in recipe.splitlines():
        if "kubectl" in line:
            assert line.count("kubectl") == line.count('--context "$(K8S_CONTEXT)"')


def test_independent_secret_sets_are_random_component_scoped_and_bootstrap_is_opt_in() -> None:
    first = credential_bundle("first")
    second = credential_bundle("second")
    items = {item["metadata"]["name"]: item["stringData"] for item in first["items"]}
    assert set(items) == set(SECRET_NAMES)
    assert items["django-ray-bootstrap"]["DJANGO_BOOTSTRAP_SUPERUSER"] == "false"
    assert items["django-ray-bootstrap"]["DJANGO_SUPERUSER_USERNAME"] == ""
    assert (
        items["django-ray-database"]["POSTGRES_PASSWORD"]
        == items["django-ray-runtime"]["DATABASE_PASSWORD"]
    )
    assert set(items["django-ray-secret"]) == {"DJANGO_API_TOKEN"}
    assert set(items["django-ray-metrics"]) == {"DJANGO_METRICS_TOKEN"}
    for left, right in zip(first["items"], second["items"], strict=True):
        for key, value in left["stringData"].items():
            if any(part in key for part in ("TOKEN", "PASSWORD", "SECRET_KEY")):
                assert len(value) >= 32
                assert value != right["stringData"][key]


def test_local_preparation_preserves_existing_file_without_disclosing_values(
    tmp_path, capsys
) -> None:
    target = prepare(tmp_path, "local")
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(tmp_path, "local")
    assert target.read_bytes() == before
    assert capsys.readouterr().out == ""
    assert target.relative_to(tmp_path).as_posix() == ".local/k8s/local/secrets.json"
    assert json.loads(before)["kind"] == "List"


@pytest.mark.parametrize("namespace", ["../other", "", "UPPER", "a" * 64])
def test_preparation_rejects_namespace_escape(tmp_path, namespace) -> None:
    with pytest.raises(ValueError):
        prepare(tmp_path, namespace)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "namespace",
        "duplicate",
        "foreign",
        "extra-key",
        "type",
        "short",
        "oversize",
        "nested-key",
        "bootstrap",
        "database",
    ],
)
def test_provision_rejects_invalid_local_bundle_before_kubectl(
    tmp_path, monkeypatch, damage
) -> None:
    target = prepare(tmp_path, "local")
    bundle = json.loads(target.read_text())
    item = bundle["items"][0]
    if damage == "kind":
        item["kind"] = "ConfigMap"
    elif damage == "namespace":
        item["metadata"]["namespace"] = "foreign"
    elif damage == "duplicate":
        bundle["items"][-1] = item
    elif damage == "foreign":
        item["metadata"]["name"] = "foreign-secret"
    elif damage == "extra-key":
        item["stringData"]["DJANGO_API_TOKEN"] = "unintended-extra-token-value-is-not-allowed"
    elif damage == "type":
        item["stringData"]["DATABASE_PASSWORD"] = []
    elif damage == "short":
        item["stringData"]["DATABASE_PASSWORD"] = "short"
    elif damage == "nested-key":
        item["metadata"]["annotations"] = {"unexpected": "field"}
    elif damage == "bootstrap":
        bundle["items"][4]["stringData"]["DJANGO_BOOTSTRAP_SUPERUSER"] = "true"
    elif damage == "database":
        item["stringData"]["DATABASE_PASSWORD"] = "different-uncoordinated-database-password"
    target.write_text(json.dumps(bundle) if damage != "oversize" else " " * 32769)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("kubectl ran before validation")
    )
    with pytest.raises(ValueError):
        provision(tmp_path, "local", "docker-desktop")


def test_provision_sends_only_validated_stdin_and_preserves_a_complete_set(
    tmp_path, monkeypatch, capsys
) -> None:
    target = prepare(tmp_path, "local")
    before = target.read_bytes()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["capture_output"] is True and kwargs["timeout"] == 30
        assert command[:5] == ["kubectl", "--context", "kind-test", "--namespace", "local"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    provision(tmp_path, "local", "kind-test")
    assert len(calls) == 2
    assert calls[1][0][-3:] == ["create", "-f", "-"]
    assert json.loads(calls[1][1]["input"]) == json.loads(before)
    calls.clear()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command)
            or subprocess.CompletedProcess(
                command, 0, "\n".join(f"secret/{name}" for name in SECRET_NAMES), ""
            )
        ),
    )
    provision(tmp_path, "local", "kind-test")
    assert len(calls) == 1 and "create" not in calls[0]
    assert target.read_bytes() == before
    assert capsys.readouterr().out == ""


def test_partial_live_secret_set_is_never_overwritten_or_completed(tmp_path, monkeypatch) -> None:
    prepare(tmp_path, "local")
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command)
            or subprocess.CompletedProcess(command, 0, "secret/django-ray-secret\n", "")
        ),
    )
    with pytest.raises(ValueError, match="partial Secret set"):
        provision(tmp_path, "local", "docker-desktop")
    assert len(calls) == 1


def test_missing_file_and_junction_parent_fail_before_kubectl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected kubectl")
    )
    with pytest.raises(ValueError, match="prepare"):
        provision(tmp_path, "local", "docker-desktop")
    prepare(tmp_path, "local")
    monkeypatch.setattr(Path, "is_junction", lambda path: path.name == "k8s")
    with pytest.raises(ValueError, match="junctions"):
        provision(tmp_path, "local", "docker-desktop")


@pytest.mark.parametrize("value", ["True", "yes", "1", ""])
def test_bootstrap_flag_never_accepts_ambiguous_opt_in(value) -> None:
    with pytest.raises(ValueError):
        bootstrap_superuser({"DJANGO_BOOTSTRAP_SUPERUSER": value})


def test_bootstrap_disabled_and_missing_credentials_do_not_access_database() -> None:
    assert bootstrap_superuser({}) is False
    with pytest.raises(ValueError, match="requires explicit"):
        bootstrap_superuser({"DJANGO_BOOTSTRAP_SUPERUSER": "true"})


@pytest.mark.parametrize("password", ["short", " " * 32, "x" * 32, "too-long-" * 70])
def test_bootstrap_rejects_weak_or_unbounded_passwords_before_database_access(password) -> None:
    with pytest.raises(ValueError, match="password"):
        bootstrap_superuser(
            {
                "DJANGO_BOOTSTRAP_SUPERUSER": "true",
                "DJANGO_SUPERUSER_USERNAME": "chosen-admin",
                "DJANGO_SUPERUSER_EMAIL": "chosen@example.invalid",
                "DJANGO_SUPERUSER_PASSWORD": password,
            }
        )


@pytest.mark.django_db
def test_bootstrap_reapply_does_not_silently_leave_or_rotate_a_password(django_user_model) -> None:
    environment = {
        "DJANGO_BOOTSTRAP_SUPERUSER": "true",
        "DJANGO_SUPERUSER_USERNAME": "chosen-admin",
        "DJANGO_SUPERUSER_EMAIL": "chosen@example.invalid",
        "DJANGO_SUPERUSER_PASSWORD": "first-explicit-random-password-value",
    }
    assert bootstrap_superuser(environment) is True
    user = django_user_model.objects.get(username="chosen-admin")
    assert user.is_superuser and user.check_password(environment["DJANGO_SUPERUSER_PASSWORD"])
    environment["DJANGO_SUPERUSER_PASSWORD"] = "different-explicit-random-password-value"
    with pytest.raises(ValueError, match="changepassword"):
        bootstrap_superuser(environment)
    user.refresh_from_db()
    assert not user.check_password(environment["DJANGO_SUPERUSER_PASSWORD"])


@pytest.mark.parametrize("service", SERVICES)
def test_forwarding_is_always_loopback_and_does_not_open_gcs_or_client(service) -> None:
    command = forward_command("docker-desktop", "django-ray", service)
    assert command[command.index("--address") + 1] == "127.0.0.1"
    assert ":6379" not in command[-1] and ":10001" not in command[-1]


@pytest.fixture(scope="module")
def all_overlays():
    kubectl = shutil.which("kubectl")
    if not kubectl:
        pytest.skip("kubectl is required for pure Kustomize rendering")
    result = {}
    for path in sorted((ROOT / "k8s/overlays").glob("*/kustomization.yaml")):
        completed = subprocess.run(
            [kubectl, "kustomize", str(path.parent)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        result[path.parent.name] = [item for item in yaml.safe_load_all(completed.stdout) if item]
    return result


def _containers(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"containers", "initContainers"}:
                yield from child
            else:
                yield from _containers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _containers(child)


def test_every_overlay_keeps_services_private_and_credentials_unrendered(all_overlays) -> None:
    for profile, resources in all_overlays.items():
        for resource in resources:
            assert resource["kind"] not in {"Ingress", "Secret"}, profile
            if resource["kind"] == "Service":
                assert resource["spec"].get("type", "ClusterIP") == "ClusterIP", profile
                assert all("nodePort" not in port for port in resource["spec"]["ports"]), profile
            if resource["kind"] == "RayCluster":
                assert resource["spec"]["headGroupSpec"]["serviceType"] == "ClusterIP", profile
            for container in _containers(resource):
                assert all("secretRef" not in value for value in container.get("envFrom", []))
                env = {entry["name"]: entry for entry in container.get("env", [])}
                name = container["name"]
                if name.startswith(("ray-", "django-ray-worker")):
                    assert "DJANGO_API_TOKEN" not in env
                    assert not any(key.startswith("DJANGO_SUPERUSER") for key in env)
                    if "--sync" not in container.get("args", []):
                        assert env["RAY_AUTH_MODE"]["value"] == "token"
                        assert (
                            env["RAY_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"]
                            == "django-ray-auth"
                        )
                if name == "grafana":
                    assert env["GF_AUTH_ANONYMOUS_ENABLED"]["value"] == "false"
                    assert "valueFrom" in env["GF_SECURITY_ADMIN_PASSWORD"]
                if name == "django-web":
                    assert "DJANGO_API_TOKEN" in env and "DJANGO_METRICS_TOKEN" in env
                    if profile in {"kuberay-kind", "kong-local"}:
                        assert env["DJANGO_DEMO_WORKLOADS_ENABLED"]["value"] == "true"
                        assert (
                            env["DJANGO_DEMO_TOKEN"]["valueFrom"]["secretKeyRef"]["name"]
                            == "django-ray-demo"
                        )
                    else:
                        assert "DJANGO_DEMO_TOKEN" not in env
                elif name not in {"wait-for-db", "wait-for-ray"}:
                    assert "DJANGO_DEMO_TOKEN" not in env
                if name in {"grafana", "prometheus", "dashboard-importer"}:
                    assert "DJANGO_API_TOKEN" not in env
