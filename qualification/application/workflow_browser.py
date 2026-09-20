"""Bounded Chromium observation for the disposable Admin workload."""

from __future__ import annotations

import importlib
import json
import os
import re
import signal
import subprocess
import sys
from urllib.parse import urlsplit

from qualification.application.run_api import ApplicationHttp

BROWSER_PYTHON = "/opt/qualification-browser/bin/python"
MAX_PACKET_BYTES = 4096


class BrowserObservationError(ValueError):
    """Carry only a source line, never a browser exception or DOM value."""

    def __init__(self, line: int | None = None):
        super().__init__("Rendered Admin verification failed")
        self.line = line


def terminal_only_explanation_visible(text: str) -> bool:
    """Recognize the actual server explanation, without accepting missing detail."""
    expected = (
        "A terminal workflow summary is available; topology and node detail were "
        "omitted by the terminal-only reporting policy."
    )
    return expected in " ".join(text.split())


def validate_packet(packet: dict) -> None:
    """Validate the finite fixture contract before launching a browser."""
    ApplicationHttp(packet["origin"])
    if type(packet["execution_pk"]) is not int or packet["execution_pk"] < 1:
        raise ValueError("Browser observation requires an execution identity")
    if packet["policy"] not in {"full", "terminal_only", "disabled"}:
        raise ValueError("Browser observation requires a known policy")
    if not re.fullmatch(r"[A-Za-z0-9_]+=[A-Za-z0-9]+", packet["cookie"]):
        raise ValueError("Browser observation requires a disposable session")
    attempts = packet["attempts"]
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 3:
        raise ValueError("Browser observation requires bounded attempt history")
    for attempt in attempts:
        if attempt["state"] not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("Browser observation requires terminal attempts")
        status = attempt["admin_status"]
        expected_status = "AVAILABLE" if packet["policy"] == "full" else "UNAVAILABLE"
        if status not in {expected_status, "LIMIT_EXCEEDED"}:
            raise ValueError("Browser status contradicts reporting policy")
        if status == "LIMIT_EXCEEDED" and (
            packet["policy"] != "full" or attempt["nodes"] != 101 or attempt["edges"] != 100
        ):
            raise ValueError("Browser display-limit case must retain its fixed API graph")
        for key, maximum in (("nodes", 101 if status == "LIMIT_EXCEEDED" else 100), ("edges", 256)):
            count = attempt[key]
            if type(count) is not int or not 0 <= count <= maximum:
                raise ValueError("Browser observation exceeds graph bounds")
        if (packet["policy"] == "full") != (attempt["nodes"] > 0):
            raise ValueError("Browser graph count contradicts reporting policy")
        pending = attempt.get("pending_nodes", 0)
        if type(pending) is not int or not 0 <= pending <= attempt["nodes"]:
            raise ValueError("Browser pending count exceeds graph bounds")
        if pending and (attempt["state"] != "FAILED" or status != "AVAILABLE"):
            raise ValueError("Only available failed graphs may retain unstarted nodes")


def observe_rendered_workflow(
    request: ApplicationHttp,
    *,
    execution_pk: int,
    policy: str,
    observations: list[dict],
    admin_cookie: str,
) -> dict:
    """Run one isolated browser process; retain only fixed scalar evidence."""
    host = request.hostname
    if ":" in host:
        host = f"[{host}]"
    port = f":{request.port}" if request.port is not None else ""
    packet = {
        "origin": f"{'https' if request.secure else 'http'}://{host}{port}",
        "execution_pk": execution_pk,
        "policy": policy,
        "cookie": admin_cookie,
        "attempts": [
            {
                "state": item["state"],
                "nodes": (item["counts"] if "counts" in item else item["api_counts"])["nodes"],
                "edges": (item["counts"] if "counts" in item else item["api_counts"])["edges"],
                "pending_nodes": item.get("admin_contract", {}).get("graph_pending_nodes", 0),
                "admin_status": item.get(
                    "admin_status", "AVAILABLE" if policy == "full" else "UNAVAILABLE"
                ),
            }
            for item in observations
        ],
    }
    validate_packet(packet)
    encoded = json.dumps(packet).encode()
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError("Browser observation packet exceeds its bound")
    process = subprocess.Popen(
        [BROWSER_PYTHON, "-m", "qualification.application.workflow_browser"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(encoded, timeout=45)
    finally:
        # The child owns a fresh process group, including its browser processes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    expected = {
        "status": "passed",
        "attempts_rendered": len(observations),
        "policy": policy,
        "javascript_errors": 0,
    }
    if process.returncode != 0:
        line = None
        if len(output) <= 256:
            try:
                failure = json.loads(output)
                candidate = failure.get("line")
                if type(candidate) is int and 0 < candidate <= 10000:
                    line = candidate
            except (ValueError, AttributeError):
                pass
        raise BrowserObservationError(line)
    if output != json.dumps(expected, sort_keys=True).encode():
        raise BrowserObservationError()
    return expected


def render(packet: dict) -> dict:
    """Execute real shipped JavaScript and inspect its visible terminal state."""
    validate_packet(packet)
    playwright = importlib.import_module("playwright.sync_api")
    origin = packet["origin"]
    expected_origin = urlsplit(origin)
    errors = []
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            name, value = packet["cookie"].split("=", 1)
            context.add_cookies(
                [{"name": name, "value": value, "url": origin, "httpOnly": True, "sameSite": "Lax"}]
            )

            def route_request(route):
                target = urlsplit(route.request.url)
                if (target.scheme, target.netloc) == (
                    expected_origin.scheme,
                    expected_origin.netloc,
                ) and route.request.method == "GET":
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda _error: errors.append(True) if not errors else None)
            path = f"/admin/django_ray/raytaskexecution/{packet['execution_pk']}/change/"
            response = page.goto(origin + path, wait_until="domcontentloaded")
            if response is None or response.status != 200 or page.url != origin + path:
                raise ValueError("Admin page was not authenticated")
            panel = page.locator("#django-ray-workflow-diagnostics")
            panel.locator(":scope > summary").click()
            page.wait_for_function("""() => document.querySelector(
                '[data-workflow-diagnostics-status]')?.textContent ===
                'Workflow diagnostics loaded.'""")
            if packet["policy"] == "terminal_only":
                if panel.locator("[data-workflow-current-graph-panel]").count() != 0:
                    raise ValueError("Terminal-only reporting advertised a graph")
                if not terminal_only_explanation_visible(panel.inner_text()):
                    raise ValueError("Terminal-only explanation was not rendered")
            else:
                for number, expected in enumerate(packet["attempts"], 1):
                    graph = panel.locator(f'[data-workflow-graph-attempt="{number}"]')
                    graph.locator(":scope > summary").click()
                    state = "ready" if expected["admin_status"] == "AVAILABLE" else "unavailable"
                    page.wait_for_function(
                        """({number, state}) => document.querySelector(
                        `[data-workflow-graph-attempt="${number}"]`)
                        ?.dataset.hydrationState === state""",
                        arg={"number": number, "state": state},
                    )
                    nodes = graph.locator(".django-ray-workflow-graph__node")
                    edges = graph.locator(".django-ray-workflow-graph__connector")
                    expected_nodes = expected["nodes"] if state == "ready" else 0
                    expected_edges = expected["edges"] if state == "ready" else 0
                    if nodes.count() != expected_nodes or edges.count() != expected_edges:
                        raise ValueError("Rendered graph counts disagree with API")
                    if state == "ready":
                        if not graph.locator(".django-ray-workflow-graph__content").is_visible():
                            raise ValueError("Rendered graph is hidden")
                        failed = graph.locator(
                            '.django-ray-workflow-graph__node[data-state="FAILED"]'
                        ).count()
                        if (failed > 0) != (expected["state"] == "FAILED"):
                            raise ValueError("Rendered failure state disagrees with API")
                        pending = graph.locator(
                            '.django-ray-workflow-graph__node[data-state="PENDING"]'
                        ).count()
                        if pending != expected.get("pending_nodes", 0):
                            raise ValueError("Rendered unstarted nodes disagree with API")
                        if graph.locator(
                            '.django-ray-workflow-graph__node[data-state="RUNNING"]'
                        ).count():
                            raise ValueError("Terminal graph contains running nodes")
                        # A path element without geometry does not prove visible edges.
                        page.wait_for_function(
                            """number => [...document.querySelectorAll(
                            `[data-workflow-graph-attempt="${number}"] .django-ray-workflow-graph__connector`
                        )].every(edge => Boolean(edge.getAttribute('d')))""",
                            arg=number,
                        )
                    elif expected["admin_status"] == "LIMIT_EXCEEDED":
                        if (
                            "LIMIT EXCEEDED" not in graph.inner_text()
                            or "paginated workflow API" not in graph.inner_text()
                        ):
                            raise ValueError("Admin display-limit guidance was not rendered")
                        if graph.locator(".django-ray-workflow-graph__content").is_visible():
                            raise ValueError("Over-limit graph content was displayed")
                    elif "disabled" not in graph.inner_text().lower():
                        raise ValueError("Disabled explanation was not rendered")
            if errors:
                raise ValueError("Admin JavaScript raised an error")
            return {
                "status": "passed",
                "attempts_rendered": len(packet["attempts"]),
                "policy": packet["policy"],
                "javascript_errors": 0,
            }
        finally:
            browser.close()


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(MAX_PACKET_BYTES + 1)
        if len(payload) > MAX_PACKET_BYTES:
            raise ValueError("Browser packet exceeds its bound")
        result = render(json.loads(payload))
        sys.stdout.write(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Never print browser exceptions, URLs, DOM content or session cookies.
        location = None
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                location = trace.tb_lineno
            trace = trace.tb_next
        sys.stdout.write(json.dumps({"status": "failed", "line": location}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
