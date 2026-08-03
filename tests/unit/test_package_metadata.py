"""Packaging metadata regression tests."""

import re
import tomllib
from pathlib import Path

from coverage import Coverage
from coverage.results import should_fail_under
from packaging.version import Version


def _css_rule_body(stylesheet: str, selector: str) -> str:
    match = re.search(rf"{selector}\s*\{{(?P<body>[^}}]*)\}}", stylesheet)
    assert match is not None
    return match.group("body")


def test_coverage_floor_uses_central_two_decimal_precision() -> None:
    """Coverage must enforce the configured floor at its displayed precision."""
    project_root = Path(__file__).parents[2]
    coverage_config = Coverage(config_file=str(project_root / "pyproject.toml"))
    fail_under = coverage_config.get_option("report:fail_under")
    precision = coverage_config.get_option("report:precision")

    assert fail_under == 95
    assert precision == 2
    assert should_fail_under(94.91, fail_under, precision)
    assert not should_fail_under(95.00, fail_under, precision)


def test_readme_images_use_absolute_urls() -> None:
    """PyPI cannot resolve repository-relative image paths in the long description."""
    readme = Path(__file__).parents[2] / "README.md"
    readme_text = readme.read_text(encoding="utf-8")
    image_targets = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme_text)
    image_targets.extend(re.findall(r'<img[^>]+src="([^"]+)"', readme_text))

    assert image_targets
    assert all(target.startswith(("https://", "http://")) for target in image_targets)


def test_readme_logo_uses_pypi_compatible_absolute_url() -> None:
    """The package logo must use an absolute URL so PyPI can proxy it."""
    readme = Path(__file__).parents[2] / "README.md"
    readme_text = readme.read_text(encoding="utf-8")

    assert (
        "https://raw.githubusercontent.com/dariuszpanas/django-ray/main/"
        "docs/assets/images/django-ray.svg"
    ) in readme_text


def test_admin_observability_assets_are_inside_the_wheel_package() -> None:
    """Hatch includes the admin template and assets through the package selector."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert packages == ["src/django_ray"]
    package_root = project_root / packages[0]
    expected_assets = [
        package_root
        / "templates"
        / "admin"
        / "django_ray"
        / "raytaskexecution"
        / "change_form.html",
        package_root / "templates" / "admin" / "django_ray" / "taskattempt" / "change_form.html",
        package_root / "templates" / "admin" / "django_ray" / "bounded_task_detail_limit.html",
        package_root / "templates" / "admin" / "django_ray" / "sensitive_task_data.html",
        package_root / "templates" / "admin" / "django_ray" / "sensitive_task_data_limit.html",
        package_root / "static" / "django_ray" / "admin" / "task_live.css",
        package_root / "static" / "django_ray" / "admin" / "task_live.js",
        package_root / "static" / "django_ray" / "admin" / "workflow_diagnostics.js",
        package_root / "static" / "django_ray" / "admin" / "sensitive_task_data.css",
        package_root / "static" / "django_ray" / "admin" / "sensitive_task_data_theme.js",
        package_root / "static" / "django_ray" / "admin" / "diagnostics.css",
    ]
    assert all(asset.is_file() and asset.is_relative_to(package_root) for asset in expected_assets)

    stylesheet = (package_root / "static" / "django_ray" / "admin" / "task_live.css").read_text(
        encoding="utf-8"
    )
    assert "#django-ray-live-observability" in stylesheet
    assert "grid-template-columns" in stylesheet
    assert ":focus-visible" in stylesheet
    assert 'html[data-theme="dark"]' in stylesheet
    assert 'html[data-theme="auto"]' in stylesheet
    assert "@media (prefers-color-scheme: dark)" in stylesheet
    assert "@media (max-width: 640px)" in stylesheet
    stock_execution_label_rule = re.search(
        r"body\.django-ray-execution-change #content > h2,\s*"
        r"body\.django-ray-execution-change nav > \.breadcrumbs\s*"
        r"\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert stock_execution_label_rule is not None
    assert "max-width: 100%;" in stock_execution_label_rule.group("body")
    assert "min-width: 0;" in stock_execution_label_rule.group("body")
    assert "overflow-wrap: anywhere;" in stock_execution_label_rule.group("body")
    assert "--django-ray-live-action-bg: #e0f2fe;" in stylesheet
    assert "--django-ray-live-action-fg: #075985;" in stylesheet
    assert "--django-ray-live-action-hover-fg: #0c4a6e;" in stylesheet
    assert "--django-ray-live-state-neutral-fg: inherit;" in stylesheet
    assert "--django-ray-live-status-error-bg: #fef2f2;" in stylesheet
    assert "--django-ray-live-status-error-border: #ef4444;" in stylesheet
    assert "--django-ray-live-workflow-body-bg: transparent;" in stylesheet

    diagnostics_stylesheet = expected_assets[-1].read_text(encoding="utf-8")
    assert ".django-ray-diagnostic" in diagnostics_stylesheet
    assert "white-space: pre-wrap" in diagnostics_stylesheet
    assert "overflow-wrap: anywhere" in diagnostics_stylesheet
    sensitive_action_rule = re.search(
        r"a\.django-ray-admin-action\.django-ray-admin-action--sensitive\s*"
        r"\{(?P<body>[^}]*)\}",
        diagnostics_stylesheet,
    )
    assert sensitive_action_rule is not None
    sensitive_action_body = sensitive_action_rule.group("body")
    for stock_override in (
        "background: #fff7ed;",
        "color: #9a3412;",
        "display: inline-flex;",
        "float: none;",
        "font-size: 0.8125rem;",
        "font-weight: 700;",
        "letter-spacing: normal;",
        "padding: 0.375rem 0.75rem;",
        "text-transform: none;",
    ):
        assert stock_override in sensitive_action_body
    for neutral_dark_token in (
        "--django-ray-live-bg: #16171a;",
        "--django-ray-live-header-bg: #0b0c0f;",
        "--django-ray-live-surface-bg: #212226;",
        "--django-ray-live-border: #303238;",
        "--django-ray-live-heading: #f4f4f5;",
        "--django-ray-live-muted: #a1a1aa;",
        "--django-ray-live-accent: #38bdf8;",
    ):
        assert neutral_dark_token in stylesheet
    assert "background: #0c4a6e;" not in stylesheet
    assert "background: #075985;" not in stylesheet

    summary_arrow_rule = re.search(
        r"\.django-ray-workflow__summary-arrow\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert summary_arrow_rule is not None
    assert "color: var(--django-ray-live-accent-strong);" in summary_arrow_rule.group("body")

    action_hover_rule = re.search(
        r":is\(\s*\.django-ray-workflow__actions a,\s*"
        r"\.django-ray-workflow__copy\s*\):hover\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert action_hover_rule is not None
    assert "color: var(--django-ray-live-action-hover-fg);" in action_hover_rule.group("body")

    explicit_dark_rule = re.search(
        r":is\(html\.dark, html\[data-theme=\"dark\"\]\) "
        r"#django-ray-live-observability\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    auto_dark_rule = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{\s*"
        r"html\[data-theme=\"auto\"\] #django-ray-live-observability\s*"
        r"\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert explicit_dark_rule is not None
    assert auto_dark_rule is not None
    variable_pattern = r"(--django-ray-live-[\w-]+):\s*([^;]+);"
    explicit_dark_variables = dict(re.findall(variable_pattern, explicit_dark_rule.group("body")))
    auto_dark_variables = dict(re.findall(variable_pattern, auto_dark_rule.group("body")))
    assert len(explicit_dark_variables) >= 40
    assert auto_dark_variables == explicit_dark_variables
    assert 'data-state="EXPIRED"' in stylesheet

    script = (package_root / "static" / "django_ray" / "admin" / "task_live.js").read_text(
        encoding="utf-8"
    )
    assert "setTimeout(refresh, 3000)" in script
    assert "document.hidden" in script
    assert 'credentials: "same-origin"' in script
    assert "textContent" in script
    assert "stateNode.dataset.state = state" in script
    assert "innerHTML" not in script
    assert all(state in script for state in ("SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"))

    diagnostics_script = (
        package_root / "static" / "django_ray" / "admin" / "workflow_diagnostics.js"
    ).read_text(encoding="utf-8")
    assert "django-ray-workflow-diagnostics" in diagnostics_script
    assert 'credentials: "same-origin"' in diagnostics_script
    assert 'cache: "no-store"' in diagnostics_script
    assert "textContent" in diagnostics_script
    assert "innerHTML" not in diagnostics_script


def test_unfold_is_testproject_only_and_reproducibly_pinned() -> None:
    """The modern sample admin must not become a required package dependency."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    unfold_requirement = "django-unfold==0.102.0"

    assert all("django-unfold" not in requirement for requirement in project["dependencies"])
    assert unfold_requirement in project["optional-dependencies"]["sample"]
    assert unfold_requirement in config["dependency-groups"]["dev"]
    assert all(
        all("django-unfold" not in requirement for requirement in requirements)
        for extra, requirements in project["optional-dependencies"].items()
        if extra != "sample"
    )


def test_admin_retry_action_has_complete_interaction_feedback() -> None:
    """The native detail retry button must stay legible in every interaction state."""
    project_root = Path(__file__).parents[2]
    stylesheet = (
        project_root / "src/django_ray/static/django_ray/admin/diagnostics.css"
    ).read_text(encoding="utf-8")
    retry_selector = (
        r"#django-ray-task-actions\s+"
        r"\.django-ray-admin-action\.django-ray-admin-action--retry"
    )

    base_rule = _css_rule_body(stylesheet, retry_selector)
    for declaration in (
        "--django-ray-retry-bg: #0369a1;",
        "--django-ray-retry-hover-bg: #075985;",
        "--django-ray-retry-active-bg: #0c4a6e;",
        "--django-ray-retry-focus-ring: #075985;",
        "--django-ray-retry-disabled-bg: #cbd5e1;",
        "--django-ray-retry-disabled-border: #94a3b8;",
        "--django-ray-retry-disabled-fg: #334155;",
        "background-color 120ms ease",
        "border-color 120ms ease",
        "box-shadow 120ms ease",
        "transform 80ms ease;",
    ):
        assert declaration in base_rule
    assert "transition: all" not in base_rule

    hover_rule = _css_rule_body(
        stylesheet,
        rf"{retry_selector}:hover:not\(:disabled\)",
    )
    assert "background: var(--django-ray-retry-hover-bg);" in hover_rule
    assert "border-color: var(--django-ray-retry-hover-bg);" in hover_rule
    assert "transform: translateY(-1px);" in hover_rule

    focus_rule = _css_rule_body(stylesheet, rf"{retry_selector}:focus-visible")
    assert "outline: 3px solid var(--django-ray-retry-focus-ring);" in focus_rule
    assert "outline-offset: 2px;" in focus_rule

    active_rule = _css_rule_body(
        stylesheet,
        rf"{retry_selector}:active:not\(:disabled\)",
    )
    assert "background: var(--django-ray-retry-active-bg);" in active_rule
    assert "box-shadow: inset 0 2px 4px" in active_rule
    assert "transform: translateY(1px);" in active_rule

    disabled_rule = _css_rule_body(stylesheet, rf"{retry_selector}:disabled")
    assert "background: var(--django-ray-retry-disabled-bg);" in disabled_rule
    assert "border-color: var(--django-ray-retry-disabled-border);" in disabled_rule
    assert "color: var(--django-ray-retry-disabled-fg);" in disabled_rule
    assert "cursor: not-allowed;" in disabled_rule
    assert "opacity: 1;" in disabled_rule
    assert "transform: none;" in disabled_rule

    explicit_dark_rule = _css_rule_body(
        stylesheet,
        r":is\(html\.dark, html\[data-theme=\"dark\"\]\)\s+" + retry_selector,
    )
    auto_dark_rule = _css_rule_body(
        stylesheet,
        r"html\[data-theme=\"auto\"\]\s+" + retry_selector,
    )
    for theme_rule in (explicit_dark_rule, auto_dark_rule):
        assert "--django-ray-retry-focus-ring: #38bdf8;" in theme_rule
        assert "--django-ray-retry-disabled-bg: #3f4148;" in theme_rule
        assert "--django-ray-retry-disabled-border: #71717a;" in theme_rule
        assert "--django-ray-retry-disabled-fg: #e4e4e7;" in theme_rule

    reduced_motion = stylesheet[stylesheet.index("@media (prefers-reduced-motion: reduce)") :]
    reduced_base_rule = _css_rule_body(reduced_motion, retry_selector)
    assert "transition: none;" in reduced_base_rule
    reduced_interaction_rule = _css_rule_body(
        reduced_motion,
        rf"{retry_selector}:hover:not\(:disabled\)\s*,\s*"
        rf"{retry_selector}:active:not\(:disabled\)",
    )
    assert "transform: none;" in reduced_interaction_rule


def test_cryptography_is_an_unconditional_runtime_dependency() -> None:
    """RuntimeEnv encryption must not rely on a transitive or optional dependency."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    assert project["dependencies"].count("cryptography>=42.0.8") == 1
    assert all(
        all("cryptography" not in requirement for requirement in requirements)
        for requirements in project["optional-dependencies"].values()
    )


def test_pyasn1_security_floor_is_an_unconditional_runtime_dependency() -> None:
    """Mandatory Ray extras must not resolve the known-vulnerable pyasn1 range."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    assert project["dependencies"].count("pyasn1>=0.6.4") == 1
    assert config["dependency-groups"]["dev"].count("pip-audit==2.10.1") == 1
    assert all(
        all("pyasn1" not in requirement for requirement in requirements)
        for requirements in project["optional-dependencies"].values()
    )

    lock = tomllib.loads((project_root / "uv.lock").read_text(encoding="utf-8"))
    locked_versions = [
        package["version"] for package in lock["package"] if package["name"] == "pyasn1"
    ]
    assert locked_versions == ["0.6.4"]


def test_ray_security_floor_is_an_unconditional_runtime_dependency() -> None:
    """Fresh installs must not resolve Ray releases below upstream security fixes."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    assert project["dependencies"].count("ray[default]>=2.56.0") == 1
    assert all(
        all(not requirement.startswith("ray[") for requirement in requirements)
        for requirements in project["optional-dependencies"].values()
    )

    lock = tomllib.loads((project_root / "uv.lock").read_text(encoding="utf-8"))
    locked_versions = [
        Version(package["version"]) for package in lock["package"] if package["name"] == "ray"
    ]
    assert len(locked_versions) == 1
    assert locked_versions[0] >= Version("2.56.0")


def test_ray_security_floor_is_consistent_across_public_install_paths() -> None:
    """Docs and the default Ray image must not bypass the package security floor."""
    project_root = Path(__file__).parents[2]
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    ray_dockerfile = (project_root / "Dockerfile.ray").read_text(encoding="utf-8")

    assert "Ray 2.56.0+" in readme
    assert "Ray 2.53.0+" not in readme
    assert "ARG RAY_VERSION=2.56.0" in ray_dockerfile
    assert "ARG RAY_VERSION=2.53.0" not in ray_dockerfile
