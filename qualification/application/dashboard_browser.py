"""Observe one real Core task through Admin and a prefixed dashboard proxy."""

from __future__ import annotations

import importlib
import json
import os
import re
import signal
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from qualification.application.dashboard_proxy import (
    DASHBOARD_BASE,
    DASHBOARD_ORIGIN,
    dashboard_proxy,
)
from qualification.application.run_api import ApplicationHttp
from qualification.application.workflow_browser import (
    BROWSER_PYTHON,
    MAX_PACKET_BYTES,
    BrowserObservationError,
)
from qualification.application.workflow_session import qualification_admin_session

EXPECTED = {
    "status": "passed",
    "anonymous_admin_denied": True,
    "admin_task_link_verified": True,
    "proxy_base_path_verified": True,
    "task_api_identity_verified": True,
    "task_detail_rendered": True,
    "javascript_errors": 0,
}


def validate_packet(packet):
    ApplicationHttp(packet["origin"])
    if type(packet["execution_pk"]) is not int or packet["execution_pk"] <= 0:
        raise ValueError("Invalid disposable execution identity")
    if not re.fullmatch(r"[0-9a-f]{8}", packet["job_id"]):
        raise ValueError("Invalid Ray job identity")
    if not re.fullmatch(r"[0-9a-f]{48}", packet["task_id"]):
        raise ValueError("Invalid Ray task identity")
    if not re.fullmatch(r"[A-Za-z0-9_]+=[A-Za-z0-9]+", packet["cookie"]):
        raise ValueError("Invalid disposable Admin session")


def validate_task_response(value, packet):
    if value.get("result") is not True:
        raise ValueError("Dashboard API did not report success")
    rows = value["data"]["result"]["result"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Dashboard must return exactly the selected task")
    row = rows[0]
    if (
        row.get("task_id") != packet["task_id"]
        or row.get("job_id") != packet["job_id"]
        or row.get("state") != "FINISHED"
        or type(row.get("attempt_number")) is not int
        or row["attempt_number"] != 0
    ):
        raise ValueError("Dashboard returned another task or outcome")


def observe_dashboard(request, task_id):
    from django.conf import settings

    from django_ray.models import RayTaskExecution

    if settings.RAY_DASHBOARD_URL != DASHBOARD_BASE:
        raise ValueError("Dashboard qualification requires its explicit proxy URL")
    row = RayTaskExecution.objects.get(task_id=task_id)
    job, task = row.ray_job_id.split(":", 1)
    host = request.hostname
    if ":" in host:
        host = f"[{host}]"
    port = f":{request.port}" if request.port is not None else ""
    with qualification_admin_session() as cookie, dashboard_proxy():
        packet = {
            "origin": f"{'https' if request.secure else 'http'}://{host}{port}",
            "execution_pk": row.pk,
            "job_id": job,
            "task_id": task,
            "cookie": cookie,
        }
        validate_packet(packet)
        encoded = json.dumps(packet).encode()
        if len(encoded) > MAX_PACKET_BYTES:
            raise ValueError("Dashboard browser packet exceeds its bound")
        process = subprocess.Popen(
            [BROWSER_PYTHON, "-m", "qualification.application.dashboard_browser"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(encoded, timeout=60)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        if process.returncode:
            line = None
            if len(output) <= 256:
                try:
                    candidate = json.loads(output).get("line")
                    if type(candidate) is int and 0 < candidate <= 10000:
                        line = candidate
                except (ValueError, AttributeError):
                    pass
            raise BrowserObservationError(line)
        if len(output) > 1024 or json.loads(output) != EXPECTED:
            raise BrowserObservationError()
    return {**EXPECTED, "session_and_proxy_removed": True}


def render(packet):
    validate_packet(packet)
    playwright = importlib.import_module("playwright.sync_api")
    origin = packet["origin"]
    admin_origin = urlsplit(origin)
    proxy_origin = urlsplit(DASHBOARD_ORIGIN)
    expected_link = f"{DASHBOARD_BASE}/#/jobs/{packet['job_id']}/tasks/{packet['task_id']}"
    errors = []
    observed = []
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            context = browser.new_context()

            def route_request(route):
                target = urlsplit(route.request.url)
                allowed = (target.scheme, target.netloc) == (
                    admin_origin.scheme,
                    admin_origin.netloc,
                ) or (
                    (target.scheme, target.netloc) == (proxy_origin.scheme, proxy_origin.netloc)
                    and target.path.startswith("/ray/")
                )
                if allowed and route.request.method == "GET":
                    route.continue_()
                else:
                    route.abort()

            def observe_response(response):
                target = urlsplit(response.url)
                if target.path != "/ray/api/v0/tasks":
                    return
                query = parse_qs(target.query)
                if query.get("filter_values") != [packet["task_id"]]:
                    return
                try:
                    body = response.body()
                    if response.status != 200 or len(body) > 256 * 1024:
                        raise ValueError("Dashboard task response is unavailable")
                    validate_task_response(json.loads(body), packet)
                    observed[:] = [True]
                except Exception:
                    observed[:] = [False]

            context.route("**/*", route_request)
            context.on("response", observe_response)
            context.on(
                "page",
                lambda page: page.on(
                    "pageerror", lambda _error: errors.append(True) if not errors else None
                ),
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            admin_url = (
                origin + f"/admin/django_ray/raytaskexecution/{packet['execution_pk']}/change/"
            )
            page.goto(admin_url, wait_until="domcontentloaded")
            if not page.url.startswith(origin + "/admin/login/"):
                raise ValueError("Anonymous Admin request was not denied")
            name, value = packet["cookie"].split("=", 1)
            context.add_cookies([{"name": name, "value": value, "url": origin, "httpOnly": True}])
            response = page.goto(admin_url, wait_until="domcontentloaded")
            if response is None or response.status != 200 or page.url != admin_url:
                raise ValueError("Authenticated Admin page did not load")
            link = page.get_by_role("link", name="[Open in Dashboard]", exact=True)
            if link.count() != 1 or link.get_attribute("href") != expected_link:
                raise ValueError("Admin dashboard link does not identify the actual task")
            with context.expect_page() as opened:
                link.click()
            dashboard = opened.value
            dashboard.set_default_timeout(20000)
            dashboard.wait_for_load_state("domcontentloaded")
            dashboard.get_by_text("FINISHED", exact=True).first.wait_for(state="visible")
            dashboard.get_by_text(packet["task_id"], exact=True).first.wait_for(state="visible")
            dashboard.get_by_text(packet["job_id"], exact=True).first.wait_for(state="visible")
            if dashboard.url != expected_link or observed != [True] or errors:
                raise ValueError("Dashboard did not render the verified task through the proxy")
            return EXPECTED.copy()
        finally:
            browser.close()


def main():
    try:
        payload = sys.stdin.buffer.read(MAX_PACKET_BYTES + 1)
        if len(payload) > MAX_PACKET_BYTES:
            raise ValueError("Dashboard packet is too large")
        result = render(json.loads(payload))
        sys.stdout.write(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        line = None
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                line = trace.tb_lineno
            trace = trace.tb_next
        sys.stdout.write(json.dumps({"status": "failed", "line": line}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
