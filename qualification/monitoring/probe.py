"""Bounded HTTP observations from inside the owned web resource envelope."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 262144
EXPECTED_JOBS = {"django-ray": 1, "ray-head": 1, "ray-workers": 1}


class NoRedirect(HTTPRedirectHandler):
    """No credentials may follow a redirected request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(
    url: str, *, deadline: float, token: str | None = None, host: str | None = None
) -> tuple[int, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Monitoring observation deadline expired")
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = token
    if host is not None:
        headers["Host"] = host
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(Request(url, headers=headers), timeout=min(5.0, remaining))
    except HTTPError as error:
        response = error
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Monitoring response exceeds the receipt input bound")
        return response.code, body


def targets(payload: object) -> dict[str, int]:
    """Exact cardinality and health; no target errors/labels reach receipts."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("Monitoring target response is invalid")
    active = payload.get("data", {}).get("activeTargets")
    if not isinstance(active, list) or len(active) != 3:
        raise ValueError("Monitoring target cardinality differs")
    counts = dict.fromkeys(EXPECTED_JOBS, 0)
    for target in active:
        job = target.get("labels", {}).get("job")
        if job not in counts or target.get("health") != "up":
            raise ValueError("Monitoring target health differs")
        counts[job] += 1
    if counts != EXPECTED_JOBS:
        raise ValueError("Monitoring target ownership differs")
    return counts


def observe(seconds: float) -> dict:
    if not 0 < seconds <= 60:
        raise ValueError("Invalid HTTP observation budget")
    deadline = time.monotonic() + seconds
    base = "http://django-web-svc"
    for path, expected in (
        ("/api/health", "healthy"),
        ("/api/readyz", "healthy"),
        ("/api/livez", "alive"),
    ):
        status, body = request(base + path, deadline=deadline, host="django-ray.localhost")
        if status != 200 or json.loads(body).get("status") != expected:
            raise ValueError("Application probe failed")
    api = os.environ["DJANGO_API_TOKEN"]
    metrics = os.environ["DJANGO_METRICS_TOKEN"]
    if api == metrics:
        raise ValueError("Metrics and API credentials must differ")
    for token in (None, "Bearer invalid-monitoring-token"):
        if request(base + "/api/metrics", deadline=deadline, token=token)[0] != 401:
            raise ValueError("Metrics credential isolation failed")
    for token in (metrics, api):
        status, body = request(base + "/api/metrics", deadline=deadline, token="Bearer " + token)
        if status != 200 or b"django_ray_tasks_by_execution_protocol_total" not in body:
            raise ValueError("Authenticated protocol metrics are missing")
    if request(base + "/api/executions", deadline=deadline, token="Bearer " + metrics)[0] != 401:
        raise ValueError("Metrics credential authorizes execution access")
    credentials = (
        os.environ["GF_SECURITY_ADMIN_USER"] + ":" + os.environ["GF_SECURITY_ADMIN_PASSWORD"]
    )
    basic = "Basic " + base64.b64encode(credentials.encode()).decode()
    grafana = "http://grafana-svc:3000"
    for token in (None, "Basic " + base64.b64encode(b"invalid:invalid").decode()):
        if request(grafana + "/api/search?limit=1", deadline=deadline, token=token)[0] != 401:
            raise ValueError("Grafana denied-auth contract failed")
    status, body = request(grafana + "/api/user", deadline=deadline, token=basic)
    identity = json.loads(body)
    if (
        status != 200
        or identity.get("login") != os.environ["GF_SECURITY_ADMIN_USER"]
        or identity.get("isGrafanaAdmin") is not True
    ):
        raise ValueError("Grafana administrator authentication failed")
    for uid in ("ray-cluster", "rayDefaultDashboard"):
        while True:
            status, body = request(
                grafana + "/api/dashboards/uid/" + uid, deadline=deadline, token=basic
            )
            if status != 404:
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        dashboard = json.loads(body).get("dashboard", {})
        if status != 200 or dashboard.get("uid") != uid or not dashboard.get("panels"):
            raise ValueError("Shipped or imported Ray dashboard is unavailable")
    status, body = request(
        grafana + "/api/datasources/name/Prometheus", deadline=deadline, token=basic
    )
    datasource = json.loads(body)
    uid = datasource.get("uid")
    if (
        status != 200
        or datasource.get("type") != "prometheus"
        or not isinstance(uid, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid) is None
    ):
        raise ValueError("Shipped datasource is unavailable")
    proxy = grafana + "/api/datasources/proxy/uid/" + uid
    status, body = request(grafana + "/api/health", deadline=deadline)
    version = json.loads(body).get("version")
    if (
        status != 200
        or not isinstance(version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]{0,24})?", version) is None
    ):
        raise ValueError("Grafana version unavailable")
    status, body = request("http://prometheus-svc:9090/api/v1/status/buildinfo", deadline=deadline)
    prometheus_version = json.loads(body).get("data", {}).get("version")
    if (
        status != 200
        or not isinstance(prometheus_version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]{0,24})?", prometheus_version)
        is None
    ):
        raise ValueError("Prometheus version unavailable")
    while True:
        status, body = request(
            "http://prometheus-svc:9090/api/v1/targets?state=active", deadline=deadline
        )
        try:
            if status != 200:
                raise ValueError("Monitoring target response unavailable")
            counts = targets(json.loads(body))
            break
        except ValueError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Monitoring target deadline expired") from None
            time.sleep(min(2, remaining))
    status, body = request(proxy + "/api/v1/query?query=up", deadline=deadline, token=basic)
    query = json.loads(body)
    results = query.get("data", {}).get("result", [])
    query_counts = dict.fromkeys(EXPECTED_JOBS, 0)
    if status != 200 or query.get("status") != "success" or len(results) != 3:
        raise ValueError("Datasource query failed")
    for item in results:
        job = item.get("metric", {}).get("job")
        if job not in query_counts or item.get("value", [None, None])[1] != "1":
            raise ValueError("Datasource query health differs")
        query_counts[job] += 1
    if query_counts != EXPECTED_JOBS:
        raise ValueError("Datasource query ownership differs")
    status, body = request(
        proxy + "/api/v1/query?query=django_ray_tasks_by_execution_protocol_total",
        deadline=deadline,
        token=basic,
    )
    query = json.loads(body)
    if status != 200 or query.get("status") != "success" or not query.get("data", {}).get("result"):
        raise ValueError("Product metric query failed")
    return {
        "status": "passed",
        "targets": counts,
        "metrics_auth": "isolated",
        "grafana_auth": "401/401/200",
        "dashboards": ["ray-cluster", "rayDefaultDashboard"],
        "datasource_query": "passed",
        "probe_host": "django-ray.localhost",
        "probe_paths": ["/api/health", "/api/readyz", "/api/livez"],
        "prometheus_version": prometheus_version,
        "grafana_version": version,
    }


def main() -> int:
    """Print one finite receipt; never an exception, response or credential."""
    try:
        if sys.argv[1:] == ["dashboard-reset"]:
            basic = (
                "Basic "
                + base64.b64encode(
                    (
                        os.environ["GF_SECURITY_ADMIN_USER"]
                        + ":"
                        + os.environ["GF_SECURITY_ADMIN_PASSWORD"]
                    ).encode()
                ).decode()
            )
            deadline = time.monotonic() + 20
            provisioned = request(
                "http://grafana-svc:3000/api/dashboards/uid/ray-cluster",
                deadline=deadline,
                token=basic,
            )[0]
            imported = request(
                "http://grafana-svc:3000/api/dashboards/uid/rayDefaultDashboard",
                deadline=deadline,
                token=basic,
            )[0]
            if provisioned != 200 or imported != 404:
                raise ValueError("Grafana replacement did not demonstrate import loss")
            print(
                json.dumps(
                    {"status": "passed", "provisioned_restored": True, "imported_absent": True}
                )
            )
            return 0
        if sys.argv[1:] == ["wait-database"]:
            deadline = time.monotonic() + 120
            while True:
                try:
                    with socket.create_connection(("postgres-svc", 5432), timeout=2):
                        return 0
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Database startup deadline expired") from None
                    time.sleep(1)
        result = observe(float(sys.argv[1]))
    except Exception:
        print(json.dumps({"status": "failed", "failure": "monitoring_observation"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
