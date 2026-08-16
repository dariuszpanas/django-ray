"""Contracts that keep the growing live documentation discoverable and current."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

from django_ray.conf.defaults import DEFAULTS
from scripts.verify_wheel import EXPECTED_MIGRATION_LEAF

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
EVIDENCE_DIRECTORIES = {"benchmarks", "investigations"}
SETTINGS_OUTSIDE_DJANGO_RAY = {"RAY_DASHBOARD_URL"}
PRIVATE_VULNERABILITY_REPORT_URL = (
    "https://github.com/dariuszpanas/django-ray/security/advisories/new"
)
SECURITY_POLICY_URL = "https://github.com/dariuszpanas/django-ray/security/policy"


def _markdown_heading_anchors(content: str) -> set[str]:
    anchors: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", content, re.MULTILINE):
        plain = heading.replace("`", "").lower()
        anchor = re.sub(r"[^\w\s-]", "", plain)
        anchors.add(re.sub(r"[\s-]+", "-", anchor).strip("-"))
    return anchors


def _assert_markdown_link(source: Path, destination: str) -> None:
    content = source.read_text(encoding="utf-8")
    destinations = set(re.findall(r"\[[^\]]+\]\(([^)]+)\)", content))
    assert destination in destinations


def _assert_local_markdown_link(source: Path, destination: str) -> None:
    _assert_markdown_link(source, destination)

    relative, separator, fragment = destination.partition("#")
    target = (source.parent / relative).resolve() if relative else source.resolve()
    assert target.is_relative_to(ROOT.resolve())
    assert target.is_file()
    if separator:
        assert fragment in _markdown_heading_anchors(target.read_text(encoding="utf-8"))


def test_repository_llms_guide_matches_published_copy() -> None:
    assert (ROOT / "llms.txt").read_bytes() == (DOCS / "llms.txt").read_bytes()


def test_local_heavy_resource_coordination_policy_is_consistent() -> None:
    guidance = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in ("AGENTS.md", "CONTRIBUTING.md", "docs/contributing.md")
    }
    profiles = ("ci-final", "real-ray", "kuberay-final")
    platform_boundary = (
        "Contained coordinator runs are supported only on Windows, Linux, and macOS; "
        "on other POSIX hosts they fail before lane acquisition because Phase 1 has no "
        "stable native process-birth identity, and contributors must not bypass the "
        "coordinator."
    )
    posix_legacy_boundary = (
        "On POSIX, the historical real-Ray lock is only a same-user compatibility bridge "
        "when its fixed path is safely usable; a foreign-user inode is ignored without "
        "mutation and never establishes authority."
    )

    for path, content in guidance.items():
        normalized = " ".join(content.split())
        assert "uv run make local-resources" in content, path
        assert all(profile in content for profile in profiles), path
        assert "safe action" in normalized.lower(), path
        assert "same OS user" in normalized, path
        assert "malicious same-user" in content, path
        assert "PostgreSQL" in content and "Docker Compose" in content, path
        assert "explicit live handoff" in normalized, path
        assert "vault" in normalized.lower(), path
        assert "termination" in normalized.lower(), path
        assert platform_boundary in normalized, path

    for path in ("CONTRIBUTING.md", "docs/contributing.md"):
        normalized = " ".join(guidance[path].split())
        assert "`-i`/`--ignore-errors`" in normalized, path
        assert "both" in normalized and "CI entrypoints reject" in normalized, path
        assert "ownership or inheritance failure" in normalized, path

    gate = (DOCS / "deployment" / "local-kuberay-gate.md").read_text(encoding="utf-8")
    normalized_gate = " ".join(gate.split())
    assert platform_boundary in normalized_gate
    assert "## Shared heavy-lane ownership and status" in gate
    assert "uv run make k8s-final-gate-status" in gate
    assert "K8S_CONTEXT=docker-desktop" in gate
    assert "K8S_NAMESPACE=django-ray" in gate
    assert "confirms the API server is local" in normalized_gate
    assert "`kubectl config view`" in gate and "`kubectl ... get`" in gate
    assert "raw/flattened/minified kubeconfig" in normalized_gate
    assert "rejects proxy routing" in normalized_gate
    assert "exact verified snapshot and API server" in normalized_gate
    assert "cleaning the file on every exit" in normalized_gate
    assert "context, namespace, and selected output format as unexpanded private" in normalized_gate
    assert "validates each value as exactly one argument before any status read" in normalized_gate
    assert "discards inherited Make recursion metadata" in normalized_gate
    assert "scrubs the private fields from every `kubectl` child" in normalized_gate
    assert "kubernetes_mirror.state" in gate and "not-configured" in gate
    assert "`image-references-only`" in gate
    assert "current image-reference observation only" in normalized_gate
    assert "historical deploy attribution" in normalized_gate
    contributor_guide = guidance["docs/contributing.md"]
    normalized_contributor_guide = " ".join(contributor_guide.split())
    assert "Standalone pytest" in contributor_guide
    assert "detached Ray descendant" in contributor_guide
    assert "never grants coordinator kill authority" in normalized_contributor_guide
    assert "Preflight-only" in gate and "without acquiring" in normalized_gate
    assert "before the `images` layer" in gate
    assert "bounded diagnostics" in normalized_gate
    assert "private-workspace cleanup" in normalized_gate
    assert "`local-resources`" in gate
    assert "before Git or another preflight helper can inherit it" in normalized_gate
    assert "`local-resources-recheck`" in gate
    assert "revalidate the active record and clean source" in normalized_gate
    assert "durably recorded contained child" in normalized_gate
    assert "outer release pending" in normalized_gate
    assert "`[final-release] passed:" in gate
    assert "Docker daemon work and Kubernetes server-side operations" in normalized_gate
    assert "one fail-closed `&&` recipe" in normalized_gate
    assert "ignore-errors mode cannot continue" in normalized_gate
    assert (
        "The final-gate wrapper never interpolates `K8S_CONTEXT`, `K8S_NAMESPACE`, "
        "`K8S_RAY_RESTART`, `K8S_WEB_URL`, or `K8S_PROMETHEUS_URL` into its recipes"
    ) in normalized_gate
    assert "validates each as exactly one argument before any preflight helper" in normalized_gate
    assert "repeat command-line assignments" in normalized_gate
    assert "keeps those private fields out of Docker and `kubectl` children" in normalized_gate
    assert "exports its unexpanded value through a private internal environment field" in (
        normalized_gate
    )
    assert "bounds and parses it as arguments, never as shell syntax" in normalized_gate
    assert "cannot select help or preflight-only mode" in normalized_gate
    assert (
        "override the wrapper-owned scope, restart decision, or local endpoints" in normalized_gate
    )

    changelog = (DOCS / "changelog.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", maxsplit=1)[1].split("## [", maxsplit=1)[0]
    assert platform_boundary in " ".join(unreleased.split())
    assert "daemonless host-wide local-resource coordinator" in unreleased
    assert "historical real-Ray lock" in unreleased
    assert posix_legacy_boundary in " ".join(guidance["docs/contributing.md"].split())
    assert posix_legacy_boundary in " ".join(unreleased.split())
    assert "no termination authority" in unreleased


def test_security_policy_is_private_bounded_and_discoverable() -> None:
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    normalized_policy = " ".join(policy.split())

    assert "## Supported versions" in policy
    assert "Latest version published on PyPI" in policy
    assert "Earlier versions" in policy
    assert "does not maintain parallel security-support branches" in normalized_policy
    assert PRIVATE_VULNERABILITY_REPORT_URL in policy
    assert "Do **not** open a public issue, discussion, or pull request" in policy
    assert "does not promise a response, fix, or disclosure deadline" in normalized_policy
    assert "develop and test a fix before publishing exploit-enabling details" in normalized_policy
    assert "sanitized, non-actionable public issue" in normalized_policy

    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    root_contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    docs_home = (DOCS / "README.md").read_text(encoding="utf-8")
    docs_contributing = (DOCS / "contributing.md").read_text(encoding="utf-8")

    assert SECURITY_POLICY_URL in root_readme
    assert "[`SECURITY.md`](SECURITY.md)" in root_contributing
    assert SECURITY_POLICY_URL in docs_home
    assert SECURITY_POLICY_URL in docs_contributing
    assert PRIVATE_VULNERABILITY_REPORT_URL in docs_contributing


def test_issue_forms_keep_vulnerability_details_private() -> None:
    template_directory = ROOT / ".github" / "ISSUE_TEMPLATE"
    config = yaml.safe_load((template_directory / "config.yml").read_text(encoding="utf-8"))
    bug_form = yaml.safe_load((template_directory / "bug-report.yml").read_text(encoding="utf-8"))
    feature_form = yaml.safe_load(
        (template_directory / "feature-request.yml").read_text(encoding="utf-8")
    )
    security_contact_form = yaml.safe_load(
        (template_directory / "security-contact.yml").read_text(encoding="utf-8")
    )

    assert config["blank_issues_enabled"] is False
    security_link = next(
        link for link in config["contact_links"] if link["url"] == PRIVATE_VULNERABILITY_REPORT_URL
    )
    assert "privately" in security_link["name"].lower()
    assert "public issue" in security_link["about"].lower()
    assert "secrets" in security_link["about"].lower()

    assert bug_form["labels"] == ["bug"]
    assert bug_form["assignees"] == ["dariuszpanas"]
    assert feature_form["labels"] == ["enhancement"]
    assert feature_form["assignees"] == ["dariuszpanas"]
    for issue_form in (bug_form, feature_form):
        rendered_form = str(issue_form)
        assert PRIVATE_VULNERABILITY_REPORT_URL in rendered_form
        assert "not a suspected security vulnerability" in rendered_form
        assert "credentials, secrets, or private data" in rendered_form

    assert security_contact_form["labels"] == ["area:security"]
    assert security_contact_form["assignees"] == ["dariuszpanas"]
    assert {item["type"] for item in security_contact_form["body"]} == {
        "checkboxes",
        "markdown",
    }
    rendered_security_contact = str(security_contact_form)
    assert PRIVATE_VULNERABILITY_REPORT_URL in rendered_security_contact
    assert "private vulnerability report and it is unavailable" in rendered_security_contact
    assert (
        "affected component or version, impact, reproduction details" in rendered_security_contact
    )
    assert "credentials, secrets, or private data" in rendered_security_contact


def _nav_markdown_paths(value: Any) -> set[Path]:
    if isinstance(value, str):
        return {Path(value)} if value.endswith(".md") else set()
    if isinstance(value, list):
        return set().union(*(_nav_markdown_paths(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(_nav_markdown_paths(item) for item in value.values()))
    return set()


def test_live_documentation_pages_are_navigable() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    nav_paths = _nav_markdown_paths(config["project"]["nav"])
    all_pages = {path.relative_to(DOCS) for path in DOCS.rglob("*.md")}
    live_pages = {
        path for path in all_pages if not path.parts or path.parts[0] not in EVIDENCE_DIRECTORIES
    }

    assert nav_paths <= all_pages
    assert live_pages <= nav_paths


def test_docs_use_one_rich_homepage_source() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    homepage = DOCS / "README.md"
    homepage_text = homepage.read_text(encoding="utf-8")

    assert config["project"]["nav"][0] == {"Home": "README.md"}
    assert homepage.is_file()
    assert not (DOCS / "index.md").exists()
    assert "# django-ray Documentation" in homepage_text
    assert "## What is django-ray?" in homepage_text
    assert "assets/images/testproject-landing.png" in homepage_text
    assert "# Documentation source" not in homepage_text


def test_rolling_upgrade_guide_names_the_current_migration_leaf() -> None:
    architecture = (DOCS / "architecture.md").read_text(encoding="utf-8")
    rolling_upgrades = architecture.split("### Rolling upgrades", maxsplit=1)[1].split(
        "Migrations `0007` and `0008`", maxsplit=1
    )[0]
    migration_app, migration_name = EXPECTED_MIGRATION_LEAF

    assert migration_app == "django_ray"
    assert f"`{migration_name}` before starting upgraded workers" in rolling_upgrades
    assert "Migration `0026` adds unseeded, immutable per-generation claim evidence" in (
        rolling_upgrades
    )
    assert "enables protocol-2 writes" in rolling_upgrades


def test_safe_first_production_path_is_discoverable_and_explicit() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = (DOCS / "getting-started.md").read_text(encoding="utf-8")
    tasks = (DOCS / "tasks.md").read_text(encoding="utf-8")
    retry = (DOCS / "retry.md").read_text(encoding="utf-8")
    workflows = (DOCS / "workflows.md").read_text(encoding="utf-8")
    worker_modes = (DOCS / "worker-modes.md").read_text(encoding="utf-8")
    performance = (DOCS / "performance.md").read_text(encoding="utf-8")
    architecture = (DOCS / "architecture.md").read_text(encoding="utf-8")
    celery_migration = (DOCS / "celery-migration.md").read_text(encoding="utf-8")
    runbook = (DOCS / "runbook.md").read_text(encoding="utf-8")
    changelog = (DOCS / "changelog.md").read_text(encoding="utf-8")

    quick_start = readme.split("## Quick Start", maxsplit=1)[1].split(
        "## Worker Execution Modes", maxsplit=1
    )[0]
    assert '@task(queue_name="default")' in quick_start
    assert "add_numbers.enqueue(20, 22)" in quick_start
    assert 'task_backends["default"].get_result(enqueued.id)' in quick_start
    _assert_markdown_link(
        ROOT / "README.md",
        "https://django-ray.readthedocs.io/en/latest/getting-started/",
    )
    _assert_markdown_link(
        ROOT / "README.md",
        "https://django-ray.readthedocs.io/en/latest/celery-migration/",
    )
    readme_destinations = re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
    assert readme_destinations
    assert all(target.startswith(("https://", "http://")) for target in readme_destinations)
    assert "not first-class adapters" in readme
    assert "ensuring no tasks are lost" not in readme
    assert "delivery is at least once" not in readme
    assert "In a separate terminal, start the worker" in quick_start
    assert quick_start.index("After the worker reports completion") < quick_start.index(
        'task_backends["default"].get_result(enqueued.id)'
    )
    python_sources = re.findall(r"```python\n(.*?)\n```", quick_start, re.DOTALL)
    assert python_sources
    for source in python_sources:
        ast.parse(source)

    production = getting_started.split("## Before Production", maxsplit=1)[1].split(
        "## A Real Django Task", maxsplit=1
    )[0]
    normalized_production = re.sub(
        r"\s+",
        " ",
        re.sub(r"^>\s?", "", production, flags=re.MULTILINE),
    )
    for required_boundary in (
        "Execution is not exactly once",
        "expire or be cancelled before application code runs",
        "work that starts may repeat after lost completion evidence",
        "workflow retry starts again at its entry node",
        "transaction.on_commit()",
        "earliest eligibility time for one submission",
        "Use PostgreSQL for production",
        "Every queue selected by a producer",
        "Queued work expires after 24 hours by default",
        "use an unlimited queue only with idempotent tasks",
        "Cluster Ray Core uses Ray Client",
        "outer django-ray retry is a replay, not a resume",
    ):
        assert required_boundary in normalized_production
    assert "Execution is at least once" not in normalized_production
    _assert_local_markdown_link(
        DOCS / "getting-started.md", "retry.md#make-side-effects-idempotent"
    )
    _assert_local_markdown_link(
        DOCS / "getting-started.md", "tasks.md#enqueue-after-a-database-commit"
    )
    _assert_local_markdown_link(DOCS / "getting-started.md", "retry.md#workflow-retries")
    _assert_local_markdown_link(DOCS / "getting-started.md", "worker-modes.md#cluster-ray-core")
    _assert_local_markdown_link(DOCS / "getting-started.md", "workflows.md#durability-semantics")
    _assert_local_markdown_link(DOCS / "getting-started.md", "queues.md#run-queue-specific-workers")
    _assert_local_markdown_link(DOCS / "getting-started.md", "tasks.md#queue-expiration")
    assert "partial(send_email.enqueue, to=to, subject=subject, body=body)" in tasks
    normalized_queue_expiration = re.sub(
        r"\s+",
        " ",
        tasks.split("## Queue expiration", maxsplit=1)[1].split(
            "## Reading Current Status", maxsplit=1
        )[0],
    )
    for required_expiration_boundary in (
        "row becomes ineligible for a task claim",
        "during a subsequent bounded sweep",
        "remain visibly `QUEUED` past its deadline",
        "will not execute when the worker restarts",
    ):
        assert required_expiration_boundary in normalized_queue_expiration
    transaction_section = tasks.split("### Enqueue after a database commit", maxsplit=1)[1].split(
        "## Priority", maxsplit=1
    )[0]
    transaction_sources = re.findall(r"```python\n(.*?)\n```", transaction_section, re.DOTALL)
    assert len(transaction_sources) == 1
    ast.parse(transaction_sources[0])

    normalized_retry = re.sub(r"\s+", " ", retry)
    assert "does not provide exactly-once execution" in normalized_retry
    assert "Queued work can expire or be cancelled before application code runs" in normalized_retry
    assert "provides at-least-once execution" not in normalized_retry
    normalized_architecture = re.sub(r"\s+", " ", architecture)
    normalized_celery_migration = re.sub(r"\s+", " ", celery_migration)
    normalized_runbook = re.sub(r"\s+", " ", runbook)
    for current_contract in (
        normalized_architecture,
        normalized_celery_migration,
        normalized_runbook,
    ):
        assert "Queued work can expire or be cancelled before application code" in current_contract
    assert "does not provide exactly-once execution" in normalized_architecture
    assert (
        "work that starts may be replayed after uncertain completion" in normalized_celery_migration
    )
    assert "Treat started production work as replayable, not exactly once" in normalized_runbook

    workflow_retry = retry.split("## Workflow Retries", maxsplit=1)[1].split(
        "## Lost and Stuck Work", maxsplit=1
    )[0]
    normalized_workflow_retry = re.sub(r"\s+", " ", workflow_retry)
    for required_boundary in (
        "does not resume at the failed node",
        "progress state is not proof",
        "external system's idempotency receipt",
        "transaction/outbox or reconciliation record",
        "unknown external outcome as reconciliation work",
    ):
        assert required_boundary in normalized_workflow_retry
    durability = workflows.split("## Durability Semantics", maxsplit=1)[1].split(
        "## Test Project Examples", maxsplit=1
    )[0]
    normalized_durability = re.sub(r"\s+", " ", durability)
    for required_boundary in (
        "reruns the workflow from its entry node",
        "diagnostic evidence",
        "Durable selective stage resume remains a planned extension",
        "Never infer that an external effect is safe to skip or repeat",
    ):
        assert required_boundary in normalized_durability

    normalized_worker_modes = re.sub(r"\s+", " ", worker_modes)
    for required_client_boundary in (
        "Ray Client is not an independent job lifecycle",
        "30-second reconnect grace period by default",
        "RAY_CLIENT_RECONNECT_GRACE_PERIOD",
        "terminating its in-flight workload",
        "retry starts a new attempt",
        "recommends Ray Jobs for long-running work",
        "Train, Tune, RLlib, or other component-owned lifecycles",
        "Keep them off Ray Client",
        "driver independent of the task-manager connection",
    ):
        assert required_client_boundary in normalized_worker_modes
    _assert_markdown_link(
        DOCS / "worker-modes.md",
        "https://docs.ray.io/en/latest/cluster/running-applications/job-submission/ray-client.html",
    )
    assert "Cluster mode is tied to the task manager's Ray Client connection" in readme
    assert "disconnect beyond Ray's reconnect grace period" in re.sub(r"\s+", " ", workflows)
    normalized_performance = re.sub(r"\s+", " ", performance)
    assert "task-manager connection is part of the workload lifetime" in normalized_performance
    assert "Prefer Ray Job" in normalized_performance

    assert changelog.index("### Upgrade from 0.3.1") < changelog.index("### Development scope")
    upgrade = changelog.split("### Upgrade from 0.3.1", maxsplit=1)[1].split(
        "### Development scope", maxsplit=1
    )[0]
    normalized_upgrade = re.sub(r"\s+", " ", upgrade)
    for required_upgrade_step in (
        "migrations `0007` through `0018`",
        "Then stop every old task manager and workflow coordinator",
        "preserve queued rows for the `0016` policy review",
        "instead of submitting them merely to complete the upgrade",
        "Quiesce new claims while already claimed Ray Jobs and active workflows finish",
        "run migration `0015`'s duplicate-ID preflight",
        "Preview the queued backlog before crossing migration `0016`",
        "24-hour default deadline or the deliberate `DJANGO_RAY_EXISTING_QUEUED_UNLIMITED=1` opt-out",
        "Ray 2.56.0 or a newer compatible release",
        "Start only the 0.4.0 fleet after every enqueue writer and task manager is upgraded",
        "do not run old and new writers, task managers, or workflow coordinators together",
        "before enabling input spillover",
        "schema-v3 workflow detail publication default-off",
        "Drain pre-`0014` managers",
        "Retain migration `0018` during that rollback",
    ):
        assert required_upgrade_step in normalized_upgrade
    pause_index = normalized_upgrade.index("Pause producers")
    migration_preflight_index = normalized_upgrade.index(
        "run migration `0015`'s duplicate-ID preflight"
    )
    queue_policy_index = normalized_upgrade.index(
        "Preview the queued backlog before crossing migration `0016`"
    )
    ray_upgrade_index = normalized_upgrade.index("Upgrade task managers, the Ray head")
    migration_apply_index = normalized_upgrade.index(
        "Apply django-ray migrations `0007` through `0018`"
    )
    fleet_start_index = normalized_upgrade.index("Start only the 0.4.0 fleet")
    assert (
        pause_index
        < migration_preflight_index
        < queue_policy_index
        < ray_upgrade_index
        < migration_apply_index
        < fleet_start_index
    )
    _assert_local_markdown_link(DOCS / "changelog.md", "reference/input-storage.md#rolling-upgrade")
    _assert_local_markdown_link(
        DOCS / "changelog.md",
        "runtime-environments.md#roll-out-encrypted-writes",
    )
    _assert_local_markdown_link(DOCS / "changelog.md", "tasks.md#queue-expiration")
    _assert_local_markdown_link(DOCS / "changelog.md", "compatibility.md#supported-versions")


def test_settings_reference_tracks_every_package_default() -> None:
    reference = (DOCS / "reference" / "settings.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### ([A-Z][A-Z0-9_]*)\s*$", reference, re.MULTILINE))

    assert documented == set(DEFAULTS) | SETTINGS_OUTSIDE_DJANGO_RAY


def test_management_commands_are_discoverable_in_live_guides() -> None:
    command_directory = ROOT / "src" / "django_ray" / "management" / "commands"
    commands = {path.stem for path in command_directory.glob("*.py") if path.name != "__init__.py"}
    live_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in DOCS.rglob("*")
        if path.suffix in {".md", ".txt"}
        and path.name != "changelog.md"
        and (
            not path.relative_to(DOCS).parts
            or path.relative_to(DOCS).parts[0] not in EVIDENCE_DIRECTORIES
        )
    )

    assert {command for command in commands if command not in live_text} == set()


def test_result_storage_guide_covers_integrity_rotation_and_incidents() -> None:
    guide = (DOCS / "reference" / "result-storage.md").read_text(encoding="utf-8")

    assert "## Integrity and Authority Contract" in guide
    assert "## Configuration Rotation and Legacy References" in guide
    assert "## Corruption Incident Recovery" in guide
    assert "SHA-256" in guide
    assert "archived-attempt" in guide
    assert "Never change the stored digest or byte count" in guide
    assert "leading and trailing `/`" in guide
    assert "concurrently replace digest" in guide


def test_input_storage_guide_states_filesystem_trust_boundary() -> None:
    guide = (DOCS / "reference" / "input-storage.md").read_text(encoding="utf-8")

    assert "INPUT_STORAGE_FILESYSTEM_PATH" in guide
    assert "concurrently" in guide
    assert "symlinks or Windows reparse points" in guide


def test_execution_api_docs_define_bounded_list_and_detail_surfaces() -> None:
    api_reference = (DOCS / "reference" / "api.md").read_text(encoding="utf-8")
    observability = (DOCS / "observability.md").read_text(encoding="utf-8")

    for document in (api_reference, observability):
        normalized = " ".join(document.lower().split())
        assert "4,096" in document
        assert "256 KiB" in document
        assert "stored_value_exceeds_list_limit" in document
        assert "65,536" in document
        assert "stored_value_exceeds_detail_limit" in document
        assert "external_result_not_loaded" in document
        assert "execution_detail_response_limit" in document
        assert "response_size_limit" in document
        assert "next_cursor" in document
        assert "filter-bound" in document
        assert "SQLite" in document and "PostgreSQL" in document
        assert "GET /api/executions/{id}" in document
        assert "exact operator" in document
        assert "lookup" in document
        assert "malformed" in normalized
        assert "[redacted]" in normalized
        assert "ordinary exception" in normalized
        assert "process-control exceptions" in normalized


def test_celery_ray_portfolio_recipe_is_parseable_and_complete() -> None:
    guide = (DOCS / "celery-migration.md").read_text(encoding="utf-8")
    section = guide.split("### Configure Celery and Ray together", maxsplit=1)[1]
    match = re.search(r"```python\n(?P<source>.*?)\n```", section, re.DOTALL)

    assert match is not None
    module = ast.parse(match.group("source"))
    assignments = {
        statement.targets[0].id: statement.value
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    }

    assert ast.literal_eval(assignments["CELERY_BROKER_URL"])
    assert ast.literal_eval(assignments["CELERY_RESULT_BACKEND"])
    assert ast.literal_eval(assignments["CELERY_RESULT_EXTENDED"]) is True

    tasks = ast.literal_eval(assignments["TASKS"])
    assert tasks["default"]["BACKEND"] == "django_tasks_celery.CeleryBackend"
    assert "default" in tasks["default"]["QUEUES"]
    assert tasks["default"]["OPTIONS"]["CELERY_APP"] == "myproject.celery.app"
    assert tasks["ray"]["BACKEND"] == "django_ray.backends.RayTaskBackend"
    assert "ray-batch" in tasks["ray"]["QUEUES"]
    assert tasks["ray"]["OPTIONS"]["RAY_ADDRESS"] == "auto"

    django_ray = ast.literal_eval(assignments["DJANGO_RAY"])
    assert django_ray["RAY_ADDRESS"] == "auto"
    assert django_ray["RUNNER"] == "ray_core"

    route_section = guide.split("### Route only new work with an allowlisted flag", maxsplit=1)[
        1
    ].split("### Refresh a Django `TaskResult`", maxsplit=1)[0]
    route_sources = re.findall(r"```python\n(.*?)\n```", route_section, re.DOTALL)
    service_source = next(source for source in route_sources if "_REPORT_ROUTES" in source)
    service_module = ast.parse(service_source)
    route_assignment = next(
        statement
        for statement in service_module.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "_REPORT_ROUTES"
    )

    assert ast.literal_eval(route_assignment.value) == {
        "default": "default",
        "ray": "ray-batch",
    }

    using_call = next(
        node
        for node in ast.walk(service_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "using"
    )
    assert {keyword.arg: ast.unparse(keyword.value) for keyword in using_call.keywords} == {
        "backend": "backend_alias",
        "queue_name": "queue_name",
    }

    receipt_call = next(
        node
        for node in ast.walk(service_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
    )
    assert {keyword.arg: ast.unparse(keyword.value) for keyword in receipt_call.keywords} == {
        "backend_alias": "enqueued.backend",
        "task_id": "enqueued.id",
    }

    result_section = guide.split("### Refresh a Django `TaskResult`", maxsplit=1)[1].split(
        "### Configure retrievable oversized results", maxsplit=1
    )[0]
    result_sources = re.findall(r"```python\n(.*?)\n```", result_section, re.DOTALL)
    tracked_source = next(source for source in result_sources if "task_backends" in source)
    tracked_module = ast.parse(tracked_source)
    expressions = {ast.unparse(node) for node in ast.walk(tracked_module)}

    assert "task_backends[receipt.backend_alias]" in expressions
    assert "backend.get_result(receipt.task_id)" in expressions
