"""Contracts for the tracked Docker Compose quickstart."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.bounded_redact import read_redacted_bounded, redact_and_bound
from testproject import docker_smoke

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _TextResponse(_Response):
    def __init__(
        self,
        value: str,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = io.BytesIO(value.encode())


def _compose() -> dict[str, Any]:
    return yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_application_services_share_required_postgresql_configuration() -> None:
    compose = _compose()
    services = compose["services"]
    required_environment = {
        "DATABASE_ENGINE": "django.db.backends.postgresql",
        "DATABASE_NAME": "django_ray",
        "DATABASE_USER": "django_ray",
        "DATABASE_PASSWORD": ("${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD before running Compose}"),
        "DATABASE_HOST": "postgres",
        "DATABASE_PORT": "5432",
    }

    for service_name in ("migrate", "web", "worker", "smoke"):
        environment = services[service_name]["environment"]
        assert {name: environment[name] for name in required_environment} == required_environment

    assert services["web"]["environment"]["DJANGO_API_TOKEN"].startswith("${DJANGO_API_TOKEN:?")
    assert services["smoke"]["environment"]["DJANGO_API_TOKEN"].startswith("${DJANGO_API_TOKEN:?")
    assert "DJANGO_API_TOKEN" not in services["migrate"]["environment"]
    assert "DJANGO_API_TOKEN" not in services["worker"]["environment"]
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith(
        "${POSTGRES_PASSWORD:?"
    )
    assert "secret" not in json.dumps(compose).lower()


def test_migrations_are_a_single_ordered_service() -> None:
    services = _compose()["services"]

    assert services["migrate"]["command"] == ["migrate"]
    assert services["migrate"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    for service_name in ("web", "worker"):
        assert services[service_name]["depends_on"]["migrate"] == {
            "condition": "service_completed_successfully"
        }
    assert "migrate" not in services["web"]["command"]
    assert "migrate" not in services["worker"]["command"]


def test_smoke_is_opt_in_and_waits_for_web_and_worker() -> None:
    smoke = _compose()["services"]["smoke"]

    assert smoke["profiles"] == ["smoke"]
    assert smoke["depends_on"] == {
        "web": {"condition": "service_healthy"},
        "worker": {"condition": "service_started"},
    }
    assert smoke["command"][:3] == ["python", "-m", "testproject.docker_smoke"]


def test_compose_smoke_is_a_blocking_ci_job() -> None:
    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    smoke = jobs["docker-compose-smoke"]
    smoke_commands = "\n".join(
        step.get("run", "") for step in smoke["steps"] if isinstance(step, dict)
    )

    assert "docker compose up --build --detach web worker" in smoke_commands
    assert "docker compose --profile smoke run --rm --no-deps smoke" in smoke_commands
    assert "scripts/bounded_redact.py" in smoke_commands
    assert "--max-chars 65536" in smoke_commands
    assert "docker-compose-smoke" in jobs["build"]["needs"]
    assert "docker-compose-smoke" in jobs["ci-gate"]["needs"]


def test_runtime_image_seeds_pip_after_the_final_uv_sync() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

    ensurepip = "/app/.venv/bin/python -m ensurepip --upgrade --default-pip"
    assert ensurepip in dockerfile
    assert dockerfile.index(ensurepip) > dockerfile.rindex("uv sync --frozen")


def test_ci_diagnostics_redact_before_a_marker_inclusive_hard_bound() -> None:
    secret = "operator-token-that-must-not-leak"
    output = redact_and_bound(
        f"{'x' * 80}{secret}{'y' * 80}",
        secrets=[secret],
        max_chars=100,
        source_truncated=True,
    )

    assert secret not in output
    assert "[diagnostics truncated; output capped at 100 characters]" in output
    assert len(output) <= 100


def test_ci_diagnostics_redact_secrets_split_across_stream_chunks() -> None:
    secret = "split-secret-value"
    output = read_redacted_bounded(
        io.StringIO(f"before-{secret}-after-{'z' * 200}"),
        secrets=[secret],
        max_chars=80,
        chunk_chars=5,
    )

    assert secret not in output
    assert "split-" not in output
    assert "[diagnostics truncated; output capped at 80 characters]" in output
    assert len(output) <= 80


def test_request_json_sends_bearer_token_without_putting_it_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Any = None

    def open_request(request, *, timeout):
        nonlocal captured_request
        captured_request = request
        assert timeout == docker_smoke._REQUEST_TIMEOUT_SECONDS
        return _Response({"status": "ok"})

    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", open_request)

    payload = docker_smoke._request_json(
        "http://web:8000",
        "/api/executions",
        token="private-token",
    )

    assert payload == {"status": "ok"}
    assert captured_request is not None
    assert captured_request.full_url == "http://web:8000/api/executions"
    assert captured_request.get_header("Authorization") == "Bearer private-token"
    assert "private-token" not in captured_request.full_url


def test_admin_text_request_keeps_session_cookie_out_of_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Any = None

    def open_request(request, *, timeout):
        nonlocal captured_request
        captured_request = request
        assert timeout == docker_smoke._REQUEST_TIMEOUT_SECONDS
        return _TextResponse("<html>django-ray</html>")

    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", open_request)

    body = docker_smoke._request_text(
        "http://web:8000",
        "/admin/",
        headers={"Cookie": "sessionid=private-session"},
    )

    assert body == "<html>django-ray</html>"
    assert captured_request is not None
    assert captured_request.full_url == "http://web:8000/admin/"
    assert captured_request.get_header("Cookie") == "sessionid=private-session"
    assert "private-session" not in captured_request.full_url


@pytest.mark.parametrize("path", ["admin/", "https://example.com/admin/"])
def test_admin_text_request_rejects_nonlocal_paths(path: str) -> None:
    with pytest.raises(docker_smoke.DockerSmokeError, match="local absolute path"):
        docker_smoke._request_text("http://web:8000", path)


def test_unfold_stylesheet_match_accepts_manifest_hash() -> None:
    match = docker_smoke._UNFOLD_STYLESHEET_RE.search(
        '<link href="/static/unfold/css/styles.0123456789ab.css" rel="stylesheet">'
    )

    assert match is not None
    assert match.group("path") == "/static/unfold/css/styles.0123456789ab.css"


def test_django_ray_admin_assets_accept_manifest_hashes() -> None:
    stylesheet_match = docker_smoke._DJANGO_RAY_STYLESHEET_RE.search(
        '<link href="/static/testproject/admin.0123456789ab.css" rel="stylesheet">'
    )
    icon_match = docker_smoke._DJANGO_RAY_ICON_RE.search(
        '<img src="/static/testproject/django-ray.abcdef012345.svg" alt="Home">'
    )

    assert stylesheet_match is not None
    assert stylesheet_match.group("path") == "/static/testproject/admin.0123456789ab.css"
    assert icon_match is not None
    assert icon_match.group("path") == "/static/testproject/django-ray.abcdef012345.svg"


def test_admin_text_request_uses_remaining_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 100.0

    def open_request(_request, *, timeout):
        assert timeout == pytest.approx(0.25)
        return _TextResponse("body", content_type="text/css; charset=utf-8")

    monkeypatch.setattr(docker_smoke.time, "monotonic", lambda: current_time)
    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", open_request)

    assert (
        docker_smoke._request_text(
            "http://web:8000",
            "/static/unfold/css/styles.0123456789ab.css",
            expected_content_type="text/css",
            deadline=current_time + 0.25,
        )
        == "body"
    )


@pytest.mark.parametrize(
    "content_type",
    ("text/javascript; charset=utf-8", "application/javascript"),
)
def test_admin_text_request_accepts_standard_javascript_content_types(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
) -> None:
    monkeypatch.setattr(
        docker_smoke.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _TextResponse(
            "window.djangoRay = true;",
            content_type=content_type,
        ),
    )

    assert (
        docker_smoke._request_text(
            "http://web:8000",
            "/static/django_ray/admin/workflow_diagnostics.js",
            expected_content_type=("text/javascript", "application/javascript"),
        )
        == "window.djangoRay = true;"
    )


def test_admin_text_request_rejects_expired_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_smoke.time, "monotonic", lambda: 100.0)

    with pytest.raises(docker_smoke.DockerSmokeError, match="deadline expired"):
        docker_smoke._request_text(
            "http://web:8000",
            "/admin/",
            deadline=99.0,
        )


def test_admin_text_request_rejects_wrong_static_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_smoke.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _TextResponse("<html>redirected</html>"),
    )

    with pytest.raises(docker_smoke.DockerSmokeError, match="expected text/css"):
        docker_smoke._request_text(
            "http://web:8000",
            "/static/unfold/css/styles.0123456789ab.css",
            expected_content_type="text/css",
        )


def test_admin_smoke_cleanup_attempts_user_delete_when_session_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django.contrib.auth
    import django.contrib.sessions.backends.db

    cleanup_events: list[str] = []

    class FakeUser:
        pk = 1

        def __init__(self, **_kwargs: object) -> None:
            pass

        def set_unusable_password(self) -> None:
            pass

        def save(self) -> None:
            cleanup_events.append("user-save")

        def delete(self) -> None:
            cleanup_events.append("user-delete")

        def get_session_auth_hash(self) -> str:
            return "session-hash"

    class FakeSession(dict[str, str]):
        session_key = "session-key"

        def save(self) -> None:
            cleanup_events.append("session-save")

        def delete(self, session_key: str) -> None:
            assert session_key == self.session_key
            cleanup_events.append("session-delete")
            raise RuntimeError("session cleanup failed")

    def fail_request(*_args: object, **_kwargs: object) -> str:
        raise docker_smoke.DockerSmokeError("admin request failed")

    monkeypatch.setattr(django.contrib.auth, "get_user_model", lambda: FakeUser)
    monkeypatch.setattr(django.contrib.sessions.backends.db, "SessionStore", FakeSession)
    monkeypatch.setattr(docker_smoke, "_request_text", fail_request)

    with pytest.raises(RuntimeError, match="session cleanup failed"):
        docker_smoke._verify_unfold_admin_contract(
            base_url="http://web:8000",
            deadline=docker_smoke.time.monotonic() + 5,
            execution=SimpleNamespace(pk=1, state="QUEUED"),
            attempt=SimpleNamespace(pk=1, attempt_number=1, state="SUCCEEDED"),
        )

    assert cleanup_events == [
        "user-save",
        "session-save",
        "session-delete",
        "user-delete",
    ]


def test_response_json_rejects_oversized_payload() -> None:
    response = _Response({"value": "x" * docker_smoke._MAX_RESPONSE_BYTES})

    with pytest.raises(docker_smoke.DockerSmokeError, match="byte limit"):
        docker_smoke._response_json(response)
