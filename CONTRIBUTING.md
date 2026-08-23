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

Two required review checks apply the repository's conditional review policy. `Maintainer Approval`
passes the owner's own pull requests without self-approval; a pull request from any other author
requires an `APPROVED` review by `dariuszpanas` on the current head commit. `Codex Review` requires a
trusted Codex outcome for the exact current candidate. Both publishers recheck the live head and base
before passing. A synchronized head needs a new commit-bound Codex outcome. A base change remains
pending until the pull request moves to a new head because GitHub's connector evidence does not bind
the reviewed base. Strict required-status freshness prevents a default-branch advance from reusing an
older candidate's result.

Review events cross an explicit privilege boundary. The shared unprivileged `Review Policy Event`
workflow observes lifecycle and review events with no token permissions, checkout, artifact, or
pull-request code execution. Its versioned run-name JSON records bounded head, base, previous-head,
action, and event-time data. The default-branch `Maintainer Approval Publisher` validates that schema
and publishes the maintainer policy, while the separate `YAGA Publisher` executes one audited,
immutable YAGA commit. Review event observation is nonblocking; trusted publishers own the required
states.

For every affected current, displaced, or closed head, the maintainer publisher first replaces the
prior `Maintainer Approval` context with `pending`. It then validates the source workflow path,
repository, event, head, and unique open pull-request association before publishing the fresh live
policy. Approval, dismissal, reapproval, and unrelated reviews update one required status instead of
creating ambiguous same-named checks. Synchronize-only displaced-head recovery and current-head
evaluation remain independent, head-serialized jobs. A close immediately reevaluates a remaining
unique same-head owner; if no owner remains, bounded association and capacity checks allow the stale
pending state to return to terminal success. Ambiguous or uncorroborated ownership remains pending.
The maintainer publisher trusts no upstream artifact or pull-request code and checks out only the
exact default-branch commit bound to its dispatch.

YAGA observes Codex reviews; it never requests one, posts a comment, or adds or removes reactions.
Configure Codex automatic reviews in repository settings, or have an owner deliberately post exactly
`@codex review` when a review is needed. An owner request is only a reconciliation wake-up; the
publisher does not create it. YAGA accepts these bounded connector outcomes:

- a clean connector issue comment from the official GitHub App with a reviewed-commit marker that
  resolves to the current head;
- a formal connector findings review whose native commit ID and reviewed-commit marker identify the
  current head; or
- the official connector's `+1` reaction on the initial ready `opened` candidate.

A pull-request-body reaction has no commit or base identifier, so YAGA never reuses it for a later
synchronized head. Reaction-only later reviews and draft-to-ready reviews remain pending until Codex
emits commit-bound evidence. GitHub schedules may be delayed or dropped. The thirty-minute schedule
is a bounded repair path for reaction-only initial clean reviews and missed event delivery, not a
completion-time guarantee. Connector comments and formal reviews wake reconciliation immediately.
Uncertainty fails closed as `pending`.

Each scheduled pass scans at most 40 open pull requests and writes `pending` for every detected lost,
newer, or base-mismatched boundary before selecting a rotating window of at most four already-pending
candidates for terminal reconciliation. The shipped limits model scheduled work at no more than 710
GitHub REST requests per hour: two 163-request repair passes plus four 48-request terminal jobs per
pass. This is not a total repository guarantee under arbitrary event traffic; API or rate-limit
uncertainty leaves an already-observed boundary pending. The bounded beta repair pass rejects a
repository with more than 40 open pull requests.

YAGA v1 requires `Codex Review` to be an up-to-date/strict required status. Merge queues are
unsupported in v1: the shipped workflows have no `merge_group` trigger or combined-head contract.
Scheduled repair is defense in depth for missed observer delivery and shared heads, not a substitute
for strict branch currency.

GitHub commit-status writes are not transactional. YAGA reserves enough local request budget and
wall-clock window before publishing success to run its bounded validation and compensating-pending
tail. The shipped 15-minute terminal jobs run YAGA as their first and only step, declare that same
window through `job-timeout-minutes`, and reserve an additional cleanup margin. The REST timeout
bounds socket inactivity rather than total response time, and runner termination remains external to
the process. If GitHub accepts success and a network, rate-limit, trickled-response, or runner outage
then rejects or prevents every repair write, that status can remain ambiguous until a later event or
scheduled repair succeeds. A new pull request can likewise briefly inherit status on a reused SHA
before its asynchronous observer boundary is published. Privileged merge automation must wait for
the current PR-specific YAGA boundary instead of acting on inherited green immediately.

The repository token writes statuses as the shared GitHub Actions integration. Reserve the exact
`Codex Review` context and every case-insensitive alias, and reject any colliding workflow, job, check,
or other status publisher before making it required.

The shared observer includes `closed` because Maintainer Approval needs immediate shared-head
recovery. The Codex adapter validates that event and cleanly performs no status or review write after
close; scheduled reconciliation may settle a newly unique remaining same-head owner on its next
bounded pass. GitHub's native required review-conversation resolution remains enabled separately, so
a completed Codex review with findings cannot merge until every actionable thread is answered and
resolved. YAGA does not reinterpret review completion as approval.

## Rebase auto-merge

The repository uses pull requests for every change, but merges should preserve the descriptive commits
inside the PR. Do not squash a PR. After CI is green, enable auto-merge with the rebase method:

```bash
gh pr merge --auto --rebase <PR-number>
```

Auto-merge waits for `Commit Messages`, `CI Gate`, `Maintainer Approval`, and `Codex Review`, plus
GitHub's native required review-conversation resolution, and then applies the rebase merge method so
each descriptive commit remains visible on `main`. The `Commit Messages` workflow validates the PR
title and ordinary PR commits from a base-branch checkout with read-only repository permission. Its
title-only Dependabot path is limited by trusted `pull_request_target` event metadata to the bot's
same-repository branch namespace. `CI Gate` runs with `always()` and rejects failed, cancelled,
timed-out, or skipped blocking jobs, including a package build skipped after an upstream failure.
The Codex publisher executes only the immutable YAGA action with bounded permissions and no checkout.
Review events execute only the unprivileged shared observer; the separate default-branch publishers
alone receive permission to update commit statuses. Maintainer status jobs invalidate the old state
before checking out trusted default-branch code, while YAGA writes only through its head-serialized
publisher jobs. Neither path executes pull-request code with a write token.

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
conflicts, rerun the affected checks, re-evaluate the full-gate triggers below, and push. Repository
Codex settings or an owner request must produce a fresh exact-head outcome; YAGA evaluates it without
creating another request. Auto-merge waits for the new head's checks. The
merge policy's steady state requires
`Commit Messages`, `CI Gate`, `Maintainer Approval`, and `Codex Review` from GitHub Actions, requires
native review conversation resolution, and permits rebase merges only. Activate a new required
context only after its workflow is merged and a ready canary pull request reports that exact context
successfully; GitHub does not retroactively create a newly required check. Staged ruleset activation
must also enable strict required-status freshness so a changed base cannot reuse results from an
older candidate. An owner-authored canary proves the contexts but does not exercise the
review-submission trigger. Treat rollout as incomplete until an external or bot-authored canary is
available and proves that approval, dismissal, reapproval, synchronization, and an unrelated
`COMMENTED` review replace the single current-head `Maintainer Approval` status without leaving a
second required result. Existing pull requests with legacy same-named check runs must move to a new
head before serving as that canary. The native approval count remains zero so the owner is not forced
to self-approve; `Maintainer Approval` supplies the conditional rule that requires the owner's
current-head approval for every other author. The current owner
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
