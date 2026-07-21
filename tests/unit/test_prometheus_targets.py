"""Tests for the bundled Prometheus scrape ownership and health check."""

from pathlib import Path

import pytest
import yaml

from scripts.check_prometheus_targets import inspect_target_health, wait_for_healthy_targets

ROOT = Path(__file__).resolve().parents[2]


def _target(job: str, *, health: str = "up", error: str = "") -> dict[str, object]:
    return {
        "labels": {"instance": f"{job}.example:8080", "job": job},
        "health": health,
        "lastError": error,
    }


def _payload(*targets: dict[str, object]) -> dict[str, object]:
    return {"status": "success", "data": {"activeTargets": list(targets)}}


def test_bundled_prometheus_scrapes_ray_and_authenticated_application_metrics() -> None:
    documents = list(
        yaml.safe_load_all((ROOT / "k8s" / "base" / "monitoring.yaml").read_text(encoding="utf-8"))
    )
    config_map = next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") == "prometheus-config"
    )
    prometheus = yaml.safe_load(config_map["data"]["prometheus.yml"])
    jobs = {config["job_name"]: config for config in prometheus["scrape_configs"]}

    assert set(jobs) == {"django-ray", "ray-head", "ray-workers"}
    assert jobs["django-ray"]["static_configs"] == [{"targets": ["django-web-svc:80"]}]
    assert jobs["django-ray"]["metrics_path"] == "/api/metrics"
    assert jobs["django-ray"]["authorization"] == {
        "type": "Bearer",
        "credentials_file": "/etc/prometheus-secrets/DJANGO_API_TOKEN",
    }
    assert jobs["ray-head"]["metrics_path"] == "/metrics"
    assert jobs["ray-workers"]["kubernetes_sd_configs"]
    ray_worker_selector = next(
        rule
        for rule in jobs["ray-workers"]["relabel_configs"]
        if rule.get("source_labels")
        == ["__meta_kubernetes_pod_label_app", "__meta_kubernetes_pod_label_component"]
    )
    assert ray_worker_selector == {
        "source_labels": [
            "__meta_kubernetes_pod_label_app",
            "__meta_kubernetes_pod_label_component",
        ],
        "regex": "ray;worker",
        "action": "keep",
    }


def test_target_health_accepts_all_expected_scrape_jobs() -> None:
    counts, problems = inspect_target_health(
        _payload(
            _target("django-ray"),
            _target("ray-head"),
            _target("ray-workers"),
            _target("ray-workers"),
        )
    )

    assert counts == {"django-ray": 1, "ray-head": 1, "ray-workers": 2}
    assert problems == []


def test_target_health_reports_missing_down_and_removed_worker_targets() -> None:
    counts, problems = inspect_target_health(
        _payload(
            _target("django-ray", health="down", error="server returned HTTP status 401"),
            _target("django-ray-worker", health="down", error="connection refused"),
        )
    )

    assert counts == {"django-ray": 1, "ray-head": 0, "ray-workers": 0}
    assert problems == [
        "django-ray target django-ray.example:8080 is down: server returned HTTP status 401",
        "expected scrape job 'ray-head' has no active targets",
        "expected scrape job 'ray-workers' has no active targets",
        "removed scrape job 'django-ray-worker' still has 1 active target(s); reload or restart "
        "Prometheus",
    ]


def test_target_health_rejects_an_invalid_prometheus_response() -> None:
    with pytest.raises(ValueError, match="status=success"):
        inspect_target_health({"status": "error"})


def test_target_health_waits_for_prometheus_to_converge() -> None:
    responses = iter(
        [
            _payload(_target("django-ray")),
            _payload(_target("django-ray"), _target("ray-head"), _target("ray-workers")),
        ]
    )
    sleeps: list[float] = []
    timestamps = iter([0.0, 0.0, 1.0])

    counts = wait_for_healthy_targets(
        lambda: next(responses),
        timeout=5,
        interval=1,
        clock=lambda: next(timestamps),
        sleep=sleeps.append,
    )

    assert counts == {"django-ray": 1, "ray-head": 1, "ray-workers": 1}
    assert sleeps == [1]
