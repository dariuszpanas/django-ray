# Contributing to django-ray

Thank you for contributing. The full development, testing, documentation, and release guide is in
[`docs/contributing.md`](docs/contributing.md). The conventions below apply to human contributors and
automated agents alike.

## Branches

Create branches from an up-to-date `main`. Use lowercase kebab-case after one of these prefixes:

| Change | Branch | Example |
|---|---|---|
| Feature or enhancement | `feat/` | `feat/issues-25-33-contributor-qol` |
| Bug fix | `fix/` | `fix/ray-job-timeout-race` |
| Documentation only | `docs/` | `docs/worker-mode-selection` |
| Maintenance, tooling, or dependencies | `chore/` | `chore/ruff-upgrade` |
| Test-only change | `test/` | `test/worker-reconnect-coverage` |

`feat/` is the default for feature work. Repository conventions take precedence over generic tool
defaults, so automated agents must not substitute an unrelated `agent/`, `codex/`, or similar prefix.
Follow an explicit maintainer-requested branch name when one is provided.

## Commits and pull requests

Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages and PR titles:

```text
<type>[optional scope]: <imperative summary>
```

Common types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, and `chore`. Keep
commits focused and use `!` plus a `BREAKING CHANGE:` footer for an intentional breaking change.

Examples:

| Change | Commit or PR title |
|---|---|
| Feature | `feat: add runtime environment cache metrics` |
| Bug fix | `fix(worker): preserve completion during timeout cancellation` |
| Documentation | `docs: clarify Ray Job worker selection` |
| Maintenance | `chore(deps): update Ruff` |

A PR should explain the problem and approach, call out migrations or persisted-protocol changes, list
validation results, and link the issue with `Closes #<number>` when appropriate. Prefer a few focused
commits over unrelated cleanup in the same PR.

The required `Commit Messages` GitHub Actions check validates the PR title and every commit in the PR.
Use one of `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, or
`test`, optionally add a scope and `!`, and include a summary after `:`. A failed check prints the
offending title or commit and the expected format.

## Rebase auto-merge

The repository uses pull requests for every change, but merges should preserve the descriptive commits
inside the PR. Do not squash a PR. After CI is green, enable auto-merge with the rebase method:

```bash
gh pr merge --auto --rebase <PR-number>
```

Auto-merge waits for the required checks on the pull request and then applies the rebase merge method,
so each descriptive commit remains visible on `main`. The `Commit Messages` workflow validates the PR
title and each commit through the read-only `pull_request_target` event.

If an auto-merge PR becomes stale or conflicts, update the branch from the latest `main`, resolve
conflicts, run `uv run make ci`, and push. Auto-merge will wait for the new checks. A maintainer must
configure the `main` ruleset to require the `Commit Messages` check and CI checks, enable rebase
merging and auto-merge, and leave approval requirements disabled when the repository's sole-developer
workflow does not need them.

## Worktree and staging safety

Inspect the worktree before editing:

```bash
git status --short
git branch --show-current
git log -1 --oneline
```

Preserve unrelated changes. In a shared dirty worktree, use a separate worktree or coordinate with the
owner instead of moving or rewriting their files. Stage explicit paths, then inspect the staged diff:

```bash
git add AGENTS.md CONTRIBUTING.md docs/contributing.md
git diff --cached --check
git diff --cached
```

Do not use `git add .`, broad restore/reset commands, or an all-files formatter without confirming the
scope.

## Validation before a pull request

Run the narrowest relevant tests while developing. Before opening a PR, run the local CI-equivalent
checks:

```bash
uv run make ci
```

This command checks formatting, lint, types, the CI coverage floors, strict documentation, and the
package build for the current interpreter without modifying tracked files. GitHub Actions additionally
tests the supported Python and dependency-resolution matrix. Use `uv run make format` or
`uv run make fix` explicitly when files should be changed.

For faster iteration on documentation changes, run the strict documentation build directly:

```bash
make docs-build-strict
```

The local gate includes `real_ray` tests, which start an isolated local Ray runtime and do not require
the configured Kubernetes cluster. `live_cluster` tests are opt-in; follow `docs/contributing.md` when
a change needs them. Report exactly which commands passed and explain any checks that were not run.

## Automated agents and shared project memory

Automated agents must follow [`AGENTS.md`](AGENTS.md), including instruction precedence, safe handling
of unrelated changes, explicit staging, and validation reporting.

If an ignored repository-local Obsidian vault is available, agents should use it for targeted context
retrieval and handoffs. Start from its entry point and current-workspace note, verify its source commit,
search only for relevant concepts, and write one task-local handoff. The vault never overrides source
or tests and must not contain secrets. Work must continue normally when Obsidian is unavailable.
