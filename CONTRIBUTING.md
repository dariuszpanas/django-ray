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
commits focused and use `!` for an intentional breaking change; add a `BREAKING CHANGE:` footer when
the history needs additional migration detail.
Every commit must include enough descriptive body context to understand the retained change without
reconstructing intent from its diff. The tracked template recommends this useful layout:

```text
<type>[optional scope][!]: <imperative summary>

## Summary

- Describe the concrete durable change.
- Explain the problem, invariant, or outcome that motivates it.

## Validation

- `<command>`: result
```

The headings are guidance, not a required format. Unstructured prose is equally valid when it
provides meaningful context. The gate requires at least eight body words outside headings,
validation evidence, generated metadata, and trailers, or structurally complete generated
dependency context; rejects template placeholders, development-only prose such as "address
review feedback," and bodies that merely repeat the header; and requires a blank line after the
header. A non-empty `BREAKING CHANGE:` footer may follow the descriptive body; `!` in the header is
sufficient to mark the change as breaking. Wrap ordinary prose at 72 characters so history remains
readable in narrow terminals. Structurally validated generated dependency headers and metadata, URL
destinations, complete Markdown tables, and recognized Git trailers are not required to wrap. The
same content standard applies to human and automated commits, so Dependabot's descriptive generated
messages pass without a bot-wide bypass.

Install the tracked template for this checkout:

```bash
git config extensions.worktreeConfig true
git config --worktree commit.template "$(git rev-parse --show-toplevel)/.gitmessage"
git config --worktree core.commentChar ";"
```

Worktree-specific configuration keeps each linked worktree pointed at its own tracked template. The
comment-character setting is required: Git otherwise treats the `##` section headings as comments and
removes them when the editor closes. Template guidance uses `;` comments, while the required Markdown
headings remain in the commit message.

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

The required `Commit Messages` GitHub Actions check validates the PR title and the full message of
every commit in the PR. The separate required `CI Gate` check fails unless lint, docs, typing,
supported-Python tests, PostgreSQL, live-cluster faults, testproject, minimum/latest dependencies,
Compiled Graph candidates, and package build all succeed. Use one of `build`, `chore`, `ci`, `docs`,
`feat`, `fix`, `perf`, `refactor`,
`revert`, `style`, or `test`, optionally add a scope and `!`, and include a summary after `:`. The
check enforces meaningful body context and the wrappable-prose line limit without prescribing
section headings. A failed check prints the offending title, commit, or line and the expected
correction.

## Rebase auto-merge

The repository uses pull requests for every change, but merges should preserve the descriptive commits
inside the PR. Do not squash a PR. After CI is green, enable auto-merge with the rebase method:

```bash
gh pr merge --auto --rebase <PR-number>
```

Auto-merge waits for both required checks and then applies the rebase merge method, so each descriptive
commit remains visible on `main`. The `Commit Messages` workflow validates the PR title and each commit
through the read-only `pull_request_target` event. `CI Gate` runs with `always()` and rejects failed,
cancelled, timed-out, or skipped blocking jobs, including a package build skipped after an upstream
failure.

Scheduled/manual Compiled Graph canary and benchmark workflows, post-merge/manual documentation
builds, and tag/manual release workflows remain outside the PR merge gate. PR-facing equivalents that
protect correctness live in the blocking CI workflow; Codecov upload is advisory within the otherwise
blocking Python 3.12 job.

Before every push and again before enabling auto-merge, fetch and inspect the exact history that the
rebase merge will retain:

```bash
git fetch origin
git log --format=fuller origin/main..HEAD
uv run python scripts/check_conventional_commits.py --range origin/main..HEAD
```

Fold `fixup!`/`squash!` commits, CI repairs, review repairs, formatting-only follow-ups, and other
development iterations into the logical commit they correct. Use an interactive rebase when needed,
then push rewritten branches with `--force-with-lease`. Do not collapse genuinely independent changes:
retain each one as a focused commit with its own descriptive body. Run the validator with the final
PR title as well before enabling auto-merge.

If an auto-merge PR becomes stale or conflicts, update the branch from the latest `main`, resolve
conflicts, run `uv run make ci`, and push. Auto-merge will wait for the new checks. The `main`
ruleset requires `Commit Messages` and `CI Gate` from GitHub Actions, permits rebase merges only, and
leaves approval requirements disabled for the repository's sole-developer workflow. The owner bypass is
limited to pull requests: ordinary merges remain gated, and emergency use requires the explicit
`gh pr merge --admin --rebase <PR-number>` path.

Treat the bypass as break-glass recovery for a GitHub infrastructure failure, never as permission to
retain an invalid commit or skip local validation. Record the outage and urgency in the PR, validate
the exact range and title with `scripts/check_conventional_commits.py`, run `uv run make ci`, and
record both results before using it. Afterward, verify the rebased `main` history and ruleset bypass
event, then open or link a follow-up incident. If the PR merge service itself is unavailable, export
the ruleset, temporarily change only the named owner bypass to `always`, make the smallest recovery,
immediately restore `pull_request`, and verify the complete ruleset through the API. Never leave an
`always` or `exempt` bypass configured.

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
