"""Execute the shipped sidecar script against bounded, synthetic HTTP replies."""

import json
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def importer(monkeypatch, tmp_path):
    resources = yaml.safe_load_all((ROOT / "k8s/base/monitoring.yaml").read_text(encoding="utf-8"))
    config = next(
        r for r in resources if r["metadata"]["name"] == "grafana-dashboard-import-script"
    )
    module = types.ModuleType("shipped_dashboard_importer")
    exec(
        compile(config["data"]["import_dashboards.py"], "shipped_importer.py", "exec"),
        module.__dict__,
    )
    module.DASHBOARDS_PATH = str(tmp_path)
    monkeypatch.setenv("GF_SECURITY_ADMIN_USER", "synthetic-admin")
    monkeypatch.setenv("GF_SECURITY_ADMIN_PASSWORD", "synthetic-password")
    return module


def dashboard(importer, name="ray_dashboard.json"):
    path = Path(importer.DASHBOARDS_PATH) / name
    path.write_text(json.dumps({"uid": "fixed-ray-dashboard", "panels": []}), encoding="utf-8")
    return path


class Response:
    def __init__(self, value, status=200):
        self.data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status = status
        self.bounds = []
        self.closed = False

    def read(self, size):
        self.bounds.append(size)
        return self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def test_import_requires_authenticated_matching_acceptance(importer, monkeypatch):
    path = dashboard(importer)
    response = Response({"status": "success", "uid": "fixed-ray-dashboard"})
    request = Mock(return_value=response)
    monkeypatch.setattr(importer.urllib.request, "urlopen", request)
    assert importer.import_dashboard(path)
    sent = request.call_args.args[0]
    assert sent.get_method() == "POST"
    assert sent.full_url == "http://grafana-svc:3000/api/dashboards/db"
    assert sent.get_header("Authorization").startswith("Basic ")
    assert json.loads(sent.data) == {
        "dashboard": {"uid": "fixed-ray-dashboard", "panels": []},
        "overwrite": True,
        "folderUid": "",
    }
    assert request.call_args.kwargs == {"timeout": 10}
    assert response.bounds == [importer.MAX_RESPONSE_BYTES + 1]
    assert response.closed


@pytest.mark.parametrize(
    "value,status",
    [
        ({"status": "success", "uid": "other"}, 200),
        ({"uid": "fixed-ray-dashboard"}, 200),
        ({"status": "failed", "uid": "fixed-ray-dashboard"}, 200),
        ({"status": "success", "uid": "fixed-ray-dashboard"}, 202),
        ([], 200),
        (b"invalid json", 200),
        (b"x" * (64 * 1024 + 1), 200),
    ],
    ids=["wrong_uid", "no_status", "failed", "wrong_http", "list", "invalid_json", "oversized"],
)
def test_import_refuses_unconfirmed_or_oversized_reply(importer, monkeypatch, value, status):
    response = Response(value, status)
    monkeypatch.setattr(importer.urllib.request, "urlopen", Mock(return_value=response))
    assert not importer.import_dashboard(dashboard(importer))
    assert response.closed


@pytest.mark.parametrize(
    "value",
    [b"invalid", b"[]", b"{}", b"x" * (2 * 1024 * 1024 + 1)],
    ids=["invalid_json", "list", "no_uid", "oversized"],
)
def test_invalid_files_fail_before_http(importer, monkeypatch, value):
    path = dashboard(importer)
    path.write_bytes(value)
    request = Mock(side_effect=AssertionError("unexpected HTTP"))
    monkeypatch.setattr(importer.urllib.request, "urlopen", request)
    assert not importer.import_dashboard(path)
    request.assert_not_called()


@pytest.mark.parametrize("once", [True, False])
def test_one_shot_exits_and_default_sidecar_remains_alive(importer, monkeypatch, once):
    dashboard(importer)
    monkeypatch.setattr(importer, "wait_for_grafana", lambda: True)
    monkeypatch.setattr(importer, "wait_for_dashboards", lambda: True)
    imported = Mock(return_value=True)
    monkeypatch.setattr(importer, "import_dashboard", imported)

    class SidecarAlive(BaseException):
        pass

    sleep = Mock(side_effect=SidecarAlive)
    monkeypatch.setattr(importer.time, "sleep", sleep)
    if once:
        assert importer.main(["--once"]) == 0
        sleep.assert_not_called()
    else:
        with pytest.raises(SidecarAlive):
            importer.main([])
        sleep.assert_called_once_with(3600)
    imported.assert_called_once()


@pytest.mark.parametrize("failure", ["grafana", "discovery", "empty", "too_many", "import"])
def test_failed_one_shot_never_reports_success_or_enters_keepalive(importer, monkeypatch, failure):
    path = dashboard(importer)
    monkeypatch.setattr(importer, "wait_for_grafana", lambda: failure != "grafana")
    monkeypatch.setattr(importer, "wait_for_dashboards", lambda: failure != "discovery")
    monkeypatch.setattr(importer, "import_dashboard", lambda _: failure != "import")
    monkeypatch.setattr(importer.time, "sleep", Mock(side_effect=AssertionError("keepalive")))
    if failure == "empty":
        path.unlink()
    if failure == "too_many":
        monkeypatch.setattr(importer.glob, "glob", lambda _: [str(path)] * 33)
    assert importer.main(["--once"]) == 1


def test_network_failure_does_not_print_response_or_credentials(importer, monkeypatch, capsys):
    path = dashboard(importer)
    monkeypatch.setattr(
        importer.urllib.request,
        "urlopen",
        Mock(side_effect=TimeoutError("synthetic-password confidential response")),
    )
    assert not importer.import_dashboard(path)
    text = capsys.readouterr().out
    assert "synthetic-password" not in text
    assert "confidential response" not in text


def test_readiness_closes_its_response(importer, monkeypatch):
    response = Response({"database": "ok"})
    request = Mock(return_value=response)
    monkeypatch.setattr(importer.urllib.request, "urlopen", request)
    assert importer.wait_for_grafana()
    assert response.closed
    assert request.call_args.kwargs == {"timeout": 5}
    assert response.bounds == []


def test_readiness_and_discovery_stop_after_finite_retries(importer, monkeypatch):
    request = Mock(side_effect=TimeoutError("unavailable"))
    sleep = Mock()
    monkeypatch.setattr(importer.urllib.request, "urlopen", request)
    monkeypatch.setattr(importer.time, "sleep", sleep)
    assert not importer.wait_for_grafana()
    assert request.call_count == importer.MAX_RETRIES
    assert sleep.call_count == importer.MAX_RETRIES
    sleep.reset_mock()
    assert not importer.wait_for_dashboards()
    assert sleep.call_count == importer.MAX_RETRIES
