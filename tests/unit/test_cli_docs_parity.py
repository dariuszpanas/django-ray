"""Contracts keeping CLI flags, admonitions, and operational docs accurate."""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from django_ray.management.commands.django_ray_lease_policy import (
    Command as LeasePolicyCommand,
)

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

GFM_ALERT_RE = re.compile(
    r"^\s*>\s*\[!(?:TIP|IMPORTANT|NOTE|WARNING|CAUTION)\]",
    re.MULTILINE | re.IGNORECASE,
)
BARE_CLUSTER_CODE_RE = re.compile(r"`--cluster`")
LEASE_POLICY_CMD_RE = re.compile(
    r"python\s+(?:testproject/)?manage\.py\s+django_ray_lease_policy([^\n`]*)",
)
WORKER_CMD_RE = re.compile(
    r"python\s+(?:testproject/)?manage\.py\s+django_ray_worker([^\n`]*)",
)
KUBECTL_EXEC_MANAGE_RE = re.compile(
    r"kubectl\s+exec\b[^\n]*python\s+(?:testproject/)?manage\.py\b",
)
FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
ADMONITION_OPENER_RE = re.compile(r"^\s*(?:!!!|\?\?\?\+?)(?:\s|$)")
SINGLE_QUOTED_ADMONITION_RE = re.compile(r"^\s*(?:!!!|\?\?\?\+?)\s+\S+(?:\s+[\w-]+)*\s+'")
VALID_ADMONITION_HEADER_RE = re.compile(
    r'^\s*(?:!!!|\?\?\?\+?)\s+[\w-]+(?:\s+[\w-]+)*(?:\s+"[^"]*")?\s*$'
)


def _all_doc_paths() -> list[Path]:
    paths = sorted(DOCS.rglob("*.md"))
    paths.extend([ROOT / "README.md", ROOT / "llms.txt", DOCS / "llms.txt"])
    return paths


def _join_shell_continuations(text: str) -> str:
    return re.sub(r"\\\r?\n\s*", " ", text)


def test_docs_and_llms_contain_no_nonexistent_execution_mode_flag() -> None:
    for path in _all_doc_paths():
        content = path.read_text(encoding="utf-8")
        assert "--execution-mode" not in content, (
            f"{path.relative_to(ROOT)} references nonexistent --execution-mode flag"
        )

    for path in (DOCS / "configuration.md", ROOT / "llms.txt", DOCS / "llms.txt"):
        assert "--ray-job" in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(ROOT)} must document the --ray-job flag"
        )


def test_docs_and_llms_use_cluster_flag_with_explicit_address() -> None:
    mode_reference_docs = (
        DOCS / "configuration.md",
        ROOT / "llms.txt",
        DOCS / "llms.txt",
        DOCS / "worker-modes.md",
        DOCS / "reference" / "cli.md",
    )
    for path in mode_reference_docs:
        content = path.read_text(encoding="utf-8")
        assert BARE_CLUSTER_CODE_RE.search(content) is None, (
            f"{path.relative_to(ROOT)} contains bare `--cluster` without `=ADDRESS`"
        )

    for path in _all_doc_paths():
        joined = _join_shell_continuations(path.read_text(encoding="utf-8"))
        for match in WORKER_CMD_RE.finditer(joined):
            tokens = shlex.split(match.group(1))
            for idx, token in enumerate(tokens):
                if token == "--cluster":
                    assert idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"), (
                        f"{path.relative_to(ROOT)} has bare --cluster in: {match.group(0)!r}"
                    )


def test_zensical_docs_use_admonitions_instead_of_gfm_alerts() -> None:
    for path in sorted(DOCS.rglob("*.md")):
        content = path.read_text(encoding="utf-8")
        match = GFM_ALERT_RE.search(content)
        assert match is None, (
            f"{path.relative_to(ROOT)} uses unsupported GFM alert {match.group(0)!r}"
        )

    config_doc = (DOCS / "configuration.md").read_text(encoding="utf-8")
    assert "!!! tip" in config_doc
    assert "!!! warning" in config_doc


def test_django_ray_lease_policy_examples_parse_and_satisfy_transition_contract() -> None:
    parser = LeasePolicyCommand().create_parser("manage.py", "django_ray_lease_policy")
    inspect_examples = 0
    mutating_examples = 0
    config_has_mutating_example = False

    for path in _all_doc_paths():
        joined = _join_shell_continuations(path.read_text(encoding="utf-8"))
        for match in LEASE_POLICY_CMD_RE.finditer(joined):
            raw_args = match.group(1).strip().rstrip(".,;:")
            tokens = shlex.split(raw_args) if raw_args else []
            # Replace placeholder tokens like <rev> with integer sample values for parsing.
            normalized_tokens = [re.sub(r"<[^>]+>", "1", token) for token in tokens]
            parsed = parser.parse_args(normalized_tokens)

            if parsed.set_seconds is None:
                inspect_examples += 1
                assert parsed.expected_seconds is None
                assert parsed.expected_revision is None
                assert parsed.all_lease_users_stopped is False
            else:
                mutating_examples += 1
                if path == DOCS / "configuration.md":
                    config_has_mutating_example = True
                assert parsed.expected_seconds is not None, (
                    f"{path.relative_to(ROOT)} omits --expected-seconds: {match.group(0)!r}"
                )
                assert parsed.expected_revision is not None, (
                    f"{path.relative_to(ROOT)} omits --expected-revision: {match.group(0)!r}"
                )
                assert parsed.all_lease_users_stopped is True, (
                    f"{path.relative_to(ROOT)} omits --all-lease-users-stopped: {match.group(0)!r}"
                )

    assert inspect_examples >= 1
    assert mutating_examples >= 2
    assert config_has_mutating_example


def test_kubernetes_docs_use_container_testproject_manage_py_path() -> None:
    k8s_doc = _join_shell_continuations(
        (DOCS / "deployment" / "kubernetes.md").read_text(encoding="utf-8")
    )
    invocations = KUBECTL_EXEC_MANAGE_RE.findall(k8s_doc)
    assert invocations, "Expected kubectl exec manage.py examples in docs/deployment/kubernetes.md"
    for invocation in invocations:
        assert "python testproject/manage.py" in invocation, (
            f"kubectl exec must invoke python testproject/manage.py, got: {invocation!r}"
        )


def test_docs_do_not_recommend_private_snapshot_local_runtime_env() -> None:
    for path in _all_doc_paths():
        content = path.read_text(encoding="utf-8")
        assert "snapshot_local_runtime_env" not in content, (
            f"{path.relative_to(ROOT)} recommends private ephemeral snapshot_local_runtime_env()"
        )


def test_zensical_enables_code_copy_and_search_suggest() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    features = config["project"]["theme"]["features"]
    assert "content.code.copy" in features
    assert "search.suggest" in features
    assert "search.highlight" in features


def test_docs_have_no_escaped_admonition_quotes_and_match_0029_upgrade_delta() -> None:
    for path in sorted(DOCS.rglob("*.md")):
        content = path.read_text(encoding="utf-8")
        assert r"\"disallow\"" not in content, (
            f"{path.relative_to(ROOT)} contains backslash-escaped quotes in admonition title"
        )

    upgrade_07 = (DOCS / "deployment" / "upgrading-from-0.7.md").read_text(encoding="utf-8")
    assert "0029_lease_engine_and_hot_indexes" in upgrade_07
    assert "no django-ray database migration delta from 0.7.1" not in upgrade_07.lower()

    compat = (DOCS / "compatibility.md").read_text(encoding="utf-8")
    assert "0029_lease_engine_and_hot_indexes" in compat
    assert "no migration delta after 0.7.1" not in compat.lower()


def test_docs_png_assets_have_valid_png_headers() -> None:
    png_signature = b"\x89PNG\r\n\x1a\n"
    landing_png = DOCS / "assets" / "images" / "testproject-landing.png"
    assert landing_png.is_file()
    for png_path in sorted((DOCS / "assets" / "images").glob("*.png")):
        header = png_path.read_bytes()[:8]
        assert header == png_signature, (
            f"{png_path.relative_to(ROOT)} does not start with a valid PNG header"
        )


def test_llms_txt_copies_are_byte_identical() -> None:
    assert (ROOT / "llms.txt").read_bytes() == (DOCS / "llms.txt").read_bytes()


def test_cli_reference_documents_unified_format_flags_and_jitter_direction() -> None:
    cli_doc = (DOCS / "reference" / "cli.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", cli_doc)
    by_heading = {section.splitlines()[0].strip(): section for section in sections[1:] if section}

    for command_name in (
        "django_ray_lease_policy",
        "django_ray_purge_executions",
        "django_ray_replay",
        "django_ray_protocol_status",
        "django_ray_benchmark_polling",
        "django_ray_purge_inputs",
        "django_ray_cleanup_workflow_progress",
        "django_ray_runtime_env",
        "django_ray_audit_workflow_progress",
        "django_ray_worker_ready",
        "django_ray_benchmark_workflow_progress",
    ):
        body = by_heading[command_name]
        assert "--format" in body, f"docs/reference/cli.md omits --format for {command_name}"
        assert "--json" in body, f"docs/reference/cli.md omits --json for {command_name}"

    assert "| `--format` | `text` |" in by_heading["django_ray_runtime_env"]

    for doc_path in (ROOT / "README.md", DOCS / "retry.md", DOCS / "reference" / "settings.md"):
        assert "±10%" not in doc_path.read_text(encoding="utf-8"), (
            f"{doc_path.relative_to(ROOT)} documents ±10% jitter instead of downward [-10%, 0%]"
        )


def test_runtime_env_encryption_at_rest_boundary_is_documented() -> None:
    security_md = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
    runtime_env_doc = " ".join(
        (DOCS / "runtime-environments.md").read_text(encoding="utf-8").split()
    )

    for text, label in ((security_md, "SECURITY.md"), (runtime_env_doc, "runtime-environments.md")):
        assert 'RUNTIME_ENV_STORAGE_MODE="encrypted"' in text, label
        assert "at rest" in text, label
        assert "env_vars" in text, label
        assert "Ray Dashboard" in text and "GCS" in text, label
        assert "TLS" in text, label
        assert "network isolation" in text, label


def test_ci_and_taxonomy_docs_match_current_workflows() -> None:
    taxonomy_doc = " ".join(
        (DOCS / "testing" / "test-suite-taxonomy.md").read_text(encoding="utf-8").split()
    )
    assert "The blocking Python 3.12 job now uses the manifest runner" not in taxonomy_doc
    assert "pyrn" in taxonomy_doc
    assert "Testproject & Tooling" in taxonomy_doc
    assert "tests/unit/test_admin_live_polling.py" in taxonomy_doc

    contributing_doc = " ".join((DOCS / "contributing.md").read_text(encoding="utf-8").split())
    assert (
        "Blocking CI and release workflows repeat it under every supported Python version"
        not in contributing_doc
    )
    assert "Python 3.12" in contributing_doc
    assert "`Testproject Smoke`" not in contributing_doc
    assert "`Testproject & Tooling`" in contributing_doc


def test_docs_admonitions_use_double_quoted_titles_only() -> None:
    """Reject single-quoted or malformed Zensical/Markdown admonition titles across docs."""
    for path in sorted(DOCS.rglob("*.md")):
        in_fence = False
        fence_char = ""
        fence_len = 0
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            fence_match = FENCE_LINE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if not in_fence:
                    in_fence = True
                    fence_char = marker[0]
                    fence_len = len(marker)
                elif marker[0] == fence_char and len(marker) >= fence_len:
                    in_fence = False
                continue
            if in_fence or not ADMONITION_OPENER_RE.match(line):
                continue

            rel = f"{path.relative_to(ROOT)}:{line_number}"
            assert not SINGLE_QUOTED_ADMONITION_RE.match(line), (
                f"{rel} uses single quotes in admonition title (Zensical requires double quotes): "
                f"{line!r}"
            )
            assert VALID_ADMONITION_HEADER_RE.match(line), (
                f"{rel} has malformed admonition header (expected double-quoted title with no "
                f"unescaped inner double quotes): {line!r}"
            )
