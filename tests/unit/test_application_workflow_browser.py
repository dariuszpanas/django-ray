"""Resource-free browser packet and isolation checks; Linux proves rendering."""

import json
from copy import deepcopy
from unittest.mock import MagicMock, Mock

import pytest

from qualification.application import workflow_browser as browser
from qualification.application.run_api import ApplicationHttp


def packet():
    return {
        "origin": "http://django-web:8000",
        "execution_pk": 12,
        "cookie": "sessionid=fixture",
        "policy": "full",
        "attempts": [{"state": "SUCCEEDED", "nodes": 8, "edges": 8, "admin_status": "AVAILABLE"}],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "http://user:secret@django-web:8000"),
        ("origin", "http://django-web:8000/other"),
        ("execution_pk", True),
        ("execution_pk", 0),
        ("cookie", "sessionid=fixture; other=secret"),
        ("policy", "unknown"),
        ("attempts", []),
        ("attempts", [{"state": "SUCCEEDED", "nodes": 1, "edges": 0}] * 4),
    ],
)
def test_rejects_unbounded_or_ambiguous_browser_packets(field, value):
    candidate = packet()
    candidate[field] = value
    with pytest.raises(ValueError):
        browser.validate_packet(candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("nodes", True),
        ("nodes", 101),
        ("nodes", 0),
        ("edges", 257),
        ("edges", -1),
        ("state", "RUNNING"),
        ("pending_nodes", True),
        ("pending_nodes", -1),
        ("pending_nodes", 9),
        ("pending_nodes", 1),
    ],
)
def test_rejects_invalid_terminal_graph_observations(field, value):
    candidate = packet()
    candidate["attempts"][0][field] = value
    with pytest.raises(ValueError):
        browser.validate_packet(candidate)


@pytest.mark.parametrize("policy", ["full", "terminal_only", "disabled"])
def test_accepts_bounded_policy_observations(policy):
    candidate = packet()
    candidate["policy"] = policy
    if policy != "full":
        candidate["attempts"][0].update(nodes=0, edges=0, admin_status="UNAVAILABLE")
    original = deepcopy(candidate)
    browser.validate_packet(candidate)
    assert candidate == original


def test_browser_child_uses_private_stdin_deadline_and_owned_cleanup(monkeypatch):
    process = Mock(pid=1234, returncode=0)
    process.communicate.return_value = (
        b'{"attempts_rendered": 1, "javascript_errors": 0, "policy": "full", "status": "passed"}',
        None,
    )
    launch = Mock(return_value=process)
    cleanup = Mock()
    monkeypatch.setattr(browser.subprocess, "Popen", launch)
    monkeypatch.setattr(browser.os, "killpg", cleanup, raising=False)
    # SIGKILL is absent on Windows; this resource-free check runs there too.
    monkeypatch.setattr(browser.signal, "SIGKILL", 9, raising=False)
    result = browser.observe_rendered_workflow(
        ApplicationHttp("http://django-web:8000"),
        execution_pk=12,
        policy="full",
        observations=[
            {
                "state": "FAILED",
                "counts": {"nodes": 15, "edges": 20},
                "admin_contract": {"graph_pending_nodes": 7},
            }
        ],
        admin_cookie="sessionid=fixture",
    )
    assert result["attempts_rendered"] == 1
    assert launch.call_args.args[0] == [
        browser.BROWSER_PYTHON,
        "-m",
        "qualification.application.workflow_browser",
    ]
    assert launch.call_args.kwargs["start_new_session"] is True
    assert process.communicate.call_args.kwargs["timeout"] == 45
    assert b"sessionid=fixture" in process.communicate.call_args.args[0]
    assert json.loads(process.communicate.call_args.args[0])["attempts"][0]["pending_nodes"] == 7
    cleanup.assert_called_once_with(1234, 9)
    process.wait.assert_called_once_with(timeout=5)


def test_deadline_kills_browser_descendants(monkeypatch):
    process = Mock(pid=1234)
    process.communicate.side_effect = browser.subprocess.TimeoutExpired("browser", 45)
    monkeypatch.setattr(browser.subprocess, "Popen", Mock(return_value=process))
    cleanup = Mock()
    monkeypatch.setattr(browser.os, "killpg", cleanup, raising=False)
    monkeypatch.setattr(browser.signal, "SIGKILL", 9, raising=False)
    with pytest.raises(browser.subprocess.TimeoutExpired):
        browser.observe_rendered_workflow(
            ApplicationHttp("http://django-web:8000"),
            execution_pk=12,
            policy="full",
            observations=[{"state": "SUCCEEDED", "counts": {"nodes": 8, "edges": 8}}],
            admin_cookie="sessionid=fixture",
        )
    cleanup.assert_called_once_with(1234, 9)
    process.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize(
    "output,line",
    [
        (b'{"status":"failed","line":123}', 123),
        (b'{"status":"failed","line":true}', None),
        (b'{"status":"failed","line":10001}', None),
        (b"private browser exception", None),
    ],
)
def test_child_failure_retains_only_a_bounded_source_line(monkeypatch, output, line):
    process = Mock(pid=1234, returncode=1)
    process.communicate.return_value = (output, None)
    monkeypatch.setattr(browser.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(browser.os, "killpg", Mock(), raising=False)
    monkeypatch.setattr(browser.signal, "SIGKILL", 9, raising=False)
    with pytest.raises(browser.BrowserObservationError) as raised:
        browser.observe_rendered_workflow(
            ApplicationHttp("http://django-web:8000"),
            execution_pk=12,
            policy="full",
            observations=[{"state": "SUCCEEDED", "counts": {"nodes": 8, "edges": 8}}],
            admin_cookie="sessionid=fixture",
        )
    assert raised.value.line == line
    assert str(raised.value) == "Rendered Admin verification failed"


def test_terminal_only_check_accepts_the_actual_admin_message():
    from django_ray.admin import _WORKFLOW_PROGRESS_MESSAGES

    message = _WORKFLOW_PROGRESS_MESSAGES["TERMINAL_ONLY"]
    assert browser.terminal_only_explanation_visible("Workflow execution\n" + message)
    assert browser.terminal_only_explanation_visible(message.replace(" ", "\n"))
    for state in ("TERMINAL_ONLY_PENDING", "TERMINAL_ONLY_MISSING", "DISABLED"):
        assert not browser.terminal_only_explanation_visible(_WORKFLOW_PROGRESS_MESSAGES[state])


def test_browser_accepts_only_the_fixed_retained_display_limit_case():
    candidate = packet()
    candidate["attempts"][0].update(nodes=101, edges=100, admin_status="LIMIT_EXCEEDED")
    browser.validate_packet(candidate)
    candidate["attempts"][0]["nodes"] = 100
    with pytest.raises(ValueError, match="fixed API graph"):
        browser.validate_packet(candidate)


@pytest.mark.parametrize("nodes,edges,pending", [(2, 1, 1), (15, 20, 7), (21, 28, 0)])
@pytest.mark.parametrize("mismatch", [None, "pending", "running"])
def test_rendered_recovery_preserves_verified_unstarted_nodes(
    monkeypatch, nodes, edges, pending, mismatch
):
    candidate = packet()
    candidate["attempts"][0].update(
        state="FAILED" if pending else "SUCCEEDED",
        nodes=nodes,
        edges=edges,
        pending_nodes=pending,
    )
    graph = Mock()

    def locate(selector):
        if "PENDING" in selector:
            return Mock(count=Mock(return_value=pending + int(mismatch == "pending")))
        if "RUNNING" in selector:
            return Mock(count=Mock(return_value=int(mismatch == "running")))
        if "FAILED" in selector:
            return Mock(count=Mock(return_value=int(pending > 0)))
        if selector == ".django-ray-workflow-graph__node":
            return Mock(count=Mock(return_value=nodes))
        if selector == ".django-ray-workflow-graph__connector":
            return Mock(count=Mock(return_value=edges))
        return Mock(is_visible=Mock(return_value=True))

    graph.locator.side_effect = locate
    panel = Mock()
    panel.locator.return_value = graph
    page = Mock(url="http://django-web:8000/admin/django_ray/raytaskexecution/12/change/")
    page.goto.return_value.status = 200
    page.locator.return_value = panel
    api = MagicMock()
    api.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
    playwright = MagicMock()
    playwright.sync_playwright.return_value.__enter__.return_value = api
    monkeypatch.setattr(browser.importlib, "import_module", Mock(return_value=playwright))
    if mismatch:
        with pytest.raises(ValueError):
            browser.render(candidate)
    else:
        assert browser.render(candidate)["status"] == "passed"
