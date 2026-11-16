"""HTTP authentication rejects malformed credentials before application work."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from django.test import Client

from testproject import api as sample_api

pytestmark = pytest.mark.django_db

_CREDENTIALS = {
    "operator": "synthetic-operator-credential",
    "metrics": "synthetic-metrics-credential",
    "demo": "synthetic-demo-credential",
}
_SETTINGS = {
    "operator": "DJANGO_API_TOKEN",
    "metrics": "DJANGO_METRICS_TOKEN",
    "demo": "DJANGO_DEMO_TOKEN",
}


@pytest.fixture(autouse=True)
def credentials(settings):
    settings.DEBUG = False
    settings.DJANGO_API_ENABLED = True
    settings.DEPLOYMENT_MODE = "demo"
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = True
    for name, value in _CREDENTIALS.items():
        setattr(settings, _SETTINGS[name], value)


@pytest.mark.parametrize(
    ("method", "path", "authority"),
    [
        ("get", "/api/executions/stats", "operator"),
        ("post", "/api/executions/1/cancel", "operator"),
        ("get", "/api/metrics", "metrics"),
        ("post", "/api/stress/cpu?duration_seconds=0", "demo"),
    ],
)
@pytest.mark.parametrize("malformed", ["incoming", "configured"])
def test_non_ascii_bearer_is_unauthorized_before_application_work(
    settings, monkeypatch, django_assert_num_queries, method, path, authority, malformed
):
    marker = "synthetic-credential-\u00e9"
    token = _CREDENTIALS[authority]
    if malformed == "incoming":
        token = marker
    else:
        setattr(settings, _SETTINGS[authority], marker)

    def unexpected(*args, **kwargs):
        pytest.fail("rejected authentication reached application work")

    for name in (
        "_task_state_counts",
        "render_prometheus_metrics",
        "request_task_cancellation",
        "enqueue_sample",
    ):
        monkeypatch.setattr(sample_api, name, unexpected)

    with django_assert_num_queries(0):
        response = getattr(Client(), method)(path, HTTP_AUTHORIZATION=f"Bearer {token}")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert marker not in response.content.decode()
    assert "TypeError" not in response.content.decode()


@pytest.mark.parametrize("authority", ["operator", "metrics", "demo"])
@pytest.mark.parametrize("enabled", [True, False])
def test_ascii_credentials_keep_authority_separation_and_disabled_behavior(
    settings, monkeypatch, django_assert_num_queries, authority, enabled
):
    settings.DJANGO_API_ENABLED = enabled
    monkeypatch.setattr(sample_api, "_task_state_counts", dict)
    monkeypatch.setattr(
        sample_api, "render_prometheus_metrics", lambda **kwargs: "sample_metric 1\n"
    )
    enqueues = []

    def enqueue(task, **kwargs):
        enqueues.append(kwargs)
        return SimpleNamespace(
            id="synthetic-task",
            status=SimpleNamespace(value="READY"),
            enqueued_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            args=[],
            kwargs=kwargs,
        )

    monkeypatch.setattr(sample_api, "enqueue_sample", enqueue)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {_CREDENTIALS[authority]}")
    with django_assert_num_queries(0):
        stats = client.get("/api/executions/stats")
        demo = client.post("/api/stress/cpu?duration_seconds=0")
        metrics = client.get("/api/metrics")

    assert stats.status_code == (200 if enabled and authority == "operator" else 401)
    assert demo.status_code == (200 if enabled and authority == "demo" else 401)
    assert metrics.status_code == (
        200 if authority == "metrics" or (enabled and authority == "operator") else 401
    )
    assert enqueues == ([{"duration_seconds": 0.0}] if enabled and authority == "demo" else [])


@pytest.mark.parametrize("malformed_authority", ["operator", "metrics"])
def test_malformed_other_credential_preserves_independent_metrics_authority(
    settings, monkeypatch, django_assert_num_queries, malformed_authority
):
    setattr(settings, _SETTINGS[malformed_authority], "synthetic-credential-\u00e9")
    authority = "metrics" if malformed_authority == "operator" else "operator"
    monkeypatch.setattr(
        sample_api, "render_prometheus_metrics", lambda **kwargs: "sample_metric 1\n"
    )
    with django_assert_num_queries(0):
        response = Client(HTTP_AUTHORIZATION=f"Bearer {_CREDENTIALS[authority]}").get(
            "/api/metrics"
        )
    assert response.status_code == 200
    assert response.content == b"sample_metric 1\n"
