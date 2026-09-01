# Contributing to django-ray

Thank you for contributing. The full development, testing, documentation, and release guide is in
[`docs/contributing.md`](docs/contributing.md). The conventions below apply to human contributors and
automated agents alike.

Suspected security vulnerabilities do not belong in public issues or pull requests. Follow
[`SECURITY.md`](SECURITY.md) and use its private reporting channel without publishing reproduction
details, exploit instructions, credentials, or secrets.

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
<type>[optional scope][!]: <imperative summary>
```

Use one of `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, or
`test`. Keep commits focused and use `!` for an intentional breaking change; add a
`BREAKING CHANGE:` footer when the history needs additional migration detail.

For a material change, treat each retained logical commit as the portable, PR-grade change record.
Its body must stand on its own in `git log`, mirrors, archives, changelog tooling, and other systems
without GitHub metadata. Explain the observable behavior and why it is needed. Add important
boundaries, compatibility or rollout impact, and useful repository-local investigation paths when
they materially help a future reader. One large atomic commit is valid; use proportional detail for
small mechanical changes and keep unrelated changes in separate logical commits.

The tracked commitlint configuration is the authoritative structural policy. It requires a
Conventional Commit header no longer than 72 characters with a summary of at least 10 characters, a
blank line, and a descriptive body of at least 100 characters excluding footers. Wrap body prose at
72 characters; footers use commitlint's 100-character limit. End every retained commit
with a `Validation:` trailer that records exact commands and results or a specific reason validation
was not run. PR titles use the same Conventional Commit header policy but do not require a body or
trailer. Imperative wording and useful historical context remain writing guidance rather than
semantic guesses made by the linter.

A compact commit can look like this:

```text
<type>[optional scope][!]: <imperative summary>

Explain the observable change and why it is needed. Include only the
boundaries, rollout details, or investigation paths that materially help.

Validation: `<command>` passed.
```

Install dependencies and configure the tracked template and local `commit-msg` hook for this
checkout or worktree:

```bash
make install

# If dependencies are already installed:
make configure-git
```

The tracked hook validates the final message with the same commitlint policy used by the local range
check and hosted required check. Compose the message in the configured editor with `git commit`, or
prepare a complete message file and use `git commit --file <path>`. Do not assemble prose with
repeated `-m` flags, which create separate paragraphs, and do not bypass the hook with `--no-verify`.

Examples:

| Change | Commit or PR title |
|---|---|
| Feature | `feat: add runtime environment cache metrics` |
| Bug fix | `fix(worker): preserve completion during timeout cancellation` |
| Documentation | `docs: clarify Ray Job worker selection` |
| Maintenance | `chore(deps): update Ruff` |

A PR should explain the problem and approach, call out migrations or persisted-protocol changes, list
validation results, and link the issue with `Closes #<number>` when appropriate. PR descriptions and
issue trailers are supplemental: they must not be the only place durable commit context exists. Keep
the material facts aligned between the PR and every retained logical commit, but format each surface
independently. PR descriptions use natural Markdown rather than 72-column hard wrapping. Use as many
logical commits as the change needs; one large atomic commit is valid, while unrelated cleanup belongs
in a separate change.

The required `Commit Messages` GitHub Actions check validates the PR title and, for ordinary pull
requests, the full message of every commit. Trusted same-repository Dependabot pull requests keep the
required check but validate only their Conventional Commit PR title; the separate required `CI Gate`
still validates their complete change. `CI Gate` fails unless lint, docs, typing,
supported-Python tests, PostgreSQL, live-cluster faults, testproject, the tracked Docker Compose
smoke, minimum/latest dependencies, and package build all succeed. The hosted commit check runs the
same authoritative commitlint configuration as the tracked local hook and `make commit-check`.

## Rebase auto-merge

The repository uses pull requests for every change, but merges should preserve the descriptive commits
inside the PR. Do not squash a PR. After CI is green, enable auto-merge with the rebase method:

```bash
gh pr merge --auto --rebase <PR-number>
```

Auto-merge waits for `Commit Messages` and `CI Gate`. Approval and review-conversation state do not
block merges in this single-maintainer repository. The rebase method preserves each descriptive
commit on `main`. The `Commit Messages` workflow validates the PR title and ordinary PR commits from
a base-branch checkout with read-only repository permission. Its title-only Dependabot path is
limited by trusted `pull_request_target` event metadata to the bot's same-repository branch namespace.
Native `CI Gate` runs with `always()` and rejects failed, cancelled, timed-out, or skipped blocking
jobs, including a package build skipped after an upstream failure.

Native Compiled Graph validation is not run on public GitHub-hosted runners; use the guarded local
KubeRay pilot when issue #102 requires that evidence. Coverage-debt review and benchmark workflows,
post-merge/manual documentation builds, and tag/manual release workflows remain outside the PR merge
gate. PR-facing equivalents that protect correctness live in the blocking CI workflow; Codecov
upload is advisory within the otherwise blocking Python 3.12 job.

Before every push and again before enabling auto-merge, fetch and inspect the exact history that the
rebase merge will retain:

```bash
git fetch origin
git log --format=fuller origin/main..HEAD
make commit-check
PR_TITLE='feat: describe the pull request' make commit-title-check
```

For every material commit shown by that log, compare its body with the PR description before pushing
and again before enabling auto-merge. They should agree on observable behavior, important boundaries,
rollout or activation impact, validation, and useful investigation starting points. Semantic parity
does not require copied wording, identical headings, or identical wrapping.

Fold `fixup!`/`squash!` commits, CI repairs, review repairs, formatting-only follow-ups, and other
development iterations into the logical commit they correct. Use an interactive rebase when needed,
then push rewritten branches with `--force-with-lease`. Do not collapse genuinely independent changes:
retain each one as a focused commit with its own descriptive body and `Validation:` trailer. Validate
the final PR title separately before enabling auto-merge.

If an auto-merge PR becomes stale or conflicts, update the branch from the latest `main`, resolve
conflicts, rerun the affected checks, re-evaluate the full-gate triggers below, and push. Auto-merge
waits for the new head's checks. The merge policy requires `Commit Messages` and `CI Gate`, does not
require approvals or resolved review conversations, and permits rebase merges only. The current owner
`pull_request` bypass remains explicit break-glass recovery, not absolute enforcement against an
intentional owner bypass; ordinary merges remain gated and emergency use requires the explicit
`gh pr merge --admin --rebase <PR-number>` path.

Treat the bypass as break-glass recovery for a GitHub infrastructure failure, never as permission to
retain an invalid commit or skip local validation. Record the outage and urgency in the PR, validate
the exact range with `make commit-check`, validate the title with
`PR_TITLE='feat: describe the pull request' make commit-title-check`, run `uv run make ci`, and record
the results before using it. Afterward, verify the rebased `main` history and ruleset bypass event,
then open or link a follow-up incident. If the PR merge service itself is unavailable, export the
ruleset, temporarily change only the named owner bypass to `always`, make the smallest recovery,
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

Before ordinary pushes, run `uv run make check` plus the narrowest affected tests and applicable
schema, documentation, or packaging checks. Every push to an open PR receives the broad exact-head
hosted CI matrix. Record the commands and results in the retained commit and PR instead of treating
one broad command as the only valid evidence.

A PR changing executable package or runtime behavior must pass `uv run make ci` once before final
review or auto-merge. It is also required for release candidates, break-glass merges, dependency,
packaging, build, or CI-composition changes, and before a required local KubeRay gate. Later changes
limited to PR or commit metadata, documentation, or tests do not invalidate that result; focused
delta checks and green final-head hosted CI suffice. Package, dependency, and deployment metadata or
manifests are not exempt, and a runtime-affecting review repair re-evaluates the triggers. A PR
containing only exempt deltas does not require a local full gate. Current-head `CI Gate` is the final
broad merge proof.

When triggered, `uv run make ci` checks formatting, lint, types, the CI coverage floors, strict
documentation, and the package build for the current interpreter without modifying tracked files.
GitHub Actions additionally tests the supported Python and dependency-resolution matrix. Use
`uv run make format` or `uv run make fix` explicitly when files should be changed. Do not run another
local full gate merely because an exempt focused follow-up changed the commit hash; record the
passing checkpoint and the focused delta evidence instead.

Changes that cross the local deployment boundary also follow the
[local KubeRay final-gate trigger matrix](docs/deployment/local-kuberay-gate.md). Run a required gate
from a clean checkout after `uv run make ci`, choose `required` or `skip` for the cold-Ray step as the
matrix specifies, and retain a concise semantic validation summary in the material commit and PR.
The summary records the exact gate command and result, the explicit cold-Ray decision, the verified
source-tree match, and the relevant workload-readiness, authenticated API/task-smoke, and preservation
outcomes. For a recommended gate, record either the same passing summary or a specific reason it was
not run. The complete secret-free evidence block remains available as bounded runtime diagnostics;
do not paste its image IDs, pod hashes, cluster UIDs, checksums, or similar run-specific identifiers
into durable Git history by default. Retain an identifier only in a focused issue or PR comment or
diagnostic artifact when an investigation needs it, and explain how it will be used. Add the summary
to the retained commit by amending only its message, then prove the emitted `source_tree` still equals
`git rev-parse HEAD^{tree}` without recording the hash; any tree change requires a new run.
Deployment-independent documentation and policy changes remain outside this gate.

For faster iteration on documentation changes, run the strict documentation build directly:

```bash
make docs-build-strict
```

The local gate includes `real_ray` tests, which start an isolated local Ray runtime and do not require
the configured Kubernetes cluster. `live_cluster` tests are opt-in; follow `docs/contributing.md` when
a change needs them. Record exact commands and results in each retained material commit and the PR;
when a relevant check was not run, record a specific reason rather than a bare `not run` or `N/A`.

## Automated agents and shared project memory

Automated agents must follow [`AGENTS.md`](AGENTS.md), including instruction precedence, safe handling
of unrelated changes, explicit staging, and validation reporting.

If an ignored repository-local Obsidian vault is available, agents should use it for targeted context
retrieval and handoffs. Start from its entry point and current-workspace note, verify its source commit,
search only for relevant concepts, and write one task-local handoff. The vault never overrides source
or tests and must not contain secrets. Work must continue normally when Obsidian is unavailable.
