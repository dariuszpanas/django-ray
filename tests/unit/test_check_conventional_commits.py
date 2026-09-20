from __future__ import annotations

import tomllib
from pathlib import Path

from scripts import check_commits

ROOT = Path(__file__).parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_commit_tools_are_pinned_and_node_assets_are_retired() -> None:
    assert check_commits.YAGA_COMMAND[:6] == (
        "uvx",
        "--from",
        "yaga-cli==0.1.2",
        "--with",
        "typos==1.50.2",
        "yaga",
    )
    assert all(
        not (ROOT / name).exists()
        for name in (
            "package.json",
            "package-lock.json",
            ".node-version",
            ".npmrc",
            "commitlint.config.mjs",
            "commitlint-title.config.mjs",
            "tests/commit-policy.test.mjs",
            ".github/workflows/yaga-commit-qualification.yml",
        )
    )
    assert "package-ecosystem: npm" not in _read(".github/dependabot.yml")


def test_explicit_consumer_policy_preserves_structural_boundaries() -> None:
    policy = tomllib.loads(_read(".yaga.toml"))["commit"]
    assert policy["header-max-length"] == policy["body-max-line-length"] == 72
    assert policy["description-min-length"] == 10
    assert policy["body-policy"] == "required" and policy["body-min-length"] == 100
    assert policy["footer-max-line-length"] == 100
    assert policy["required-colon-footer-tokens"] == ["Validation"]
    assert policy["length-unit"] == "utf16"
    assert policy["footer-syntax"] == "colon-whitespace"
    assert policy["line-length-urls"] == "exempt"
    assert policy["typos"] == "check" and policy["typos-config"] == "isolated"
    assert policy["dependabot-pull-requests"] == "skip"
    assert not policy.get("skip-pull-request-authors")


def test_commit_template_teaches_the_enforced_policy() -> None:
    template = _read(".gitmessage")
    lines = template.splitlines()
    guidance = " ".join(line.removeprefix(";").strip() for line in lines[1:])

    assert lines[0] == "<type>[optional scope][!]: <imperative summary>"
    assert "summary of at least 10 characters" in guidance
    assert "body is required" in guidance
    assert "at least 100 characters" in guidance
    assert "wrap prose at 72 columns" in guidance
    assert "; Validation: `uv run make check` passed." in template
    assert "; Validation: not run because this changes documentation only." in template
    assert "BREAKING CHANGE: <impact and migration>" in template
    assert "Do not assemble prose with repeated `-m` flags." in guidance
    assert all(not line or index == 0 or line.startswith(";") for index, line in enumerate(lines))


def test_hook_uses_explicit_editor_input_without_mutating_the_message() -> None:
    hook = _read(".githooks/commit-msg")
    assert hook.startswith("#!/bin/sh\nset -eu\n")
    assert "git rev-parse --show-toplevel" in hook
    assert 'exec uv run --no-sync python scripts/check_commits.py edit "$1"' in hook
    assert "command -v uv" in hook
    assert ".githooks/* text eol=lf" in _read(".gitattributes")


def test_make_targets_share_the_pinned_adapter() -> None:
    makefile = _read("Makefile")
    assert 'git config --worktree core.hooksPath "$(CURDIR)/.githooks"' in makefile
    assert 'git config --worktree core.commentChar ";"' in makefile
    assert (
        'python scripts/check_commits.py range --base "$(COMMIT_BASE)" --head "$(COMMIT_HEAD)"'
        in makefile
    )
    assert "python scripts/check_commits.py title" in makefile
    assert "python -m scripts.check_commit_policy" in makefile
    assert makefile.count("$(MAKE) commit-policy-test") >= 2
    assert "npm" not in makefile


def test_branch_policy_fixtures_run_after_python_dependencies_in_ci() -> None:
    workflow = _read(".github/workflows/ci.yml")
    assert "pull_request:" in workflow
    assert "if: github.event_name != 'pull_request_target'" in workflow
    assert workflow.index("run: uv sync --frozen") < workflow.index(
        "run: uv run make commit-policy-test"
    )
    assert "actions/setup-node@" not in workflow


def test_linux_image_warms_offline_tools_after_cache_cleanup() -> None:
    image = _read("testing/linux/Dockerfile")
    assert "COPY --from=uv /uv /uvx /usr/local/bin/" in image
    assert "NODE_IMAGE" not in image and "npm ci" not in image
    assert image.index("uv cache clean") < image.index("uvx --from yaga-cli==0.1.2")
    assert image.index("uvx --from typos==1.50.2") < image.index("image_support.py record")
    assert "ENV UV_NO_SYNC=1 UV_OFFLINE=1" in image
    workflow = _read(".github/workflows/ci.yml")
    assert 'export UV_CACHE_DIR="$RUNNER_TEMP/policy-cache-runtime"' in workflow
    assert "export UV_OFFLINE=1" in workflow
    assert "uv run --no-sync make commit-policy-test spelling-check" in workflow
