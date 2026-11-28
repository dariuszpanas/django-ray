"""Unit tests for testproject UX, WCAG AA accessibility, prod hardening, and API contracts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from django_ray.models import RayTaskExecution, TaskState
from qualification.application.api import MAX_OPENAPI_SCHEMA_BYTES
from testproject.views import _validated_ray_dashboard_url

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LANDING_TEMPLATE = REPOSITORY_ROOT / "testproject/templates/testproject/landing.html"
LANDING_SCRIPT = REPOSITORY_ROOT / "testproject/static/testproject/landing.js"


def _relative_luminance(hex_color: str) -> float:
    color = hex_color.lstrip("#")
    channels = [int(color[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    l1 = _relative_luminance(foreground)
    l2 = _relative_luminance(background)
    bright, dark = (l1, l2) if l1 >= l2 else (l2, l1)
    return (bright + 0.05) / (dark + 0.05)


def _extract_css_vars(css: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", css))


def _import_settings_probe(expr: str, **environment: str | None) -> dict[str, object]:
    env = os.environ.copy()
    for key, value in environment.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import json, testproject.settings as s; print(json.dumps({expr}))",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ============================================================================
# F-051: Landing Page Layout, Visual Craft & WCAG AA Accessibility
# ============================================================================


class TestLandingPageUXAndAccessibility:
    """Verify responsive layout, WCAG AA contrast, dark mode, motion, and ARIA semantics."""

    def test_responsive_grids_and_accessibility_rules_in_landing_css(self) -> None:
        html = LANDING_TEMPLATE.read_text(encoding="utf-8")

        # 6-card balanced desktop grid, 3x2 tablet, 2-col mobile (never 5-col orphan row).
        assert "repeat(5, minmax(0, 1fr))" not in html
        assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in html
        assert "@media (max-width: 1024px)" in html
        assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in html
        assert "@media (max-width: 860px)" in html
        assert ".links, .project-links { grid-template-columns: 1fr; }" in html

        # Dark mode, focus-visible rings, and reduced motion rules.
        assert "color-scheme: light dark;" in html
        assert "@media (prefers-color-scheme: dark)" in html
        assert ":focus-visible" in html
        assert "@media (prefers-reduced-motion: reduce)" in html

    def test_landing_color_tokens_meet_wcag_aa_contrast(self) -> None:
        html = LANDING_TEMPLATE.read_text(encoding="utf-8")
        style_match = re.search(r"<style>(?P<css>.*)</style>", html, re.DOTALL)
        assert style_match is not None
        css = style_match.group("css")

        light_block = re.search(r":root\s*\{(?P<body>[^}]+)\}", css)
        dark_block = re.search(
            r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{(?P<body>[^}]+)\}",
            css,
            re.DOTALL,
        )
        assert light_block is not None
        assert dark_block is not None
        light = _extract_css_vars(light_block.group("body"))
        dark = _extract_css_vars(dark_block.group("body"))

        # Light theme pairs must clear WCAG AA (>= 4.5:1).
        assert _contrast_ratio(light["--accent-strong"], light["--panel"]) >= 4.5
        assert _contrast_ratio(light["--accent-text"], light["--accent"]) >= 4.5
        assert _contrast_ratio(light["--accent-hover-text"], light["--accent-hover"]) >= 4.5
        assert _contrast_ratio(light["--muted"], light["--panel"]) >= 4.5
        assert _contrast_ratio(light["--danger"], light["--panel"]) >= 4.5
        assert _contrast_ratio(light["--success"], light["--panel"]) >= 4.5

        # Dark theme pairs must clear WCAG AA (>= 4.5:1) on repo neutral dark surfaces.
        assert dark["--bg"] == "#0b0c0f"
        assert dark["--panel"] == "#16171a"
        assert dark["--panel-soft"] == "#212226"
        assert _contrast_ratio(dark["--accent-strong"], dark["--panel"]) >= 4.5
        assert _contrast_ratio(dark["--accent-text"], dark["--accent"]) >= 4.5
        assert _contrast_ratio(dark["--accent-hover-text"], dark["--accent-hover"]) >= 4.5
        assert _contrast_ratio(dark["--text"], dark["--panel"]) >= 4.5
        assert _contrast_ratio(dark["--muted"], dark["--panel"]) >= 4.5
        assert _contrast_ratio(dark["--danger"], dark["--panel"]) >= 4.5
        assert _contrast_ratio(dark["--success"], dark["--panel"]) >= 4.5

    @pytest.mark.django_db
    def test_landing_aria_headings_and_external_links(self, settings) -> None:
        settings.DEBUG = True
        settings.RAY_DASHBOARD_URL = "https://ray.example.com"

        response = Client().get("/")
        assert response.status_code == 200
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Cache-Control"] == "no-store"
        content = response.content.decode("utf-8")

        class LandingInspector(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.protected_attrs: dict[str, str | None] = {}
                self.output_attrs: dict[str, str | None] = {}
                self.h2_count = 0
                self.external_links: list[dict[str, str | None]] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                attr_map = dict(attrs)
                if attr_map.get("id") == "protected-response":
                    self.protected_attrs = attr_map
                if attr_map.get("id") == "output":
                    self.output_attrs = attr_map
                if tag == "h2":
                    self.h2_count += 1
                if tag == "a" and (attr_map.get("href") or "").startswith(("http://", "https://")):
                    self.external_links.append(attr_map)

        inspector = LandingInspector()
        inspector.feed(content)
        inspector.close()

        # Massive <pre> container must not use aria-live; concise #output status region does.
        assert "aria-live" not in inspector.protected_attrs
        assert inspector.output_attrs.get("role") == "status"
        assert inspector.output_attrs.get("aria-live") == "polite"
        assert inspector.output_attrs.get("aria-atomic") == "true"

        # Semantic <h2> section headings instead of <strong>.
        assert inspector.h2_count >= 4

        # Every external link enforces noopener noreferrer.
        assert len(inspector.external_links) == 4
        for link in inspector.external_links:
            rel_tokens = set((link.get("rel") or "").split())
            assert {"noopener", "noreferrer"} <= rel_tokens

        # Refresh stats button and Django Admin smoke link exist.
        assert 'id="refresh-stats"' in content
        assert 'id="smoke-task-admin-link"' in content

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "unsafe_url",
        [
            None,
            "",
            "   ",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "ftp://ray.example.com:8265",
            "http://user:pass@ray.example.com:8265",
            "http://ray.example.com:8265?token=secret",
            "http://ray.example.com:8265#frag",
            "http://ray.example.com:99999",
            "http://ray.example.com\nX-Injected: 1",
        ],
    )
    def test_landing_guards_none_and_unsafe_ray_dashboard_url(
        self, settings, unsafe_url: str | None
    ) -> None:
        settings.DEBUG = True
        settings.RAY_DASHBOARD_URL = unsafe_url

        assert _validated_ray_dashboard_url() is None
        response = Client().get("/")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert 'href="None"' not in content
        assert "javascript:" not in content
        assert "Unavailable until a valid HTTP(S)" in content

    @pytest.mark.django_db
    def test_landing_stats_reconcile_other_states_and_ignore_unknown_states(self, settings) -> None:
        settings.DEBUG = True
        RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(task_id="s-1", callable_path="t", state=TaskState.QUEUED),
                RayTaskExecution(task_id="s-2", callable_path="t", state=TaskState.SUCCEEDED),
                RayTaskExecution(task_id="s-3", callable_path="t", state=TaskState.CANCELLED),
                RayTaskExecution(task_id="s-4", callable_path="t", state=TaskState.CANCELLING),
                RayTaskExecution(task_id="s-5", callable_path="t", state=TaskState.LOST),
                RayTaskExecution(task_id="s-unknown", callable_path="t", state="CORRUPT_STATE"),
            ]
        )

        response = Client().get("/")
        assert response.status_code == 200
        ctx = response.context
        assert ctx["total"] == 5
        assert ctx["other"] == 3
        assert ctx["cancelled"] == 1
        assert ctx["cancelling"] == 1
        assert ctx["lost"] == 1
        assert (
            ctx["queued"]
            + ctx["running"]
            + ctx["succeeded"]
            + ctx["failed"]
            + ctx["expired"]
            + ctx["other"]
            == ctx["total"]
        )
        content = response.content.decode("utf-8")
        assert "Includes 3 other (1 cancelled, 1 cancelling, 1 lost)" in content


# ============================================================================
# F-058: Production-Mode Hardening in testproject/
# ============================================================================


@pytest.mark.django_db
class TestProductionHardening:
    """Verify auth gating on / and /api/docs, prod HTTPS/cookie flags, and headers."""

    def test_landing_page_requires_auth_when_debug_is_false(self, settings) -> None:
        settings.DEBUG = False
        settings.DEPLOYMENT_MODE = "demo"
        settings.DJANGO_API_TOKEN = "operator-secret-token-for-landing-test"
        settings.MIDDLEWARE = [
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]

        with CaptureQueriesContext(connection) as queries:
            anon = Client().get("/")

        assert anon.status_code == 401
        assert anon["WWW-Authenticate"] == "Bearer"
        assert anon["Cache-Control"] == "no-store"
        assert anon["X-Content-Type-Options"] == "nosniff"
        assert not [q for q in queries if "django_ray_raytaskexecution" in q["sql"]]
        assert "stat-total" not in anon.content.decode("utf-8")

        # Non-staff user is denied with 403.
        user_model = get_user_model()
        regular = user_model.objects.create_user(username="regular-u", password="pw")
        regular_client = Client()
        regular_client.force_login(regular)
        assert regular_client.get("/").status_code == 403

        # Staff user session succeeds.
        staff = user_model.objects.create_user(username="staff-u", password="pw", is_staff=True)
        staff_client = Client()
        staff_client.force_login(staff)
        staff_resp = staff_client.get("/")
        assert staff_resp.status_code == 200
        assert staff_resp["Cache-Control"] == "no-store"
        assert staff_resp["X-Content-Type-Options"] == "nosniff"

        # Operator Bearer token also succeeds.
        bearer_resp = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}").get("/")
        assert bearer_resp.status_code == 200

    def test_api_docs_and_schema_require_auth_in_production_and_when_disabled(
        self, settings
    ) -> None:
        settings.DEBUG = False
        settings.DEPLOYMENT_MODE = "demo"
        settings.DJANGO_API_ENABLED = True
        settings.DJANGO_API_TOKEN = "operator-secret-token-for-docs-test"

        # DEBUG=False gates /api/docs even in demo mode.
        anon_docs = Client().get("/api/docs")
        assert anon_docs.status_code == 401
        assert anon_docs["WWW-Authenticate"] == "Bearer"
        assert anon_docs["Cache-Control"] == "no-store"
        assert anon_docs["X-Content-Type-Options"] == "nosniff"
        assert anon_docs.json() == {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials were not provided or are invalid.",
        }

        operator = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")
        auth_docs = operator.get("/api/docs")
        assert auth_docs.status_code == 200
        assert auth_docs["Cache-Control"] == "no-store"
        assert auth_docs["X-Content-Type-Options"] == "nosniff"

        # Production mode gates both /api/docs and /api/openapi.json.
        settings.DEPLOYMENT_MODE = "production"
        assert Client().get("/api/openapi.json").status_code == 401
        assert Client().get("/api/docs").status_code == 401
        assert operator.get("/api/openapi.json").status_code == 200
        assert operator.get("/api/docs").status_code == 200

        # Disabled API fails closed for docs and schema even with operator token.
        settings.DJANGO_API_ENABLED = False
        assert operator.get("/api/docs").status_code == 401
        assert operator.get("/api/openapi.json").status_code == 401

    def test_production_settings_enable_https_cookie_and_db_tls_hardening(self) -> None:
        prod_env = {
            "DJANGO_DEPLOYMENT_MODE": "production",
            "DJANGO_SECRET_KEY": "abcDEF123!@#xyz9876543210_random_value_for_production_1234567890",
            "DJANGO_API_TOKEN": "tT9!random-api-token-1234567890-abcdefgh",
            "DJANGO_DEBUG": "False",
            "DJANGO_ALLOWED_HOSTS": "app.example.com",
            "DATABASE_ENGINE": "django.db.backends.postgresql",
            "DJANGO_ALLOW_INSECURE_HTTP": None,
            "DATABASE_SSLMODE": None,
        }
        hardened = _import_settings_probe(
            """{
                "session_secure": s.SESSION_COOKIE_SECURE,
                "csrf_secure": s.CSRF_COOKIE_SECURE,
                "ssl_redirect": s.SECURE_SSL_REDIRECT,
                "hsts": s.SECURE_HSTS_SECONDS,
                "hsts_sub": s.SECURE_HSTS_INCLUDE_SUBDOMAINS,
                "nosniff": s.SECURE_CONTENT_TYPE_NOSNIFF,
                "httponly": s.SESSION_COOKIE_HTTPONLY,
                "xframe": s.X_FRAME_OPTIONS,
                "sslmode": s.DATABASES["default"]["OPTIONS"].get("sslmode"),
            }""",
            **prod_env,
        )
        assert hardened == {
            "session_secure": True,
            "csrf_secure": True,
            "ssl_redirect": True,
            "hsts": 31536000,
            "hsts_sub": True,
            "nosniff": True,
            "httponly": True,
            "xframe": "DENY",
            "sslmode": "require",
        }

        rehearsal = _import_settings_probe(
            """{
                "allow_http": s.DJANGO_ALLOW_INSECURE_HTTP,
                "session_secure": getattr(s, "SESSION_COOKIE_SECURE", False),
                "ssl_redirect": getattr(s, "SECURE_SSL_REDIRECT", False),
                "sslmode": s.DATABASES["default"]["OPTIONS"].get("sslmode"),
            }""",
            **{**prod_env, "DJANGO_ALLOW_INSECURE_HTTP": "true"},
        )
        assert rehearsal == {
            "allow_http": True,
            "session_secure": False,
            "ssl_redirect": False,
            "sslmode": None,
        }

    def test_execution_list_and_metrics_set_nosniff_and_no_store(self, settings) -> None:
        operator = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")

        list_resp = operator.get("/api/executions")
        assert list_resp.status_code == 200
        assert list_resp["Cache-Control"] == "no-store"
        assert list_resp["X-Content-Type-Options"] == "nosniff"

        metrics_resp = operator.get("/api/metrics")
        assert metrics_resp.status_code == 200
        assert metrics_resp["Cache-Control"] == "no-store"
        assert metrics_resp["X-Content-Type-Options"] == "nosniff"


# ============================================================================
# F-086: API Contract & Ergonomics Polish
# ============================================================================


@pytest.mark.django_db
class TestAPIContractsAndErgonomics:
    """Verify unified error envelope, OpenAPI docs/scopes, echo payload, health 503, and queue."""

    def test_unified_error_envelope_across_all_error_classes(self, settings) -> None:
        operator = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")
        anon = Client()

        # 401 Unauthorized
        unauth = anon.get("/api/executions")
        assert unauth.status_code == 401
        assert unauth.json() == {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials were not provided or are invalid.",
        }
        assert unauth["Cache-Control"] == "no-store"
        assert unauth["X-Content-Type-Options"] == "nosniff"

        # 404 Missing execution
        not_found = operator.get("/api/executions/999999")
        assert not_found.status_code == 404
        assert not_found.json() == {
            "code": "NOT_FOUND",
            "message": "The requested resource was not found.",
        }

        # 422 Pydantic validation error
        invalid_type = operator.get("/api/executions?limit=not-an-int")
        assert invalid_type.status_code == 422
        invalid_payload = invalid_type.json()
        assert invalid_payload["code"] == "VALIDATION_ERROR"
        assert invalid_payload["message"] == "Request validation failed."
        assert isinstance(invalid_payload["errors"], list)
        assert "detail" not in invalid_payload

        # 422 Invalid cursor
        bad_cursor = operator.get("/api/executions?cursor=tampered")
        assert bad_cursor.status_code == 422
        assert bad_cursor.json() == {
            "code": "INVALID_CURSOR",
            "message": "Invalid execution-list cursor.",
        }

        # 422 Invalid filter
        bad_filter = operator.get("/api/executions", {"state": "bad\x00state"})
        assert bad_filter.status_code == 422
        assert bad_filter.json() == {
            "code": "INVALID_FILTER",
            "message": "Invalid execution-list filter.",
        }

        # 409 Corrupt RuntimeEnv retry conflict uses unified code/message schema
        corrupt = RayTaskExecution.objects.create(
            task_id="unified-err-runtime-env",
            callable_path="test.task",
            state=TaskState.FAILED,
            runtime_env_json='{"env_vars":{"SECRET":"never-leak"}}',
            runtime_env_hash="0" * 64,
        )
        conflict = operator.post(f"/api/executions/{corrupt.pk}/retry")
        assert conflict.status_code == 409
        assert conflict.json() == {
            "code": "RUNTIME_ENV_INVALID",
            "message": "Persisted RuntimeEnv snapshot failed validation.",
        }
        assert "never-leak" not in conflict.content.decode("utf-8")

    def test_invalid_queue_returns_actionable_error_without_echoing_input(self, settings) -> None:
        operator = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")
        canary = "unknown-queue-canary-do-not-reflect"

        resp = operator.post(f"/api/enqueue/add/1/2?queue={canary}")
        assert resp.status_code == 422
        payload = resp.json()
        assert payload["code"] == "INVALID_QUEUE"
        assert "Valid configured queues:" in payload["message"]
        assert "default" in payload["message"]
        assert "high-priority" in payload["message"]
        assert canary not in payload["message"]
        assert not RayTaskExecution.objects.exists()

    def test_enqueue_echo_accepts_json_body_query_message_and_bare_post(self, settings) -> None:
        operator = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")

        # 1. JSON body with message
        r_msg = operator.post(
            "/api/enqueue/echo",
            data=json.dumps({"message": "hello-echo"}),
            content_type="application/json",
        )
        assert r_msg.status_code == 200
        assert r_msg.json()["args"] == []
        assert r_msg.json()["kwargs"] == {"message": "hello-echo"}

        # 2. JSON body with explicit args & kwargs
        r_full = operator.post(
            "/api/enqueue/echo",
            data=json.dumps({"args": ["alpha", 42], "kwargs": {"env": "test"}}),
            content_type="application/json",
        )
        assert r_full.status_code == 200
        assert r_full.json()["args"] == ["alpha", 42]
        assert r_full.json()["kwargs"] == {"env": "test"}

        # 3. Query parameter ?message=...
        r_query = operator.post("/api/enqueue/echo?message=via-query")
        assert r_query.status_code == 200
        assert r_query.json()["kwargs"] == {"message": "via-query"}

        # 4. Backward-compatible bare POST with zero args
        r_bare = operator.post("/api/enqueue/echo")
        assert r_bare.status_code == 200
        assert r_bare.json()["args"] == []
        assert r_bare.json()["kwargs"] == {}

        # 5. Oversized payload rejected before enqueue
        r_bad = operator.post(
            "/api/enqueue/echo",
            data=json.dumps({"message": "x" * 2049}),
            content_type="application/json",
        )
        assert r_bad.status_code == 422
        assert r_bad.json()["code"] == "INVALID_INPUT"

    def test_openapi_documents_error_responses_split_bearer_scopes_and_accurate_docs(
        self, settings
    ) -> None:
        settings.DEPLOYMENT_MODE = "demo"
        settings.DJANGO_DEMO_WORKLOADS_ENABLED = False
        operator = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")

        schema_disabled_resp = operator.get("/api/openapi.json")
        assert schema_disabled_resp.status_code == 200
        assert len(schema_disabled_resp.content) <= MAX_OPENAPI_SCHEMA_BYTES
        schema_disabled = schema_disabled_resp.json()
        assert "ApiErrorSchema" in schema_disabled["components"]["schemas"]
        assert set(schema_disabled["components"]["responses"]) >= {
            "UnauthorizedError",
            "ValidationError",
            "RateLimitedError",
            "ServiceUnavailableError",
        }
        assert "ApiTokenAuth" in schema_disabled["components"]["securitySchemes"]
        assert "MetricsTokenAuth" in schema_disabled["components"]["securitySchemes"]
        assert "DemoTokenAuth" not in schema_disabled["components"]["securitySchemes"]

        # Health endpoint documents both 200 and 503.
        health_responses = schema_disabled["paths"]["/api/health"]["get"]["responses"]
        assert {"200", "503"} <= set(health_responses)

        # Authenticated enqueue endpoint documents 401, 422, 429, 503.
        add_responses = schema_disabled["paths"]["/api/enqueue/add/{a}/{b}"]["post"]["responses"]
        assert {"200", "401", "422", "429", "503"} <= set(add_responses)

        # Enable demo mode and confirm separate DemoTokenAuth scheme + accurate stress_memory doc.
        settings.DJANGO_DEMO_WORKLOADS_ENABLED = True
        settings.DJANGO_DEMO_TOKEN = "demo-openapi-token-1234567890-abcdefgh"
        schema_enabled_resp = operator.get("/api/openapi.json")
        assert schema_enabled_resp.status_code == 200
        assert len(schema_enabled_resp.content) <= MAX_OPENAPI_SCHEMA_BYTES
        schema_enabled = schema_enabled_resp.json()
        schemes = schema_enabled["components"]["securitySchemes"]
        assert {"ApiTokenAuth", "DemoTokenAuth", "MetricsTokenAuth"} <= set(schemes)
        assert "DJANGO_DEMO_TOKEN" in schemes["DemoTokenAuth"]["description"]
        assert "DJANGO_API_TOKEN" in schemes["ApiTokenAuth"]["description"]
        assert "DJANGO_METRICS_TOKEN" in schemes["MetricsTokenAuth"]["description"]

        stress_mem_op = schema_enabled["paths"]["/api/stress/memory"]["post"]
        assert stress_mem_op["security"] == [{"DemoTokenAuth": []}]
        assert "default: 8" in stress_mem_op["description"]
        assert "default: 100" not in stress_mem_op["description"]

    def test_landing_js_bounded_polling_and_reduced_motion_behavior(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI"):
                pytest.fail("Node.js is required for landing.js polling checks in CI")
            pytest.skip("Node.js is unavailable")

        probe = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const script = fs.readFileSync(process.argv[1], "utf8");
const ids = [
  "api-token", "use-token", "credential-status", "forget-token", "output",
  "trigger", "refresh-stats", "smoke-task-admin-wrap", "view-metrics",
  "view-executions", "protected-response", "protected-response-title",
  "protected-response-body", "close-protected-response", "stat-other-summary",
  "stat-total", "stat-queued", "stat-running", "stat-succeeded",
  "stat-failed", "stat-expired",
];
class FakeEl {
  constructor() {
    this.classList = { add() {}, remove() {}, contains() { return false; } };
    this.disabled = false;
    this.hidden = true;
    this.listeners = new Map();
    this.textContent = "";
    this.value = "";
    this.scrollArgs = [];
  }
  addEventListener(name, cb) { this.listeners.set(name, cb); }
  focus() {}
  scrollIntoView(opts) { this.scrollArgs.push(opts); }
}
const elements = new Map(ids.map((id) => [id, new FakeEl()]));
const fetchCalls = [];
const fetchQueue = [];
const timers = [];
globalThis.document = { querySelector: (s) => elements.get(s.slice(1)) };
globalThis.Headers = class {
  constructor() { this.m = new Map(); }
  set(k, v) { this.m.set(k.toLowerCase(), String(v)); }
  get(k) { return this.m.get(k.toLowerCase()); }
};
globalThis.window = {
  sessionStorage: { removeItem() {} },
  matchMedia: (q) => ({ matches: q === "(prefers-reduced-motion: reduce)" }),
  setTimeout: (cb, ms) => { timers.push({ cb, ms }); return timers.length; },
  clearTimeout: () => {},
  async fetch(url, opts) {
    fetchCalls.push({ url, opts });
    return fetchQueue.shift();
  },
};
const resp = (status, payload) => ({
  ok: status >= 200 && status < 300,
  status,
  async json() { return payload; },
  async text() { return String(payload); },
});

vm.runInThisContext(script, { filename: "landing.js" });

// Authenticate with reconciled "other" stats.
fetchQueue.push(resp(200, {
  total: 4, queued: 1, running: 0, succeeded: 1, failed: 0, expired: 0,
  cancelled: 1, cancelling: 0, lost: 1,
}));
elements.get("api-token").value = "tok";
await elements.get("use-token").listeners.get("click")({});
assert.equal(elements.get("stat-other-summary").hidden, false);
assert.match(elements.get("stat-other-summary").textContent, /Includes 2 other/);

// Trigger smoke task -> initial stats refresh shows queued=1 -> bounded /api/tasks/{id} poll.
fetchQueue.push(resp(200, { task_id: "smoke-42", status: "READY" }));
await elements.get("trigger").listeners.get("click")({});
assert.equal(elements.get("smoke-task-admin-wrap").hidden, false);
assert.equal(timers.length, 1);

// Tick 0: refreshStats returns queued=1 -> schedules /api/tasks/smoke-42 poll.
fetchQueue.push(resp(200, { total: 5, queued: 1, running: 0, succeeded: 2, failed: 0, expired: 0 }));
await timers.shift().cb();
assert.equal(timers.length, 1);

// Tick 1: task still RUNNING -> schedules next poll.
fetchQueue.push(resp(200, { task_id: "smoke-42", status: "RUNNING", state: "RUNNING" }));
await timers.shift().cb();
assert.equal(fetchCalls.at(-1).url, "/api/tasks/smoke-42");
assert.match(elements.get("output").textContent, /RUNNING \(poll 1\//);
assert.equal(timers.length, 1);

// Tick 2: task SUCCEEDED -> stops polling and refreshes stats.
fetchQueue.push(
  resp(200, { task_id: "smoke-42", status: "SUCCESSFUL", state: "SUCCEEDED" }),
  resp(200, { total: 5, queued: 0, running: 0, succeeded: 3, failed: 0, expired: 0 }),
);
await timers.shift().cb();
assert.equal(timers.length, 0);
assert.equal(elements.get("output").textContent, "Task smoke-42 finished with state SUCCEEDED.");
assert.equal(elements.get("stat-succeeded").textContent, 3);

// Reduced-motion preference uses behavior: "auto" instead of "smooth".
fetchQueue.push(resp(200, "metrics-body"));
await elements.get("view-metrics").listeners.get("click")({});
assert.deepEqual(elements.get("protected-response").scrollArgs, [
  { behavior: "auto", block: "nearest" },
]);

// Manual Refresh stats button refreshes stats cleanly.
fetchQueue.push(resp(200, { total: 6, queued: 0, running: 0, succeeded: 4, failed: 0, expired: 0 }));
await elements.get("refresh-stats").listeners.get("click")({});
assert.equal(elements.get("stat-total").textContent, 6);
"""
        result = subprocess.run(
            [node, "--input-type=module", "-e", probe, str(LANDING_SCRIPT)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
