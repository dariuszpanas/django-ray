"""Focused contracts for the disposable monitoring qualification."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from qualification.monitoring import admission, probe, render, run

ROOT = Path(__file__).resolve().parents[2]
IMAGES = {
    name: f"example/{name}@sha256:" + "a" * 64
    for name in ("application", "ray", "postgres", "prometheus", "grafana", "importer")
}


@pytest.fixture(scope="module")
def shipped():
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.fail("The monitoring renderer contract requires kubectl kustomize")
    result = subprocess.run(
        [kubectl, "kustomize", str(ROOT / "k8s/overlays/kuberay-kind")],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return list(yaml.safe_load_all(result.stdout))


def test_render_preserves_shipped_discovery_auth_and_probes(shipped):
    documents = render.render(shipped, "monitoring-123", IMAGES)
    by_id = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
    original = {(doc["kind"], doc["metadata"]["name"]): doc for doc in shipped}
    for kind, name in (
        ("Role", "prometheus-django-ray"),
        ("Service", "django-web-svc"),
        ("Service", "grafana-svc"),
        ("Service", "prometheus-svc"),
    ):
        expected = copy.deepcopy(original[kind, name])
        expected["metadata"]["namespace"] = "monitoring-123"
        assert by_id[kind, name] == expected
    before = yaml.safe_load(original["ConfigMap", "prometheus-config"]["data"]["prometheus.yml"])
    after = yaml.safe_load(by_id["ConfigMap", "prometheus-config"]["data"]["prometheus.yml"])
    before["scrape_configs"][1]["kubernetes_sd_configs"][0]["namespaces"]["names"] = [
        "monitoring-123"
    ]
    assert before == after
    web = by_id["Deployment", "django-web"]["spec"]["template"]["spec"]
    source_web = original["Deployment", "django-web"]["spec"]["template"]["spec"]
    for key in ("readinessProbe", "livenessProbe"):
        assert web["containers"][0][key] == source_web["containers"][0][key]
    assert all(
        doc["kind"] not in {"Secret", "PersistentVolumeClaim", "Ingress"} for doc in documents
    )
    assert render.config_digest(documents) == render.config_digest(copy.deepcopy(documents))


def test_render_has_fixed_resources_no_foreign_storage_and_generic_ray(shipped):
    documents = render.render(shipped, "monitoring-123", IMAGES)
    pods = []
    for doc in documents:
        if doc["kind"] == "Deployment":
            pods.append(doc["spec"]["template"]["spec"])
        elif doc["kind"] == "RayCluster":
            spec = doc["spec"]
            assert spec["workerGroupSpecs"][0]["replicas"] == 1
            for group in (spec["headGroupSpec"], *spec["workerGroupSpecs"]):
                assert group["rayStartParams"]["num-cpus"] == "1"
                pods.append(group["template"]["spec"])
    cpu = memory = 0
    for pod in pods:
        assert not pod.get("hostNetwork")
        for volume in pod.get("volumes", []):
            assert "hostPath" not in volume and "persistentVolumeClaim" not in volume
            if "emptyDir" in volume:
                assert "sizeLimit" in volume["emptyDir"]
        for container in pod["containers"]:
            assert "@sha256:" in container["image"]
            limits = container["resources"]["limits"]
            cpu += int(limits["cpu"].removesuffix("m"))
            memory += int(limits["memory"].removesuffix("Mi"))
    assert cpu + 100 == 2850  # separately installed operator
    assert memory + 512 == 9344


@pytest.mark.parametrize("bad", ["default", "monitoring-x", "monitoring-1/other"])
def test_render_rejects_unowned_namespace(shipped, bad):
    with pytest.raises(ValueError):
        render.render(shipped, bad, IMAGES)


def test_render_refuses_missing_or_duplicate_source_resources(shipped):
    selected = next(doc for doc in shipped if doc["kind"] == "RayCluster")
    with pytest.raises(ValueError):
        render.render([doc for doc in shipped if doc is not selected], "monitoring-1", IMAGES)
    with pytest.raises(ValueError):
        render.render([*shipped, selected], "monitoring-1", IMAGES)
    with pytest.raises(ValueError):
        render.render(shipped, "monitoring-1", {**IMAGES, "grafana": "grafana/grafana:latest"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("hosted", False),
        ("linux", False),
        ("cpus", 3),
        ("total", 14 * admission.GIB),
        ("available", 12 * admission.GIB),
        ("disk", 19 * admission.GIB),
    ],
)
def test_admission_fails_before_capacity_allocation(field, value):
    arguments = {
        "hosted": True,
        "linux": True,
        "cpus": 4,
        "total": 16 * admission.GIB,
        "available": 14 * admission.GIB,
        "disk": 30 * admission.GIB,
    }
    receipt = admission.admit(**arguments)
    assert receipt["kind_system_cpu_headroom_millicores"] == 400
    with pytest.raises(ValueError):
        admission.admit(**{**arguments, field: value})


def target_payload():
    return {
        "status": "success",
        "data": {
            "activeTargets": [
                {"labels": {"job": job}, "health": "up"} for job in probe.EXPECTED_JOBS
            ]
        },
    }


@pytest.mark.parametrize("change", ["missing", "extra", "down", "obsolete", "duplicate"])
def test_target_contract_rejects_incomplete_or_ambiguous_inventory(change):
    payload = target_payload()
    assert probe.targets(payload) == probe.EXPECTED_JOBS
    active = payload["data"]["activeTargets"]
    if change == "missing":
        active.pop()
    elif change == "extra":
        active.append(copy.deepcopy(active[0]))
    elif change == "down":
        active[0]["health"] = "down"
    else:
        active[0]["labels"]["job"] = "django-ray-worker" if change == "obsolete" else "ray-head"
    with pytest.raises(ValueError):
        probe.targets(payload)


@pytest.mark.parametrize("anonymous_search", [401, 200])
def test_probe_real_flow_checks_dashboards_queries_and_separate_tokens(
    monkeypatch, anonymous_search, settings
):
    from types import SimpleNamespace

    from testproject.api_boundary import ApiTokenAuth, MetricsTokenAuth

    settings.DJANGO_API_ENABLED = True
    settings.DJANGO_API_TOKEN = "api-secret"
    settings.DJANGO_METRICS_TOKEN = "metrics-secret"
    for key, value in {
        "DJANGO_API_TOKEN": "api-secret",
        "DJANGO_METRICS_TOKEN": "metrics-secret",
        "GF_SECURITY_ADMIN_USER": "admin",
        "GF_SECURITY_ADMIN_PASSWORD": "grafana-secret",
    }.items():
        monkeypatch.setenv(key, value)
    visited = []

    def request(url, *, deadline, token=None, host=None):
        visited.append((url, token, host))
        if url.endswith(("/api/health", "/api/readyz")) and "django-web" in url:
            return 200, b'{"status":"healthy"}'
        if url.endswith("/api/livez"):
            return 200, b'{"status":"alive"}'
        if url.endswith("/api/metrics"):
            identity = (
                MetricsTokenAuth().authenticate(
                    SimpleNamespace(path_info="/api/metrics", method="GET"),
                    token.removeprefix("Bearer "),
                )
                if token
                else None
            )
            return (
                (200, b"django_ray_tasks_by_execution_protocol_total") if identity else (401, b"{}")
            )
        if url.endswith("/api/executions"):
            identity = ApiTokenAuth().authenticate(
                SimpleNamespace(path_info="/api/executions", method="GET"),
                token.removeprefix("Bearer "),
            )
            return (200 if identity else 401), b"{}"
        if url.endswith("/api/search?limit=1"):
            return anonymous_search, b"[]"
        if url.endswith("/api/user"):
            return (
                (200, b'{"login":"admin","isGrafanaAdmin":true}')
                if token == "Basic YWRtaW46Z3JhZmFuYS1zZWNyZXQ="
                else (401, b"{}")
            )
        if "/dashboards/uid/" in url:
            return 200, json.dumps(
                {"dashboard": {"uid": url.rsplit("/", 1)[-1], "panels": [{"type": "graph"}]}}
            ).encode()
        if url.endswith("/api/datasources/name/Prometheus"):
            return 200, b'{"uid":"fixed-uid","type":"prometheus"}'
        if url.endswith("/api/health"):
            return 200, b'{"version":"12.4.12"}'
        if url.endswith("/api/v1/status/buildinfo"):
            return 200, b'{"data":{"version":"3.13.4"}}'
        if url.endswith("/api/v1/targets?state=active"):
            return 200, json.dumps(target_payload()).encode()
        return 200, json.dumps(
            {
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {"job": job}, "value": [1, "1"]} for job in probe.EXPECTED_JOBS
                    ]
                },
            }
        ).encode()

    monkeypatch.setattr(probe, "request", request)
    if anonymous_search == 200:
        with pytest.raises(ValueError, match="denied-auth"):
            probe.observe(60)
        return
    receipt = probe.observe(60)
    assert receipt["status"] == "passed"
    assert receipt["dashboards"] == ["ray-cluster", "rayDefaultDashboard"]
    assert any("/proxy/uid/fixed-uid/" in url for url, _, _ in visited)
    assert ("http://django-web-svc/api/metrics", "Bearer api-secret", None) in visited
    assert ("http://django-web-svc/api/executions", "Bearer metrics-secret", None) in visited
    assert "secret" not in json.dumps(receipt)


def test_probe_failure_receipt_never_prints_raw_error(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "argv", ["probe", "10"])
    monkeypatch.setattr(
        probe, "observe", lambda seconds: (_ for _ in ()).throw(ValueError("secret-url-token-body"))
    )
    assert probe.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "failure": "monitoring_observation",
    }


def test_command_rejects_oversized_materialized_output():
    import sys

    with pytest.raises(RuntimeError, match="Monitoring command failed"):
        run.command([sys.executable, "-c", f"print('x'*{run.MAX_COMMAND_BYTES + 1})"], seconds=10)


def test_workflow_requires_checkpoint_admission_limits_and_cleanup():
    workflow = yaml.safe_load((ROOT / ".github/workflows/monitoring-qualification.yml").read_text())
    job = workflow["jobs"]["monitoring"]
    steps = job["steps"]
    assert job["runs-on"] == "ubuntu-latest"
    names = [step.get("name", "") for step in steps]
    assert names.index("Require the exact-source Linux CI checkpoint") < names.index(
        "Recheck capacity and create bounded Kind"
    )
    create = next(
        step for step in steps if step.get("name") == "Recheck capacity and create bounded Kind"
    )
    assert "--cpus=3.25 --memory=12g --memory-swap=12g --pids-limit=4096" in create["run"]
    assert "limits['NanoCpus']" in create["run"]
    cleanup = next(
        step
        for step in steps
        if step.get("name") == "Remove only this job's disposable infrastructure"
    )
    assert cleanup["if"] == "always()" and cleanup["timeout-minutes"] == 5
    assert "python -m qualification.monitoring.cleanup" in cleanup["run"]


@pytest.mark.parametrize(
    "failed_name", ["monitoring-qualification-control-plane", "monitoring-registry"]
)
def test_outer_cleanup_attempts_all_objects_after_removal_failure(monkeypatch, failed_name):
    from qualification.monitoring import cleanup

    remaining = {
        "monitoring-qualification-control-plane",
        "monitoring-registry",
        "monitoring-1-browser",
    }
    attempted = []

    def command(argv, *, seconds):
        assert 0 < seconds <= 180
        if argv[:2] == ["docker", "ps"]:
            return "owned-id" if argv[-1][6:-1] in remaining else ""
        if argv[:2] == ["docker", "inspect"]:
            return (
                "monitoring-qualification" if argv[-1].endswith("control-plane") else "monitoring-1"
            )
        name = "monitoring-qualification-control-plane" if argv[0] == "kind" else argv[-1]
        attempted.append(name)
        if name == failed_name:
            raise RuntimeError("not retained")
        remaining.remove(name)
        return ""

    monkeypatch.setattr(cleanup, "command", command)
    receipt = cleanup.cleanup("1")
    assert attempted == [
        "monitoring-qualification-control-plane",
        "monitoring-registry",
        "monitoring-1-browser",
    ]
    assert receipt["status"] == "failed" and receipt["browser_removed"] is True
    assert len(receipt["failed_components"]) == 1
    assert "not retained" not in json.dumps(receipt)


def test_outer_cleanup_does_not_remove_foreign_browser_and_still_verifies_absence(monkeypatch):
    from qualification.monitoring import cleanup

    calls = []

    def command(argv, *, seconds):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return "foreign" if "browser" in argv[-1] else ""
        assert argv[:2] == ["docker", "inspect"]
        return "other-run"

    monkeypatch.setattr(cleanup, "command", command)
    receipt = cleanup.cleanup("1")
    assert receipt == {
        "status": "failed",
        "failed_components": ["browser"],
        "disposable_cluster_removed": True,
        "registry_removed": True,
        "browser_removed": False,
    }
    assert (
        len([call for call in calls if call[:2] == ["docker", "ps"] and "browser" in call[-1]]) == 2
    )


def test_effective_capacity_respects_inherited_cgroup_limits(tmp_path):
    (tmp_path / "cpu.max").write_text("350000 100000")
    (tmp_path / "memory.max").write_text(str(14 * admission.GIB))
    (tmp_path / "memory.current").write_text(str(2 * admission.GIB))
    assert admission.effective_capacity(8, 32 * admission.GIB, 30 * admission.GIB, tmp_path) == (
        3.5,
        14 * admission.GIB,
        12 * admission.GIB,
    )
    (tmp_path / "cpu.max").write_text("max 100000")
    (tmp_path / "memory.max").write_text("max")
    assert admission.effective_capacity(4, 16 * admission.GIB, 13 * admission.GIB, tmp_path) == (
        4,
        16 * admission.GIB,
        13 * admission.GIB,
    )


def test_resource_drift_and_unbounded_injected_init_fail_closed(shipped):
    documents = render.render(shipped, "monitoring-123", IMAGES)
    assert documents[0]["kind"] == "ResourceQuota"
    assert render.validate_resources(documents) == {
        "cpu_millicores": 2750,
        "memory_mib": 8832,
        "pods": 6,
    }
    web = next(
        doc
        for doc in documents
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "django-web"
    )["spec"]["template"]["spec"]
    web["initContainers"].append({"name": "unbounded"})
    with pytest.raises(KeyError):
        render.validate_resources(documents)
    web["initContainers"][-1]["resources"] = render.resources("1000m", "2Gi")
    with pytest.raises(ValueError):
        render.validate_resources(documents)


@pytest.mark.parametrize(
    "owner,uid,expected",
    [
        ("mine", "original", True),
        ("mine", None, True),
        ("other", "original", False),
        ("mine", "replacement", False),
    ],
)
def test_namespace_cleanup_checks_owner_and_uid(owner, uid, expected):
    calls = []

    class Kube:
        def call(self, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return json.dumps(
                    {"metadata": {"uid": "original", "labels": {run.OWNER_LABEL: owner}}}
                )
            return ""

    assert run.cleanup_namespace(Kube(), "monitoring-1", "mine", uid) is expected
    assert any(args[0] == "delete" for args in calls) is expected


def test_forward_cleanup_never_signals_after_losing_waitable_identity(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from qualification.monitoring import host_browser

    for name in ("P_PID", "WEXITED", "WNOHANG", "WNOWAIT"):
        monkeypatch.setattr(host_browser.os, name, 1, raising=False)
    monkeypatch.setattr(
        host_browser.os, "waitid", Mock(side_effect=ChildProcessError), raising=False
    )
    kill = Mock()
    monkeypatch.setattr(host_browser.os, "killpg", kill, raising=False)
    wait = Mock()
    with pytest.raises(ChildProcessError):
        host_browser.stop_forward(SimpleNamespace(pid=123, wait=wait))
    kill.assert_not_called()
    wait.assert_not_called()


def test_browser_reserves_cleanup_inside_shared_observation_deadline(monkeypatch):
    from unittest.mock import Mock

    from qualification.monitoring import host_browser

    monkeypatch.setattr(host_browser.time, "monotonic", lambda: 100)
    command = Mock()
    monkeypatch.setattr(host_browser, "command", command)
    with pytest.raises(TimeoutError, match="Insufficient remaining"):
        host_browser.observe(
            namespace="monitoring-1", image=IMAGES["application"], credentials={}, deadline=249.9
        )
    command.assert_not_called()


def test_http_reader_bounds_body_and_never_follows_redirects(monkeypatch):
    class Response:
        code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            assert count == probe.MAX_RESPONSE_BYTES + 1
            return b"x" * count

    class Opener:
        def open(self, request, timeout):
            assert 0 < timeout <= 5
            assert request.get_header("Authorization") == "Bearer private"
            return Response()

    monkeypatch.setattr(probe, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="exceeds"):
        probe.request(
            "http://django-web-svc/api/metrics",
            deadline=probe.time.monotonic() + 10,
            token="Bearer private",
        )
    assert probe.NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere") is None


@pytest.mark.parametrize("mutate", ["restart", "image", "missing", "not_ready"])
def test_live_census_rejects_partial_or_restarted_source(mutate):
    groups = [
        [("django-web", "application")],
        [("postgres", "postgres")],
        [("prometheus", "prometheus")],
        [("grafana", "grafana")],
        [("ray-head", "ray"), ("dashboard-importer", "importer")],
        [("ray-worker", "ray")],
    ]
    payload = {
        "items": [
            {
                "spec": {
                    "containers": [{"name": name, "image": IMAGES[image]} for name, image in group]
                },
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": name,
                            "ready": True,
                            "restartCount": 0,
                            "imageID": "sha256:" + "a" * 64,
                        }
                        for name, _ in group
                    ],
                },
            }
            for group in groups
        ]
    }
    assert len(run.census(payload, IMAGES)) == 7
    pod = payload["items"][0]
    if mutate == "restart":
        pod["status"]["containerStatuses"][0]["restartCount"] = 1
    elif mutate == "image":
        pod["spec"]["containers"][0]["image"] = IMAGES["ray"]
    elif mutate == "missing":
        payload["items"].pop()
    else:
        pod["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(ValueError):
        run.census(payload, IMAGES)


@pytest.mark.parametrize("matching_query", [True, False])
def test_browser_observes_real_panel_query_and_closes_owned_browser(monkeypatch, matching_query):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from qualification.monitoring import browser

    monkeypatch.setenv("GF_SECURITY_ADMIN_USER", "admin")
    monkeypatch.setenv("GF_SECURITY_ADMIN_PASSWORD", "private")
    engine = MagicMock()
    context = engine.new_context.return_value
    page = context.new_page.return_value
    api = MagicMock()
    api.chromium.launch.return_value = engine
    manager = MagicMock()
    manager.__enter__.return_value = api
    monkeypatch.setattr(
        browser.importlib,
        "import_module",
        lambda name: SimpleNamespace(sync_playwright=lambda: manager),
    )
    response = SimpleNamespace(
        url=browser.BASE + "/api/ds/query",
        status=200,
        request=SimpleNamespace(
            method="POST",
            post_data_json={
                "queries": [
                    {
                        "refId": "A",
                        "expr": 'avg(ray_node_cpu_utilization{instance=~".*",SessionName=~".*",ray_io_cluster=~".*"})'
                        if matching_query
                        else "vector(1)",
                        "datasource": {"type": "prometheus"},
                    }
                ]
            },
        ),
        body=lambda: (
            b'{"results":{"A":{"frames":[{"schema":{"refId":"A"},"data":{"values":[[1],[0.5]]}}]}}}'
        ),
    )
    page.on.side_effect = lambda event, callback: callback(response)
    page.goto.return_value = SimpleNamespace(status=200)
    page.get_by_text.return_value.count.return_value = 0
    if not matching_query:
        ticks = iter([0, 11])
        monkeypatch.setattr(browser.time, "monotonic", lambda: next(ticks))
        with pytest.raises(ValueError, match="no observed numeric"):
            browser.render_panel()
        engine.close.assert_called_once()
        return
    receipt = browser.render_panel()
    assert receipt["status"] == "passed" and receipt["numeric_query_values"] == 1
    assert "panelId=41" in page.goto.call_args.args[0]
    engine.close.assert_called_once()
    assert "private" not in json.dumps(receipt)
    rejected = MagicMock()
    rejected.request.url = "https://unrelated.invalid/"
    context.route.call_args.args[1](rejected)
    rejected.abort.assert_called_once()
    rejected.continue_.assert_not_called()
