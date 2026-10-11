"""One real imported Grafana panel, inside a separately capped container."""

from __future__ import annotations

import base64
import importlib
import json
import math
import os
import re
import signal
import time

BASE = "http://127.0.0.1:30030"
PANEL_ID = 41
PANEL_TITLE = "Cluster Utilization"
CPU_EXPRESSION = r'avg\(ray_node_cpu_utilization\{instance=~"[^"\r\n]{0,256}",SessionName=~"[^"\r\n]{0,256}",ray_io_cluster=~"[^"\r\n]{0,256}"\}\)'


def cpu_query_values(response) -> list[float]:
    """Bind numeric data to the source-verified panel's CPU target/refId."""
    if (
        not response.url.startswith(BASE + "/api/ds/query?")
        and response.url != BASE + "/api/ds/query"
    ):
        return []
    if response.status != 200 or response.request.method != "POST":
        return []
    request = response.request.post_data_json
    matching = [
        query
        for query in request.get("queries", [])
        if query.get("refId") == "A"
        and re.fullmatch(CPU_EXPRESSION, query.get("expr", ""))
        and query.get("datasource", {}).get("type") == "prometheus"
    ]
    if len(matching) != 1:
        return []
    data = response.body()
    if len(data) > 262144:
        raise ValueError("Grafana panel query response exceeds bound")
    result = json.loads(data).get("results", {}).get("A", {})
    if result.get("error") or result.get("status", 200) != 200:
        return []
    values = []
    for frame in result.get("frames", []):
        if frame.get("schema", {}).get("refId") != "A":
            continue
        for column in frame.get("data", {}).get("values", [])[1:]:
            values.extend(
                value
                for value in column
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
    return values


def render_panel() -> dict:
    sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright

    query_values = []

    def observe(response):
        query_values.extend(cpu_query_values(response))

    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            credentials = (
                os.environ["GF_SECURITY_ADMIN_USER"]
                + ":"
                + os.environ["GF_SECURITY_ADMIN_PASSWORD"]
            )
            context = browser.new_context(
                extra_http_headers={
                    "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()
                }
            )
            context.route(
                "**/*",
                lambda route: (
                    route.continue_() if route.request.url.startswith(BASE + "/") else route.abort()
                ),
            )
            page = context.new_page()
            page.on("response", observe)
            response = page.goto(
                BASE
                + f"/d-solo/rayDefaultDashboard/ray-default-dashboard?orgId=1&panelId={PANEL_ID}&from=now-5m&to=now",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if response is None or response.status != 200:
                raise ValueError("Imported Grafana panel did not load")
            page.get_by_text(PANEL_TITLE, exact=True).first.wait_for(state="visible", timeout=30000)
            page.locator("canvas").first.wait_for(state="visible", timeout=30000)
            deadline = time.monotonic() + 10
            while not query_values and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            if not query_values:
                raise ValueError("Imported panel has no observed numeric query data")
            if page.get_by_text("Panel plugin not found", exact=False).count():
                raise ValueError("Imported panel plugin is unavailable")
            return {
                "status": "passed",
                "dashboard_uid": "rayDefaultDashboard",
                "panel_id": PANEL_ID,
                "panel_title": PANEL_TITLE,
                "visible_canvas": True,
                "numeric_query_values": len(query_values),
            }
        finally:
            browser.close()


def main() -> int:
    def expired(_signum, _frame):
        raise TimeoutError("Owned Grafana render deadline expired")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(90)
    try:
        result = render_panel()
    except Exception:
        result = {"status": "failed", "failure": "grafana_panel_render"}
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
