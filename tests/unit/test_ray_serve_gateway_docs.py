"""Contracts for the Django-to-private-Ray-Serve adoption recipe."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
GUIDE = DOCS / "ray-serve-gateway.md"
EXAMPLE = DOCS / "examples" / "ray_serve_gateway.py"


def _section(content: str, heading: str) -> str:
    marker = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = marker.search(content)
    assert match is not None
    start = match.end()
    next_heading = re.search(r"^## ", content[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(content)
    return content[start:end]


def _nav_markdown_paths(value: Any) -> set[Path]:
    if isinstance(value, str):
        return {Path(value)} if value.endswith(".md") else set()
    if isinstance(value, list):
        return set().union(*(_nav_markdown_paths(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(_nav_markdown_paths(item) for item in value.values()))
    return set()


def test_gateway_starts_with_copy_and_wire_path_before_long_code() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    checklist = _section(content, "Copy and wire the smallest path")

    assert content.index("## Copy and wire the smallest path") < content.index("```python")
    assert len(re.findall(r"^\d+\. ", checklist, re.MULTILINE)) == 6
    assert "independently" in checklist
    assert "URLconf" in checklist
    assert "both settings affect more than this one route" in checklist
    assert "Do not add\n   automatic POST retries" in checklist


def test_gateway_examples_are_valid_python_without_fastapi_ingress() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    example = EXAMPLE.read_text(encoding="utf-8")
    python_blocks = re.findall(r"```python\n(.*?)\n```", content, re.DOTALL)

    assert len(python_blocks) == 3
    for source in python_blocks:
        ast.parse(source)
    ast.parse(example)
    assert python_blocks[0] == example.rstrip()

    assert "from fastapi" not in content.casefold()
    assert "import fastapi" not in content.casefold()
    assert "from ray" not in python_blocks[0]
    assert "import ray" not in python_blocks[0]
    assert "from ray" not in example
    assert "import ray" not in example
    assert "urllib.request" in python_blocks[0]
    assert "from ray import serve" in python_blocks[2]
    assert "from starlette.requests import Request" in python_blocks[2]
    assert "without writing a FastAPI ingress" in content
    assert "not a claim that the\nserving environment is **without FastAPI**" in content


def test_gateway_code_is_bounded_and_maps_failures_conservatively() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    gateway = re.sub(r"\s+", " ", _section(content, "Copyable synchronous Django gateway"))

    for boundary in (
        "MAX_REQUEST_BYTES = 16 * 1024",
        "MAX_RESPONSE_BYTES = 32 * 1024",
        "MODEL_SERVE_TIMEOUT_SECONDS = 2.0",
        "@csrf_protect def _classify_post",
        "@transaction.non_atomic_requests @never_cache @csrf_exempt def classify",
        "return _classify_post(request, request_id)",
        'request.user.has_perm("myapp.use_model")',
        '"Authorization": f"Bearer {settings.MODEL_SERVE_TOKEN}"',
        "response.read(MAX_RESPONSE_BYTES + 1)",
        "NoRedirectHandler",
        "ProxyHandler({})",
        "except RequestDataTooBig",
        "upstream_status == 408",
        "upstream_status == 429",
        "upstream_status == 503",
        'ModelGatewayError("model_timeout", 504)',
        'ModelGatewayError("model_overloaded", 503)',
        'ModelGatewayError("model_unavailable", 503)',
        "not by itself a total wall-clock deadline",
        "slow-trickle response",
        "Do not blindly retry the POST",
        "MAX_LOG_IDENTIFIER_CHARACTERS = 100",
        "_bounded_log_identifier(request.user.pk)",
        'logger.warning("model gateway request rejected", extra=audit_data)',
    ):
        assert boundary in gateway

    assert "upstream_status in {429, 503}" not in gateway
    assert "require_POST" not in gateway
    assert "Global `CsrfViewMiddleware` sees the outer `csrf_exempt` marker" in gateway
    assert "The POST is therefore not CSRF-exempt" in gateway
    assert "Do not put authentication, parsing, or model work in the exempt dispatcher" in gateway
    assert "do not call `_classify_post` directly from the URLconf" in gateway
    assert "a gateway-only image need not install Ray" in gateway
    assert 'Repeat it with `using="alias"`' in gateway
    assert gateway.count("except (ValueError, RecursionError)") == 2
    assert "per-user rate, quota, and tenant admission" in gateway
    assert "before body or model I/O" in gateway
    assert "do not call it `model_overloaded`" in gateway
    assert "do not replace Django's caller or tenant admission" in gateway
    assert gateway.index("upstream_status == 429") < gateway.index(
        'ModelGatewayError("model_overloaded", 503)'
    )
    assert gateway.index("upstream_status == 503") < gateway.index(
        'ModelGatewayError("model_unavailable", 503)'
    )


def test_failure_table_locks_timeout_overload_and_unavailable_meanings() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    failures = re.sub(r"\s+", " ", _section(content, "Failure mapping"))

    for mapping in (
        "Serve end-to-end timeout returns `408` — `504 model_timeout`",
        "Application-owned admission returns `429` — `503 model_overloaded`",
        "Generic upstream `503` — `503 model_unavailable`",
        "DNS, connection, or service failure — `503 model_unavailable`",
        "Django-owned rate or tenant rejection — application `403` or `429`",
    ):
        assert mapping in failures

    assert "generic `503` is not proof of queue saturation" in failures
    assert "application-owned, versioned discriminator" in failures
    assert "`myapp.model-failure/v1`" in failures
    assert "Never use `Retry-After`" in failures
    assert "Never copy an upstream traceback" in failures
    assert "does not issue a `WWW-Authenticate` challenge" in failures


def test_kuberay_settings_async_and_embedded_django_boundaries_are_explicit() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    gateway = re.sub(r"\s+", " ", _section(content, "Copyable synchronous Django gateway"))
    async_guidance = re.sub(r"\s+", " ", _section(content, "Sync default and async opt-in"))
    embedded = re.sub(r"\s+", " ", _section(content, "Why not embed Django in Serve?"))

    assert "<rayservice>-serve-svc" in content
    assert "classifier-serve-svc.models.svc.cluster.local:8000" in content
    assert "data plane" in content
    assert "control or cluster surfaces" in content
    assert "`CSRF_FAILURE_VIEW` is project-wide" in gateway
    assert "`DATA_UPLOAD_MAX_MEMORY_SIZE` is also project-wide" in gateway
    assert "Do not call blocking `urllib.request` directly" in async_guidance
    assert "entire middleware path is async-capable" in async_guidance
    assert "trust_env=False" in async_guidance
    assert "for every database alias the async path can use" in async_guidance
    assert "sync_to_async(..., thread_sensitive=True)" in async_guidance
    assert "`serve.ingress()` accepts any ASGI-compatible callable" in embedded
    assert "experimental application code" in embedded
    assert "It is not django-ray integration" in embedded


def test_serve_queue_and_evidence_limits_do_not_overclaim_support() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    serve = re.sub(
        r"\s+",
        " ",
        _section(content, "Serve endpoint without writing a FastAPI ingress"),
    )
    evidence = re.sub(r"\s+", " ", _section(content, "Evidence and production validation"))

    for setting in (
        "request_timeout_s: 1.5",
        "max_ongoing_requests: 16",
        "max_queued_requests: 64",
    ):
        assert setting in serve
    assert "marks `max_queued_requests` experimental" in serve
    assert "per-caller bound" in serve
    assert "not a cluster-wide ceiling" in serve
    assert "built-in HTTP backpressure response is a generic `503`" in serve
    assert "intentionally leaves it `model_unavailable`" in serve
    assert "except (ValueError, RecursionError)" in serve
    assert 'headers={"Allow": "POST"}' in serve
    assert "documentation contracts, Python syntax, and a loopback fake upstream" in evidence
    assert "no live Ray Serve or KubeRay execution evidence" in evidence
    assert "not a certified deployment" in evidence


def test_all_external_references_are_official_primary_documentation() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    references = _section(content, "Primary references")
    destinations = re.findall(r"\[[^\]]+\]\(([^)]+)\)", references)

    assert len(destinations) >= 10
    assert all(
        destination.startswith(
            (
                "https://docs.ray.io/en/latest/",
                "https://docs.djangoproject.com/en/6.0/",
                "https://docs.python.org/3/library/",
            )
        )
        for destination in destinations
    )


def test_gateway_is_discoverable_and_reciprocally_linked() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    nav_paths = _nav_markdown_paths(config["project"]["nav"])
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs_readme = (DOCS / "README.md").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    changelog = (DOCS / "changelog.md").read_text(encoding="utf-8")
    ecosystem = (DOCS / "ray-ecosystem.md").read_text(encoding="utf-8")
    serve_boundary = (DOCS / "design" / "ray-serve-boundary.md").read_text(encoding="utf-8")

    assert Path("ray-serve-gateway.md") in nav_paths
    assert "[Django Gateway to Private Ray Serve](ray-serve-gateway.md)" in docs_readme
    hosted_url = "https://django-ray.readthedocs.io/en/latest/ray-serve-gateway/"
    assert hosted_url in readme
    assert hosted_url in llms
    assert (ROOT / "llms.txt").read_bytes() == (DOCS / "llms.txt").read_bytes()
    assert "Django-to-private-Ray-Serve guide" in changelog
    assert "ray-serve-gateway.md" in ecosystem
    assert "Separate application/platform-owned service" in ecosystem
    assert "ray-serve-gateway.md" in serve_boundary
    assert "[gateway module](examples/ray_serve_gateway.py)" in GUIDE.read_text(encoding="utf-8")
