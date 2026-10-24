"""Exercise the real host runner and auth adapter with owned transport responses."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qualification.monitoring import host_browser, run
from scripts.local_kuberay_auth import RAY_AUTH_RECEIPT_KEYS


@pytest.fixture
def observer(monkeypatch, tmp_path):
    state = SimpleNamespace(
        clock=100.0,
        replay_at=100.0,
        auth_seconds=1.0,
        replay_passes=True,
        auth_error=False,
        auth={"schema_version": 1, **dict.fromkeys(RAY_AUTH_RECEIPT_KEYS, True)},
        events=[],
        namespace=None,
        replaced=False,
        output=tmp_path / "evidence",
        kube=None,
        final_query_error=None,
        final_queries=[],
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("SOURCE_SHA", "source-head")
    monkeypatch.setattr(run.time, "monotonic", lambda: state.clock)

    def command(argv, *, seconds):
        assert seconds in (5, 30)
        if argv == ["git", "rev-parse", "HEAD", "HEAD^{tree}"]:
            return "source-head\nsource-tree\n"
        if argv == ["git", "diff", "--exit-code", "HEAD", "--"]:
            return ""
        assert argv == ["kubectl", "kustomize", "k8s/overlays/kuberay-kind"]
        return "[]"

    class Kubernetes:
        def __init__(self, namespace, deadline):
            assert namespace == "monitoring-123"
            assert deadline == 700  # Original start plus the unchanged 600-second window.
            self.deadline = deadline
            state.kube = self

        def get(self, resource):
            if resource.startswith("namespace/"):
                return state.namespace
            if resource != "pods":
                return {}
            if "browser" in state.events:
                state.final_queries.append(resource)
                if state.final_query_error is not None:
                    raise state.final_query_error
            pods = []
            for name in ("head", "worker", "grafana", "web", "postgres", "prometheus"):
                uid = name + ("-new" if name == "grafana" and state.replaced else "-old")
                pods.append(
                    {
                        "metadata": {
                            "name": "owned-" + name,
                            "uid": uid,
                            "labels": {
                                "app": "ray" if name in {"head", "worker"} else name,
                                "component": name,
                            },
                        },
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }
                )
            return {"items": pods}

        def call(self, *args, data=None, timeout=30):
            if args == ("create", "-f", "-"):
                state.namespace = json.loads(data)
                state.namespace["metadata"]["uid"] = "owned-namespace-uid"
                return ""
            if args == ("apply", "-f", "-"):
                return ""
            if args[:2] == ("get", "namespace"):
                return "" if state.namespace is None else json.dumps(state.namespace)
            if args[:2] == ("delete", "namespace"):
                assert args[2] == "monitoring-123"
                assert self.deadline == state.clock + 240  # Separate existing cleanup window.
                state.events.append("namespace_cleanup")
                state.namespace = None
                return ""
            if args[:2] == ("delete", "pod"):
                assert args[2] == "owned-grafana"
                state.replaced = True
                return ""
            if args[:2] == ("get", "pods"):
                return '{"items":[]}'
            assert args[0] == "exec"
            if args[1] == "owned-head":
                if args[3] == "dashboard-importer":
                    state.events.append("replay")
                    return ""
                if not args[-1].startswith("import signal\nsignal.alarm(65)\n"):
                    return '{"alive_nodes":2}'
                assert self.deadline == 700 and timeout == 70
                state.events.append("auth")
                state.clock += state.auth_seconds
                if state.auth_error:
                    raise RuntimeError("unbounded-private-error-canary")
                return json.dumps(state.auth)
            assert args[1] == "deployment/django-web"
            if args[-1] == "dashboard-reset":
                return '{"dashboard_absent":true}'
            if "replay" in state.events:
                state.events.append("replay_observed")
                state.clock = state.replay_at
                return json.dumps({"status": "passed" if state.replay_passes else "failed"})
            state.events.append("http")
            return '{"status":"passed"}'

    def browser(**kwargs):
        assert kwargs["deadline"] == 700
        assert kwargs["deadline"] - state.clock >= 150
        state.events.append("browser")
        return {"cleanup_confirmed": True}

    monkeypatch.setattr(run, "command", command)
    monkeypatch.setattr(run, "Kubernetes", Kubernetes)
    # These unrelated platform contracts have separate renderer/census tests.
    monkeypatch.setattr(run, "render", lambda *_: [])
    monkeypatch.setattr(run, "census", lambda *_: [])
    monkeypatch.setattr(run, "validate_resources", lambda *_: {})
    monkeypatch.setattr(run, "operator_census", lambda *_: {})
    monkeypatch.setattr(run, "surfaces", lambda *_: None)
    monkeypatch.setattr(host_browser, "observe", browser)

    def execute():
        code = run.run({"application": "owned-application-image"}, state.output)
        receipt = json.loads((state.output / "summary.json").read_text(encoding="utf-8"))
        assert state.events[-1] == "namespace_cleanup"
        assert state.namespace is None and receipt["namespace_removed"] is True
        return code, receipt

    state.execute = execute
    return state


@pytest.mark.parametrize("stage", ["pod_fetch", "pod_census"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_final_census_diagnostic_survives_cleanup_without_later_checks(
    observer, monkeypatch, capsys, stage, cleanup_fails
):
    from qualification.monitoring.census_diagnostics import CensusError, record

    if stage == "pod_fetch":
        observer.final_query_error = RuntimeError("private-query-canary")
        reason = "query_failed"
    else:
        reason = "container_restart_count"

        def fail_census(*_):
            raise CensusError(reason, "private-census-canary")

        monkeypatch.setattr(run, "census", fail_census)
    monkeypatch.setattr(
        run, "validate_resources", lambda *_: pytest.fail("resource census must not run")
    )
    cleanup = run.cleanup_namespace
    cleanups = []

    def checked_cleanup(*args):
        cleanups.append(True)
        result = cleanup(*args)
        if cleanup_fails:
            raise RuntimeError("private-cleanup-canary")
        return result

    monkeypatch.setattr(run, "cleanup_namespace", checked_cleanup)
    assert run.run({"application": "owned-application-image"}, observer.output) == 1
    raw = (observer.output / "summary.json").read_text(encoding="utf-8")
    receipt = json.loads(raw)
    assert receipt["census_diagnostic"] == record(stage, reason)
    assert receipt["failure_phase"] == "census" and receipt["status"] == "failed"
    assert receipt["namespace_removed"] is (not cleanup_fails)
    assert all(key not in receipt for key in ("containers", "live_resources", "operator"))
    assert observer.final_queries == ["pods"] and cleanups == [True]
    assert observer.events[-1] == "namespace_cleanup"
    assert "private-" not in raw + capsys.readouterr().out


def test_later_census_failure_is_not_mislabeled_as_pod_census(observer, monkeypatch):
    def fail_resources(*_):
        raise ValueError("private-resource-canary")

    monkeypatch.setattr(run, "validate_resources", fail_resources)
    code, receipt = observer.execute()
    assert code == 1 and receipt["failure_phase"] == "census"
    assert "containers" in receipt and "census_diagnostic" not in receipt


def test_auth_is_mandatory_after_successful_replay_before_browser(observer):
    code, receipt = observer.execute()
    assert code == 0 and receipt["status"] == "passed"
    assert observer.events == [
        "http",
        "replay",
        "replay_observed",
        "auth",
        "browser",
        "namespace_cleanup",
    ]
    assert receipt["ray_auth"] == observer.auth
    assert set(receipt["ray_auth"]) == RAY_AUTH_RECEIPT_KEYS | {"schema_version"}


@pytest.mark.parametrize("field", sorted(RAY_AUTH_RECEIPT_KEYS))
@pytest.mark.parametrize("failure", ["missing", "false"])
def test_every_auth_fact_is_required_by_host_and_failure_still_cleans(observer, field, failure):
    if failure == "missing":
        del observer.auth[field]
    else:
        observer.auth[field] = False
    code, receipt = observer.execute()
    assert code == 1 and receipt["failure_phase"] == "ray_auth"
    assert "auth" in observer.events and "browser" not in observer.events
    assert "ray_auth" not in receipt


def test_replay_failure_does_not_enter_auth_or_browser(observer):
    observer.replay_passes = False
    code, receipt = observer.execute()
    assert code == 1 and receipt["failure_phase"] == "replay"
    assert "auth" not in observer.events and "browser" not in observer.events


def test_auth_transport_failure_is_sanitized_and_still_cleans(observer, capsys):
    observer.auth_error = True
    code, receipt = observer.execute()
    assert code == 1 and receipt["failure_phase"] == "ray_auth"
    assert "browser" not in observer.events
    assert (
        "unbounded-private-error-canary" not in observer.output.joinpath("summary.json").read_text()
    )
    assert "unbounded-private-error-canary" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "replay_at,auth_seconds,passes,auth_started",
    [(480, 70, True, True), (481, 1, False, False), (480, 70.001, False, True)],
)
def test_auth_preserves_total_deadline_and_following_browser_reserve(
    observer, replay_at, auth_seconds, passes, auth_started
):
    observer.replay_at = replay_at
    observer.auth_seconds = auth_seconds
    code, receipt = observer.execute()
    assert (code == 0) is passes
    assert ("auth" in observer.events) is auth_started
    assert ("browser" in observer.events) is passes
    if not passes:
        assert receipt["failure_phase"] == "ray_auth"
