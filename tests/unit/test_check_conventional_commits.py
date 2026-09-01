from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_commitlint_runtime_and_lock_are_exactly_pinned() -> None:
    package = json.loads(_read("package.json"))
    lock = json.loads(_read("package-lock.json"))
    expected_dependencies = {
        "@commitlint/cli": "21.2.2",
        "@commitlint/config-conventional": "21.2.2",
    }

    assert package["private"] is True
    assert package["type"] == "module"
    assert package["engines"]["node"] == ">=22.12.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+\n?", _read(".node-version"))
    assert package["devDependencies"] == expected_dependencies
    assert package["scripts"] == {
        "test": "node --test tests/commit-policy.test.mjs",
        "commitlint": "commitlint --strict --verbose",
        "commitlint:edit": "commitlint --strict --edit",
        "commitlint:title": ("commitlint --strict --verbose --config commitlint-title.config.mjs"),
    }
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == expected_dependencies
    for dependency, version in expected_dependencies.items():
        locked = lock["packages"][f"node_modules/{dependency}"]
        assert locked["version"] == version
        assert locked["integrity"].startswith("sha512-")

    assert "engine-strict=true" in _read(".npmrc")
    assert "node_modules/" in _read(".gitignore").splitlines()


def test_commitlint_configs_separate_commit_and_title_policy() -> None:
    commit_config = _read("commitlint.config.mjs")
    title_config = _read("commitlint-title.config.mjs")
    compact_commit = _compact(commit_config)
    compact_title = _compact(title_config)

    assert 'extends:["@commitlint/config-conventional"]' in compact_commit
    assert "defaultIgnores:false" in compact_commit
    for rule in (
        '"header-max-length":[2,"always",72]',
        '"subject-empty":[2,"never"]',
        '"subject-min-length":[2,"always",10]',
        '"body-empty":[2,"never"]',
        '"body-leading-blank":[2,"always"]',
        '"body-max-line-length":[2,"always",72]',
        '"body-min-length":[2,"always",100]',
        '"footer-leading-blank":[2,"always"]',
        '"footer-max-line-length":[2,"always",100]',
        '"validation-trailer":[2,"always"]',
    ):
        assert rule in compact_commit

    assert "constvalidationTrailerRule=(parsed)=>" in compact_commit
    assert "parsed.footer?.split(/\\r?\\n/u)??[]" in compact_commit
    assert "/^Validation:\\s+\\S/u.test(line)" in compact_commit
    assert compact_commit.count('"validation-trailer":validationTrailerRule') == 1
    assert "plugins:[{rules:{" in compact_commit
    assert "trailer-exists" not in commit_config
    assert "breaking-change-exclamation-mark" not in commit_config

    assert 'import{titleRules}from"./commitlint.config.mjs"' in compact_title
    assert 'extends:["@commitlint/config-conventional"]' in compact_title
    assert "defaultIgnores:false" in compact_title
    assert "rules:titleRules" in compact_title
    assert "body-" not in title_config
    assert "validation-trailer" not in title_config


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


def test_hook_uses_the_locked_local_commitlint_installation() -> None:
    hook = _read(".githooks/commit-msg")

    assert hook.startswith("#!/bin/sh\nset -eu\n")
    assert "\r\n" not in hook
    assert "git rev-parse --show-toplevel" in hook
    assert "command -v npm" in hook
    assert "node_modules/@commitlint" in hook
    assert 'exec npm run --silent commitlint:edit -- "$1"' in hook
    assert "npx" not in hook
    assert "npm install" not in hook
    assert ".githooks/* text eol=lf" in _read(".gitattributes")


def test_make_targets_install_and_run_the_repository_policy() -> None:
    makefile = _read("Makefile")
    compact = " ".join(makefile.replace("\\\n", "").split())

    assert "install: uv sync npm ci --ignore-scripts $(MAKE) configure-git" in compact
    assert 'git config --worktree commit.template "$(CURDIR)/.gitmessage"' in makefile
    assert 'git config --worktree core.hooksPath "$(CURDIR)/.githooks"' in makefile
    assert "COMMIT_BASE ?= origin/main" in makefile
    assert "COMMIT_HEAD ?= HEAD" in makefile
    assert (
        'commit-check: npm run --silent commitlint -- --from "$(COMMIT_BASE)" '
        '--to "$(COMMIT_HEAD)" --git-log-args="--no-merges"'
    ) in compact
    assert 'commit-title-check: @node -e "const title = process.env.PR_TITLE;' in compact
    assert "process.stdout.write(title + '\\n');" in makefile
    assert "| npm run --silent commitlint:title" in makefile
    assert 'test -n "$$PR_TITLE"' not in makefile
    assert "printf '%s\\n' \"$$PR_TITLE\"" not in makefile
    assert "commit-policy-test: npm test --silent" in compact
    assert makefile.count("$(MAKE) commit-policy-test") >= 2


def test_workflow_uses_trusted_code_and_pinned_node_installation() -> None:
    workflow = _read(".github/workflows/commit-messages.yml")

    assert "pull_request_target:" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "contents: write" not in workflow
    assert "pull-requests: write" not in workflow
    assert re.search(r"uses: actions/checkout@[0-9a-f]{40}(?:\s|#)", workflow)
    assert "fetch-depth: 0" in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert re.search(r"uses: actions/setup-node@[0-9a-f]{40}(?:\s|#)", workflow)
    assert "node-version-file: .node-version" in workflow
    assert "cache: npm" in workflow
    assert "run: npm ci --ignore-scripts" in workflow

    setup = workflow.index("uses: actions/setup-node@")
    install = workflow.index("run: npm ci --ignore-scripts")
    title = workflow.index("run: make commit-title-check")
    assert setup < install < title


def test_pull_request_ci_tests_the_branch_owned_commit_policy() -> None:
    trusted_workflow = _read(".github/workflows/commit-messages.yml")
    ci_workflow = _read(".github/workflows/ci.yml")
    setup_node_pattern = r"uses: actions/setup-node@([0-9a-f]{40})(?:\s|#)"
    trusted_setup = re.search(setup_node_pattern, trusted_workflow)
    ci_setup = re.search(setup_node_pattern, ci_workflow)

    assert trusted_setup is not None
    assert ci_setup is not None
    assert ci_setup.group(1) == trusted_setup.group(1)
    assert "pull_request:" in ci_workflow
    assert "if: github.event_name != 'pull_request_target'" in ci_workflow
    assert re.search(r"uses: actions/checkout@[0-9a-f]{40}(?:\s|#)", ci_workflow)
    assert "node-version-file: .node-version" in ci_workflow
    assert "cache: npm" in ci_workflow
    assert "run: npm ci --ignore-scripts" in ci_workflow
    assert "run: make commit-policy-test" in ci_workflow

    setup = ci_workflow.index("uses: actions/setup-node@")
    install = ci_workflow.index("run: npm ci --ignore-scripts")
    policy_test = ci_workflow.index("run: make commit-policy-test")
    assert setup < install < policy_test


def test_workflow_verifies_the_exact_head_before_range_lint() -> None:
    workflow = _read(".github/workflows/commit-messages.yml")

    assert "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in workflow
    assert '"refs/pull/${PR_NUMBER}/head:${pr_ref}"' in workflow
    assert 'fetched_head="$(git rev-parse "$pr_ref")"' in workflow
    assert 'if [ "$fetched_head" != "$PR_HEAD_SHA" ]; then' in workflow
    assert 'git cat-file -e "${PR_BASE_SHA}^{commit}"' in workflow
    assert "COMMIT_BASE: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "COMMIT_HEAD: refs/remotes/pull/${{ github.event.pull_request.number }}/head" in workflow

    fetch = workflow.index('"refs/pull/${PR_NUMBER}/head:${pr_ref}"')
    resolve = workflow.index('fetched_head="$(git rev-parse "$pr_ref")"')
    compare = workflow.index('if [ "$fetched_head" != "$PR_HEAD_SHA" ]; then')
    verify_base = workflow.index('git cat-file -e "${PR_BASE_SHA}^{commit}"')
    range_lint = workflow.index("run: make commit-check")
    assert fetch < resolve < compare < verify_base < range_lint


def test_workflow_skips_message_lint_only_for_exact_trusted_dependabot_prs() -> None:
    workflow = _read(".github/workflows/commit-messages.yml")
    compact = _compact(workflow)
    trusted_dependabot_negation = (
        "!("
        "github.event.pull_request.user.login=='dependabot[bot]'&&"
        "github.event.pull_request.head.repo.full_name==github.repository&&"
        "startsWith(github.event.pull_request.head.ref,'dependabot/')"
        ")"
    )

    assert compact.count(trusted_dependabot_negation) == 2
    title_step = workflow[
        workflow.index("- name: Validate PR title") : workflow.index(
            "- name: Fetch and verify PR commits"
        )
    ]
    fetch_step = workflow[
        workflow.index("- name: Fetch and verify PR commits") : workflow.index(
            "- name: Validate ordinary PR commits"
        )
    ]
    ordinary_step = workflow[workflow.index("- name: Validate ordinary PR commits") :]
    assert trusted_dependabot_negation in _compact(title_step)
    assert "run: make commit-title-check" in title_step
    assert "\n        if:" not in fetch_step
    assert trusted_dependabot_negation in _compact(ordinary_step)
    assert "run: make commit-check" in ordinary_step
    assert workflow.count("run: make commit-title-check") == 1
    assert workflow.index("run: make commit-title-check") < workflow.index(
        "- name: Validate ordinary PR commits"
    )
