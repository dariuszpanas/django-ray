from __future__ import annotations

import builtins
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from scripts.local_kuberay_auth import (
    GRAFANA_AUTH_RECEIPT_KEYS,
    RAY_AUTH_RECEIPT_KEYS,
    grafana_auth_probe_script,
    ray_auth_probe_script,
    validate_auth_receipt,
)

TOKEN = "local-test-ray-token-with-more-than-32-characters"
PASSWORD = "local-test-grafana-password-with-more-than-32-characters"


@pytest.mark.parametrize(
    "surface,keys", [("ray", RAY_AUTH_RECEIPT_KEYS), ("grafana", GRAFANA_AUTH_RECEIPT_KEYS)]
)
@pytest.mark.parametrize("damage", [None, "missing", "false", "integer", "schema", "extra"])
def test_receipt_requires_every_exact_boolean_and_no_extra_diagnostics(surface, keys, damage):
    payload = dict.fromkeys(keys, True) | {"schema_version": 1}
    key = next(iter(keys))
    if damage == "missing":
        payload.pop(key)
    elif damage == "false":
        payload[key] = False
    elif damage == "integer":
        payload[key] = 1
    elif damage == "schema":
        payload["schema_version"] = True
    elif damage == "extra":
        payload["token"] = TOKEN
    if damage is None:
        validate_auth_receipt(payload, surface=surface)
    else:
        with pytest.raises(ValueError, match="did not prove"):
            validate_auth_receipt(payload, surface=surface)


@pytest.mark.parametrize("payload", [None, [], True, {"schema_version": 1}])
def test_receipt_rejects_non_receipts(payload):
    with pytest.raises(ValueError):
        validate_auth_receipt(payload, surface="ray")


def test_unknown_surface_is_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        validate_auth_receipt({}, surface="other")


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.code = status


def install_http(monkeypatch, *, damage=None):
    calls = []

    def open_request(request, *, timeout):
        assert timeout == 5
        calls.append(request)
        if damage == "unavailable":
            raise URLError(TOKEN)
        if damage == "redirect":
            raise HTTPError(request.full_url, 302, TOKEN, {}, io.BytesIO())
        authorization = request.get_header("Authorization")
        if "grafana-svc" in request.full_url:
            if request.full_url.endswith("/api/user"):
                body = json.dumps(
                    {"login": "local-admin", "isGrafanaAdmin": damage != "not_admin"}
                ).encode()
                return Response(200, body)
            status = 200 if damage == "anonymous_access" else 401
            return Response(status, b"{}")
        if authorization is None:
            status, body = 401, b"Unauthorized: Missing authentication token"
        elif authorization == "Bearer " + TOKEN:
            status, body = 200, b'{"version":"test"}'
            if damage == "positive_failure":
                status = 503
            elif damage == "oversize":
                body = b"x" * 4097
        else:
            status, body = 403, b"Forbidden: Invalid authentication token"
        if damage == "anonymous_access":
            status = 200
        return Response(status, body)

    def opener(*handlers):
        proxy, redirect = handlers
        assert proxy.proxies == {}
        assert redirect.redirect_request(None, None, 302, "", {}, "http://other") is None
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr("urllib.request.build_opener", opener)
    return calls


def execute_ray(monkeypatch, *, damage=None):
    monkeypatch.setenv("RAY_AUTH_MODE", "token")
    monkeypatch.setenv("RAY_AUTH_TOKEN", TOKEN)
    if damage == "local_configuration_error":
        monkeypatch.delenv("RAY_AUTH_TOKEN")
    # Negative raw RPCs must remain anonymous despite an inherited loader path.
    monkeypatch.setenv("RAY_AUTH_TOKEN_PATH", "/existing/manager/token")
    metadata_seen = []

    class RpcError(Exception):
        def code(self):
            return "UNAVAILABLE" if damage == "rpc_unavailable" else "UNAUTHENTICATED"

        def details(self):
            return self.args[0]

    class Channel:
        def __init__(self, address, *, options):
            assert options == (("grpc.max_receive_message_length", 65536),)
            self.address = address

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Stub:
        def __init__(self, channel):
            self.channel = channel

        def call(self, request, *, metadata, timeout):
            assert timeout == 5
            metadata_seen.append((self.channel.address, metadata))
            authorized = metadata == (("authorization", "Bearer " + TOKEN),)
            if not authorized:
                if damage == "rpc_anonymous_access":
                    return SimpleNamespace(
                        json="{}", status=SimpleNamespace(code=0), cluster_id=b"cluster"
                    )
                details = "Invalid or missing authentication token"
                if self.channel.address.endswith(":6379"):
                    details = (
                        "WrongClusterID: another cluster"
                        if damage == "wrong_cluster"
                        else "InvalidAuthToken: Authentication token is missing or incorrect"
                    )
                raise RpcError(details)
            return SimpleNamespace(json="{}", status=SimpleNamespace(code=0), cluster_id=b"cluster")

        ClusterInfo = GetClusterId = call

    grpc = SimpleNamespace(
        insecure_channel=Channel,
        RpcError=RpcError,
        StatusCode=SimpleNamespace(UNAUTHENTICATED="UNAUTHENTICATED"),
    )
    generated = SimpleNamespace(
        gcs_service_pb2=SimpleNamespace(GetClusterIdRequest=lambda: object()),
        gcs_service_pb2_grpc=SimpleNamespace(NodeInfoGcsServiceStub=Stub),
        ray_client_pb2=SimpleNamespace(
            ClusterInfoType=SimpleNamespace(PING=6), ClusterInfoRequest=lambda **kwargs: kwargs
        ),
        ray_client_pb2_grpc=SimpleNamespace(RayletDriverStub=Stub),
    )
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "grpc":
            return grpc
        if name == "ray.core.generated":
            return generated
        return original_import(name, *args, **kwargs)

    namespace = {"__builtins__": vars(builtins) | {"__import__": import_module}}
    exec(compile(ray_auth_probe_script(), "<ray-auth-probe>", "exec"), namespace)
    return metadata_seen


def test_ray_probe_requires_positive_and_both_negative_paths_without_sdk_token_loading(
    monkeypatch, capsys
):
    http_calls = install_http(monkeypatch)
    rpc_calls = execute_ray(monkeypatch)
    output = capsys.readouterr()
    validate_auth_receipt(json.loads(output.out), surface="ray")
    assert output.err == ""
    assert len(http_calls) == len(rpc_calls) == 6
    assert [metadata for _, metadata in rpc_calls][::3] == [(), ()]
    assert TOKEN not in output.out


@pytest.mark.parametrize(
    "damage",
    [
        "unavailable",
        "redirect",
        "positive_failure",
        "oversize",
        "anonymous_access",
        "rpc_unavailable",
        "rpc_anonymous_access",
        "wrong_cluster",
        "local_configuration_error",
    ],
)
def test_ray_probe_never_calls_connectivity_or_wrong_cluster_errors_auth_denial(
    monkeypatch, capsys, damage
):
    install_http(monkeypatch, damage=damage)
    with pytest.raises(SystemExit) as error:
        execute_ray(monkeypatch, damage=damage)
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Ray authentication probe failed\n"


@pytest.mark.parametrize(
    "damage", [None, "unavailable", "redirect", "anonymous_access", "not_admin"]
)
def test_grafana_probe_checks_anonymous_search_and_existing_admin_identity(
    monkeypatch, capsys, damage
):
    monkeypatch.setenv("GF_SECURITY_ADMIN_USER", "local-admin")
    monkeypatch.setenv("GF_SECURITY_ADMIN_PASSWORD", PASSWORD)
    calls = install_http(monkeypatch, damage=damage)
    script = compile(grafana_auth_probe_script(), "<grafana-auth-probe>", "exec")
    if damage is None:
        exec(script, {})
        output = capsys.readouterr()
        validate_auth_receipt(json.loads(output.out), surface="grafana")
        assert [call.full_url.split(":3000")[1] for call in calls] == [
            "/api/search?limit=1",
            "/api/search?limit=1",
            "/api/user",
        ]
        assert output.err == ""
    else:
        with pytest.raises(SystemExit) as error:
            exec(script, {})
        assert error.value.code == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert output.err == "Grafana authentication probe failed\n"
    assert PASSWORD not in output.out + output.err
