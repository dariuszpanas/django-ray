"""Contracts for the adopter-facing Ray ecosystem support boundary."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
GUIDE = DOCS / "ray-ecosystem.md"

MATRIX_HEADERS = (
    "Component",
    "Install and django-ray 0.4 status",
)
EXPECTED_MATRIX = {
    "Ray Core": ("ray[default]", "First-class django-ray execution path"),
    "Ray Client": ("ray[default]", "connection-owned lifetime"),
    "Ray Jobs": ("ray[default]", "First-class django-ray execution path"),
    "Dashboard and State APIs": ("ray[default]", "Live diagnostics only"),
    "Ray Workflows": ("removed upstream", "Unrelated to django-ray workflows"),
    "Ray Data": ("ray[data]==2.56.0", "Shipped application-owned Ray Job recipe"),
    "Ray Train": ("ray[train]", "application-owned workload; untested"),
    "Ray Tune": ("ray[tune]", "application-owned workload; untested"),
    "RLlib": ("ray[rllib]", "application-owned workload; untested"),
    "Ray Serve": ("ray[serve]", "Separate application/platform-owned service"),
    "Ray Serve LLM": ("ray[serve,llm]", "Deferred, evidence-gated service"),
    "Compiled Graph": ("ray[cgraph]", "no enabled strategy"),
}


def _section(content: str, heading: str, *, level: int = 2) -> str:
    marker = re.compile(rf"^{'#' * level} {re.escape(heading)}\s*$", re.MULTILINE)
    match = marker.search(content)
    assert match is not None

    start = match.end()
    next_heading = re.search(rf"^{'#' * level} ", content[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(content)
    return content[start:end]


def _table_rows(section: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    lines = [line for line in section.splitlines() if line.startswith("|")]
    assert len(lines) >= 3

    parsed = [tuple(cell.strip() for cell in line.strip().strip("|").split("|")) for line in lines]
    headers = parsed[0]
    assert all(re.fullmatch(r":?-{3,}:?", cell) for cell in parsed[1])

    rows: dict[str, tuple[str, ...]] = {}
    for cells in parsed[2:]:
        assert len(cells) == len(headers)
        component = cells[0].strip("*")
        assert component not in rows
        rows[component] = cells
    return headers, rows


def _nav_markdown_paths(value: Any) -> set[Path]:
    if isinstance(value, str):
        return {Path(value)} if value.endswith(".md") else set()
    if isinstance(value, list):
        return set().union(*(_nav_markdown_paths(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(_nav_markdown_paths(item) for item in value.values()))
    return set()


def test_ray_ecosystem_matrix_matches_package_install_boundary() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert "ray[default]>=2.56.0" in dependencies
    assert all(
        not requirement.lower().startswith("ray[")
        for requirements in optional_dependencies.values()
        for requirement in requirements
    )

    content = GUIDE.read_text(encoding="utf-8")
    headers, rows = _table_rows(_section(content, "Install and support matrix"))
    assert headers == MATRIX_HEADERS
    assert set(rows) == set(EXPECTED_MATRIX)

    for component, (install, status) in EXPECTED_MATRIX.items():
        row = rows[component]
        assert install in row[1]
        assert status in row[1]

    assert "ray[default]>=2.53.0" not in content
    assert "`ray`,\n`ray.job_submission`, and `ray.dag` import" in content


def test_ray_ecosystem_contract_excludes_live_handles_and_unsafe_reuse() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    contract = re.sub(
        r"\s+",
        " ",
        _section(content, "Cross-ecosystem durable contract"),
    )

    for forbidden_handle in (
        "`Dataset`",
        "`ObjectRef`",
        "Train `Result`",
        "Tune `ResultGrid`",
        "RLlib `Algorithm`",
        "Serve handle",
        "`CompiledDAGRef`",
    ):
        assert forbidden_handle in contract

    for required_boundary in (
        "one finite outer durability boundary",
        "bounded JSON and immutable URIs",
        "Keep the ORM out of the distributed data plane",
        "storage-specific create-only or conditional-commit primitive",
        "Artifact completion is not durable task success",
        "authoritative current task is `SUCCEEDED`",
        "new fenced namespace",
        "dedicated queues",
        "Treat cancellation as intent, not rollback",
    ):
        assert required_boundary in contract

    manifest = _section(content, "Completion manifest", level=3)
    manifest_text = re.sub(r"\s+", " ", manifest)
    assert '"status": "artifact_complete"' in manifest
    assert '"status": "complete"' not in manifest
    assert '"operation_key"' in manifest
    assert '"attempt_namespace"' in manifest
    assert "fences one execution generation and attempt" in manifest_text
    assert "does not claim the Django task committed `SUCCEEDED`" in manifest_text


def test_ray_ecosystem_component_evidence_and_decision_links_are_current() -> None:
    content = GUIDE.read_text(encoding="utf-8")
    destinations = set(re.findall(r"\[[^\]]+\]\(([^)]+)\)", content))

    for destination in (
        "ray-data.md",
        "ray-serve-gateway.md",
        "design/ray-serve-boundary.md",
        "compiled-graph-compatibility.md",
    ):
        assert destination in destinations
        assert (GUIDE.parent / destination).is_file()

    client_jobs = re.sub(r"\s+", " ", _section(content, "Ray Client or Ray Jobs?"))
    assert "limitations for Train and Tune over Ray Client" in client_jobs
    assert "Ray Jobs" in client_jobs
    assert "continue independently" in client_jobs
    assert "cluster loss is not a checkpoint" in client_jobs

    data = re.sub(r"\s+", " ", _section(content, "Ray Data", level=3))
    assert "blocking real-Ray Linux probes on Python 3.12 and 3.14" in data
    assert "bounded Debian Python 3.12 rehearsal pass" in data
    assert "Windows rehearsal is recorded as failing native evidence" in data
    assert "multi-node shared-storage behavior remains unproven" in data

    workflows = re.sub(r"\s+", " ", _section(content, "Ray Workflows", level=3))
    assert "deprecated that experimental library in 2.44" in workflows
    assert "removed it after 2.47" in workflows
    assert "no compatible workflow ID" in workflows
    assert (
        "https://github.com/ray-project/ray/blob/ray-2.56.0/python/ray/workflow/__init__.py"
        in destinations
    )

    compiled = re.sub(r"\s+", " ", _section(content, "Compiled Graph", level=3))
    assert "no verified native capability row" in compiled
    assert "enables no product strategy" in compiled
    assert "fails the residual-resource cleanup invariant" in compiled
    assert "cannot promote it" in compiled

    serve = re.sub(r"\s+", " ", _section(content, "Ray Serve and Serve LLM", level=3))
    assert "tested against a loopback fake upstream" in serve
    assert "does not prove a live Serve or KubeRay deployment" in serve

    official = _section(content, "Official Ray references")
    official_destinations = re.findall(r"\[[^\]]+\]\(([^)]+)\)", official)
    assert len(official_destinations) >= 15
    assert all(
        url.startswith("https://docs.ray.io/en/latest/")
        or url
        == "https://github.com/ray-project/ray/blob/ray-2.56.0/python/ray/workflow/__init__.py"
        for url in official_destinations
    )


def test_ray_ecosystem_guide_is_discoverable_and_reciprocally_linked() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    nav_paths = _nav_markdown_paths(config["project"]["nav"])
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs_readme = (DOCS / "README.md").read_text(encoding="utf-8")
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8")
    changelog = (DOCS / "changelog.md").read_text(encoding="utf-8")

    assert Path("ray-ecosystem.md") in nav_paths
    assert "[Ray Ecosystem Support](ray-ecosystem.md)" in docs_readme
    assert "https://django-ray.readthedocs.io/en/latest/ray-ecosystem/" in readme
    assert "not first-class adapters" in readme
    assert "Ray ecosystem compatibility" not in readme
    assert "former, removed `ray.workflow` package" in readme
    assert "https://django-ray.readthedocs.io/en/latest/ray-ecosystem/" in llms
    assert (ROOT / "llms.txt").read_bytes() == (DOCS / "llms.txt").read_bytes()
    assert "Ray ecosystem support and install matrix" in changelog

    for path in (
        DOCS / "compatibility.md",
        DOCS / "ray-data.md",
        DOCS / "worker-modes.md",
        DOCS / "workflows.md",
        DOCS / "compiled-graph-compatibility.md",
        DOCS / "design" / "ray-serve-boundary.md",
    ):
        assert "ray-ecosystem.md" in path.read_text(encoding="utf-8")
