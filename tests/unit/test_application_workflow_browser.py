"""Resource-free browser packet and isolation checks; Linux proves rendering."""

import hashlib
import http.client
import io
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
        json.dumps(
            {
                "attempts_rendered": 1,
                "javascript_errors": 0,
                "policy": "full",
                "status": "passed",
                "admin_assets": browser.asset_identities(browser.source_admin_assets()),
            },
            sort_keys=True,
        ).encode(),
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
@pytest.mark.parametrize("asset_failure", [None, "missing", "network", "abort"])
def test_rendered_recovery_preserves_verified_unstarted_nodes(
    monkeypatch, nodes, edges, pending, mismatch, asset_failure, capsys
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
    context = api.chromium.launch.return_value.new_context.return_value
    context.new_page.return_value = page
    assets = browser.source_admin_assets()

    def get_page(*_args, **_kwargs):
        handler = context.route.call_args.args[1]
        for name in (
            browser.ADMIN_ASSETS[:1] if asset_failure == "missing" else browser.ADMIN_ASSETS
        ):
            route = asset_route(name)
            if asset_failure == "abort":
                route.abort.side_effect = ValueError("PRIVATE_ABORT_ERROR")
            handler(route)
        return Mock(status=200)

    page.goto.side_effect = get_page

    def read(_self, path, **_kwargs):
        if asset_failure in {"network", "abort"}:
            raise ValueError("PRIVATE_NETWORK_ERROR")
        return 200, assets[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(browser.ApplicationHttp, "__call__", read)
    playwright = MagicMock()
    playwright.sync_playwright.return_value.__enter__.return_value = api
    monkeypatch.setattr(browser.importlib, "import_module", Mock(return_value=playwright))
    if mismatch or asset_failure:
        with pytest.raises(ValueError):
            browser.render(candidate)
    else:
        assert browser.render(candidate)["status"] == "passed"
    captured = capsys.readouterr()
    assert "PRIVATE_" not in captured.out + captured.err


def asset_route(name="task_live.js", **changes):
    request = {
        "url": f"http://django-web:8000/static/django_ray/admin/{name}",
        "method": "GET",
        "resource_type": "script",
        **changes,
    }
    return Mock(request=Mock(**request))


@pytest.mark.parametrize("size,accepted", [(0, False), (65536, True), (65537, False)])
def test_source_assets_have_an_exact_raw_byte_limit(tmp_path, monkeypatch, size, accepted):
    monkeypatch.setattr(browser, "ASSET_ROOT", tmp_path)
    for name in browser.ADMIN_ASSETS:
        (tmp_path / name).write_bytes(b"\xff" * size)
    if not accepted:
        with pytest.raises(ValueError, match="bound"):
            browser.source_admin_assets()
    else:
        assert browser.source_admin_assets() == dict.fromkeys(browser.ADMIN_ASSETS, b"\xff" * size)


@pytest.mark.parametrize("name", browser.ADMIN_ASSETS)
@pytest.mark.parametrize("hashed", [False, True])
def test_asset_delivery_uses_only_the_verified_live_bytes(monkeypatch, name, hashed):
    body = b"// exact source\r\n\xff"
    assets = {name: body}
    requested = []

    def read(_self, path, **kwargs):
        requested.append((path, kwargs))
        return 200, body

    monkeypatch.setattr(browser.ApplicationHttp, "__call__", read)
    filename = name.replace(".js", ".012345abcdef.js") if hashed else name
    route = asset_route(filename)
    verified = set()
    request = ApplicationHttp("http://django-web:8000", request_timeout=5)
    assert browser.fulfill_admin_asset(route, request, assets, verified)
    assert verified == {name}
    route.fulfill.assert_called_once_with(
        status=200, content_type=browser.ADMIN_CONTENT_TYPE, body=body
    )
    assert requested == [
        (
            f"/static/django_ray/admin/{filename}",
            {
                "method": "GET",
                "headers": {"Accept-Encoding": "identity"},
                "response_limit": len(body),
                "required_response_headers": {
                    "Content-Encoding": None,
                    "Content-Type": browser.ADMIN_CONTENT_TYPE,
                },
                "required_media_types": frozenset({"text/javascript", "application/javascript"}),
            },
        )
    ]
    assert request.request_timeout == 5
    assert request.retry_read_disconnects is False


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://other:8000/static/django_ray/admin/task_live.js"},
        {"url": "http://django-web:8000/static/django_ray/admin/task_live.js?other=1"},
        {"url": "http://django-web:8000/static/django_ray/admin/task_live.js#other"},
        {"method": "POST"},
        {"resource_type": "fetch"},
    ],
)
def test_asset_identity_refuses_ambiguous_or_non_read_only_routes(changes, monkeypatch):
    monkeypatch.setattr(
        browser.ApplicationHttp, "__call__", Mock(side_effect=AssertionError("network"))
    )
    route = asset_route(**changes)
    with pytest.raises(ValueError, match="fixed contract"):
        browser.fulfill_admin_asset(
            route, ApplicationHttp("http://django-web:8000"), {"task_live.js": b"x"}, set()
        )
    route.fulfill.assert_not_called()


@pytest.mark.parametrize("status,body", [(302, b"x"), (404, b"x"), (200, b"y"), (200, b"xx")])
def test_wrong_live_asset_response_is_never_delivered(monkeypatch, status, body):
    monkeypatch.setattr(browser.ApplicationHttp, "__call__", lambda *_a, **_k: (status, body))
    route = asset_route()
    verified = set()
    with pytest.raises(ValueError, match="source bytes"):
        browser.fulfill_admin_asset(
            route, ApplicationHttp("http://django-web:8000"), {"task_live.js": b"x"}, verified
        )
    assert verified == set()
    route.fulfill.assert_not_called()


def test_duplicate_asset_cannot_replace_the_second_required_asset(monkeypatch):
    read = Mock(side_effect=AssertionError("duplicate must not read"))
    monkeypatch.setattr(browser.ApplicationHttp, "__call__", read)
    with pytest.raises(ValueError, match="fixed contract"):
        browser.fulfill_admin_asset(
            asset_route(),
            ApplicationHttp("http://django-web:8000"),
            {"task_live.js": b"x"},
            {"task_live.js"},
        )


@pytest.mark.parametrize("failure", ["missing", "digest", "length", "boolean", "extra"])
def test_parent_independently_refuses_incomplete_asset_comparison(monkeypatch, failure):
    result = {"status": "passed", "attempts_rendered": 1, "policy": "full", "javascript_errors": 0}
    if failure != "missing":
        result["admin_assets"] = browser.asset_identities(browser.source_admin_assets())
        if failure == "extra":
            result["admin_assets"]["other.js"] = result["admin_assets"]["task_live.js"]
        else:
            key, value = {
                "digest": ("sha256", "0" * 64),
                "length": ("bytes", 1),
                "boolean": ("bytes", True),
            }[failure]
            result["admin_assets"]["task_live.js"][key] = value
    output = json.dumps(result, sort_keys=True).encode()
    process = Mock(pid=1234, returncode=0)
    process.communicate.return_value = (output, None)
    monkeypatch.setattr(browser.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(browser.os, "killpg", Mock(), raising=False)
    monkeypatch.setattr(browser.signal, "SIGKILL", 9, raising=False)
    with pytest.raises(browser.BrowserObservationError):
        browser.observe_rendered_workflow(
            ApplicationHttp("http://django-web:8000"),
            execution_pk=12,
            policy="full",
            observations=[{"state": "SUCCEEDED", "counts": {"nodes": 8, "edges": 8}}],
            admin_cookie="sessionid=fixture",
        )


def test_asset_identities_retain_lengths_and_raw_byte_hashes():
    assert browser.asset_identities({"task_live.js": b"\xff\r\n"}) == {
        "task_live.js": {"bytes": 3, "sha256": hashlib.sha256(b"\xff\r\n").hexdigest()}
    }


@pytest.mark.parametrize(
    "mime,body", [("text/html", b"x"), (None, b"x"), (browser.ADMIN_CONTENT_TYPE, b"xx")]
)
def test_real_bounded_transport_refuses_wrong_media_or_oversized_asset(monkeypatch, mime, body):
    from qualification.application import run_api

    response = Mock(status=200)
    response.getheader.side_effect = lambda name, default=None: (
        mime if name == "Content-Type" else default
    )
    response.read.side_effect = lambda count: body[:count]
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(run_api.http.client, "HTTPConnection", Mock(return_value=connection))
    route = asset_route()
    with pytest.raises(run_api.ApplicationHttpError):
        browser.fulfill_admin_asset(
            route,
            ApplicationHttp("http://django-web:8000", request_timeout=5),
            {"task_live.js": b"x"},
            set(),
        )
    route.fulfill.assert_not_called()
    connection.close.assert_called_once()
    if mime == browser.ADMIN_CONTENT_TYPE:
        response.read.assert_called_once_with(2)


@pytest.mark.parametrize(
    "encoding,content_type,accepted",
    [
        (None, 'text/javascript; charset="utf-8"', True),
        ("gzip", 'text/javascript; charset="utf-8"', False),
        (None, 'text/javascript; charset="utf-16"', False),
        ("identity", 'text/javascript; charset="utf-8"', False),
    ],
)
def test_original_encoding_metadata_is_verified_before_browser_delivery(
    monkeypatch, encoding, content_type, accepted
):
    from qualification.application import run_api

    headers = f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: 1\r\n"
    if encoding is not None:
        headers += f"Content-Encoding: {encoding}\r\n"
    sock = Mock()
    sock.makefile.return_value = io.BytesIO(headers.encode() + b"\r\nx")
    response = http.client.HTTPResponse(sock)
    response.begin()
    response.read = Mock(wraps=response.read)
    connection = Mock()
    connection.getresponse.return_value = response
    connection.close.side_effect = response.close
    monkeypatch.setattr(run_api.http.client, "HTTPConnection", Mock(return_value=connection))
    route = asset_route()
    request = ApplicationHttp("http://django-web:8000", request_timeout=5)
    if accepted:
        assert browser.fulfill_admin_asset(route, request, {"task_live.js": b"x"}, set())
        route.fulfill.assert_called_once_with(status=200, content_type=content_type, body=b"x")
        response.read.assert_called_once_with(2)
    else:
        with pytest.raises(run_api.ApplicationHttpError):
            browser.fulfill_admin_asset(route, request, {"task_live.js": b"x"}, set())
        route.fulfill.assert_not_called()
        response.read.assert_not_called()
    connection.close.assert_called_once()


def test_unrelated_asset_keeps_existing_browser_routing():
    route = asset_route("other.js")
    assert not browser.fulfill_admin_asset(
        route, ApplicationHttp("http://django-web:8000"), {}, set()
    )
    route.fulfill.assert_not_called()
