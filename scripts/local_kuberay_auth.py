"""Read-only authentication probes for the admitted local KubeRay gate.

Execute each generated script in a fresh Python process. Ray probes belong in
an authorized task-manager container; Grafana probes belong in its existing
dashboard-importer container. Credentials stay in their existing environments.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping

RAY_AUTH_RECEIPT_KEYS = frozenset(
    f"{service}_{check}"
    for service in ("dashboard", "jobs", "client", "gcs")
    for check in ("anonymous_denied", "invalid_denied", "authorized")
)
GRAFANA_AUTH_RECEIPT_KEYS = frozenset(
    {"anonymous_denied", "invalid_denied", "administrator_authenticated"}
)


def validate_auth_receipt(payload: object, *, surface: str) -> None:
    """Accept only a complete, successful, secret-free probe receipt."""
    if surface == "ray":
        keys = RAY_AUTH_RECEIPT_KEYS
    elif surface == "grafana":
        keys = GRAFANA_AUTH_RECEIPT_KEYS
    else:
        raise ValueError("unsupported authentication probe surface")
    if (
        not isinstance(payload, Mapping)
        or set(payload) != keys | {"schema_version"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or any(payload[key] is not True for key in keys)
    ):
        raise ValueError("authentication probe did not prove every required boundary")


_HTTP_HELPERS = """
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def http(url, authorization=None, *, read_body=True):
    headers = {} if authorization is None else {"Authorization": authorization}
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(url, headers=headers, method="GET")
    try:
        response = opener.open(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(4097) if read_body else b""
        if len(body) > 4096:
            raise ValueError("authentication response exceeded its limit")
        return response.code, body

def require(condition):
    if condition is not True:
        raise ValueError("authentication boundary was not proved")
"""


def ray_auth_probe_script() -> str:
    """Return a bounded, non-submitting HTTP and raw-gRPC Ray probe.

    Raw channels never consult Ray's token-loader singleton, token path, default
    home token file, or cached SDK connection. Explicit empty metadata therefore
    means anonymous even inside a manager that owns a valid token. No ray.init,
    task submission, actor creation, or cluster shutdown occurs.
    """
    return textwrap.dedent(_HTTP_HELPERS) + textwrap.dedent(
        """
        def main():
            import grpc
            from ray.core.generated import (
                gcs_service_pb2, gcs_service_pb2_grpc,
                ray_client_pb2, ray_client_pb2_grpc,
            )

            token = os.environ.get("RAY_AUTH_TOKEN", "")
            require(os.environ.get("RAY_AUTH_MODE") == "token" and len(token) >= 32)
            invalid = "django-ray-invalid-authentication-probe-token"
            require(invalid != token)
            receipt = {"schema_version": 1}
            for service, path in (("dashboard", "/api/version"), ("jobs", "/api/jobs/")):
                url = "http://ray-head-svc:8265" + path
                status, body = http(url)
                require(status == 401 and body == b"Unauthorized: Missing authentication token")
                receipt[service + "_anonymous_denied"] = True
                status, body = http(url, "Bearer " + invalid)
                require(status == 403 and body == b"Forbidden: Invalid authentication token")
                receipt[service + "_invalid_denied"] = True
                status, body = http(url, "Bearer " + token, read_body=service == "dashboard")
                require(status == 200)
                if service == "dashboard":
                    require(isinstance(json.loads(body), dict))
                receipt[service + "_authorized"] = True

            for service, address in (("client", "ray-head-svc:10001"), ("gcs", "ray-head-svc:6379")):
                for check, credential in (("anonymous_denied", None), ("invalid_denied", invalid), ("authorized", token)):
                    metadata = () if credential is None else (("authorization", "Bearer " + credential),)
                    with grpc.insecure_channel(address, options=(("grpc.max_receive_message_length", 65536),)) as channel:
                        try:
                            if service == "client":
                                result = ray_client_pb2_grpc.RayletDriverStub(channel).ClusterInfo(
                                    ray_client_pb2.ClusterInfoRequest(type=ray_client_pb2.ClusterInfoType.PING),
                                    metadata=metadata, timeout=5,
                                )
                            else:
                                result = gcs_service_pb2_grpc.NodeInfoGcsServiceStub(channel).GetClusterId(
                                    gcs_service_pb2.GetClusterIdRequest(), metadata=metadata, timeout=5,
                                )
                        except grpc.RpcError as error:
                            require(check != "authorized")
                            require(error.code() == grpc.StatusCode.UNAUTHENTICATED)
                            details = error.details()
                            if service == "gcs":
                                # WrongClusterID is also UNAUTHENTICATED and is not token denial.
                                require(isinstance(details, str) and details.startswith("InvalidAuthToken:"))
                            else:
                                require(details == "Invalid or missing authentication token")
                        else:
                            require(check == "authorized")
                            if service == "client":
                                require(json.loads(result.json) == {})
                            else:
                                require(result.status.code == 0 and bool(result.cluster_id))
                    receipt[service + "_" + check] = True
            print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))

        try:
            main()
        except Exception:
            print("Ray authentication probe failed", file=sys.stderr)
            raise SystemExit(1) from None
        """
    )


def grafana_auth_probe_script() -> str:
    """Return a stdlib probe using the importer's existing administrator login."""
    return textwrap.dedent(_HTTP_HELPERS) + textwrap.dedent(
        """
        def main():
            import base64

            username = os.environ.get("GF_SECURITY_ADMIN_USER", "")
            password = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "")
            require(bool(username) and len(password) >= 32 and ":" not in username)
            invalid = "django-ray-invalid-grafana-probe-password"
            require(invalid != password)
            def basic(value):
                return "Basic " + base64.b64encode((username + ":" + value).encode()).decode()

            url = "http://grafana-svc:3000"
            # Unlike /api/user, search also detects anonymous Viewer access.
            status, _ = http(url + "/api/search?limit=1")
            require(status == 401)
            status, _ = http(url + "/api/search?limit=1", basic(invalid))
            require(status == 401)
            status, body = http(url + "/api/user", basic(password))
            require(status == 200)
            user = json.loads(body)
            require(isinstance(user, dict) and user.get("login") == username and user.get("isGrafanaAdmin") is True)
            print(json.dumps({
                "schema_version": 1,
                "anonymous_denied": True,
                "invalid_denied": True,
                "administrator_authenticated": True,
            }, sort_keys=True, separators=(",", ":")))

        try:
            main()
        except Exception:
            print("Grafana authentication probe failed", file=sys.stderr)
            raise SystemExit(1) from None
        """
    )
