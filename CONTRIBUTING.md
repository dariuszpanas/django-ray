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
<type>[optional scope]: <imperative summary>
```

Common types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, and `chore`. Keep
commits focused and use `!` for an intentional breaking change; add a `BREAKING CHANGE:` footer when
the history needs additional migration detail.
For a material change, treat each retained logical commit as the portable, PR-grade change record.
Its body must stand on its own in `git log`, mirrors, archives, changelog tooling, and other systems
without GitHub metadata. Record the observable behavior and motivation; important invariants,
boundaries, and non-goals; compatibility, migration, rollout, or activation details when applicable;
exact validation or a specific reason it was not run; and repository-local documentation, ADRs,
modules, migrations, or tests that are useful investigation starting points. One large atomic commit
is valid. Use proportional detail for small mechanical changes and keep unrelated changes in separate
logical commits.

The tracked template recommends this useful layout:

```text
<type>[optional scope][!]: <imperative summary>

## Summary

- Describe the observable durable change and why it is needed.

## Boundaries and rollout

- Record important invariants, non-goals, and applicable rollout impact.

## Investigation

- Point to useful repository-local ADRs, modules, migrations, tests, or docs.

## Validation

- `<command>`: result
```

The headings are guidance, not a required format. Unstructured prose is equally valid when it
provides the same durable context. The gate requires at least eight body words outside headings,
validation evidence, generated metadata, and trailers, plus a specific validation result or an
explicit non-placeholder reason validation was not run. It rejects template placeholders,
development-only prose such as "address review feedback," bodies that merely repeat the header, and
commands with no recorded result. A non-empty `BREAKING CHANGE:` footer may follow the descriptive
body; `!` in the header is sufficient to mark the change as breaking. Wrap ordinary **commit prose**
at 72 characters so history remains readable in narrow terminals. This wrapping rule does not apply
to PR descriptions, which use natural Markdown without artificial hard wrapping. Structurally
validated generated dependency headers and metadata, URL destinations, complete Markdown tables,
and recognized Git trailers are not required to wrap. The required hosted check validates only the
PR title for trusted Dependabot pull requests because GitHub controls their generated commit bodies
and does not provide body-formatting options. This exception requires the exact `dependabot[bot]`
author, a same-repository head, and a `dependabot/*` branch; all other pull requests retain full
commit-message validation.

Install the tracked template for this checkout:

```bash
git config extensions.worktreeConfig true
git config --worktree commit.template "$(git rev-parse --show-toplevel)/.gitmessage"
git config --worktree core.commentChar ";"
```

Worktree-specific configuration keeps each linked worktree pointed at its own tracked template. The
comment-character setting is required: Git otherwise treats the `##` section headings as comments and
removes them when the editor closes. Template guidance uses `;` comments, while its optional Markdown
headings remain available in the commit message.

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
smoke, minimum/latest dependencies, and package build all succeed. Use one of `build`, `chore`, `ci`,
`docs`, `feat`, `fix`, `perf`,
`refactor`,
`revert`, `style`, or `test`, optionally add a scope and `!`, and include a summary after `:`. The
check enforces meaningful body context, validation evidence or a specific not-run reason, and the
wrappable commit-prose line limit without prescribing section headings. A failed check prints the
offending title, commit, or line and the expected correction.

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
uv run python scripts/check_conventional_commits.py --range origin/main..HEAD
```

For every material commit shown by that log, compare its body with the PR description before pushing
and again before enabling auto-merge. They should agree on observable behavior, important boundaries,
rollout or activation impact, validation, and useful investigation starting points. Semantic parity
does not require copied wording, identical headings, or identical wrapping.

Fold `fixup!`/`squash!` commits, CI repairs, review repairs, formatting-only follow-ups, and other
development iterations into the logical commit they correct. Use an interactive rebase when needed,
then push rewritten branches with `--force-with-lease`. Do not collapse genuinely independent changes:
retain each one as a focused commit with its own descriptive body. Run the validator with the final
PR title as well before enabling auto-merge.

If an auto-merge PR becomes stale or conflicts, update the branch from the latest `main`, resolve
conflicts, rerun the affected checks, re-evaluate the full-gate triggers below, and push. Auto-merge
waits for the new head's checks. The merge policy requires `Commit Messages` and `CI Gate`, does not
require approvals or resolved review conversations, and permits rebase merges only. The current owner
`pull_request` bypass remains explicit break-glass recovery, not absolute enforcement against an
intentional owner bypass; ordinary merges remain gated and emergency use requires the explicit
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
