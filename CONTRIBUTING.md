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

Maintainer review events cross an explicit privilege boundary. The unprivileged
`Review Policy Event` workflow still records lifecycle, review, close, and displaced-head data. It
feeds both Maintainer Approval and the inverse-gated YAGA v1 publisher during bootstrap; after v2
cutover, it remains temporary maintainer-only transport until that protocol receives a human-readable
replacement. It has no token permissions, checkout, artifact, or pull-request code execution. Its
versioned run-name JSON remains visible meanwhile. Review event observation is nonblocking; trusted
publishers own the required states. `Review Policy Boundary` remains nonblocking during the staged
YAGA v2 bootstrap. `CI Prerequisites` remains nonblocking during the staged YAGA v2 bootstrap.

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

YAGA v2 separates lifecycle invalidation from quota-consuming review requests. The trusted
`YAGA Review Policy` workflow publishes the exact current boundary without checking out pull-request
code. Unprivileged PR CI uses an exact bounded run title, ends at native `CI Prerequisites`, and
completes before the `YAGA Codex Review Publisher` may request or observe Codex. The publisher pins one
immutable YAGA commit. Failed CI publishes terminal gate errors and never requests a review. Both
authenticated workflow completions are reconcile wakes; YAGA deterministically elects only one
processor before any write, so their completion order cannot create duplicate requests.

The bootstrap is fail-safe when the repository variable `YAGA_CODEX_V2_ENABLED` is absent or not
exactly `true`: every v2 entry job skips and the pinned v1 jobs remain active behind the inverse
condition. CI still publishes native `CI Prerequisites`, but the compatibility bridge retains the
required native `CI Gate`. After the variables and protected environment are verified, setting the
flag to `true` skips v1, renames the bridge to non-colliding `Legacy CI Gate`, and makes YAGA the sole
publisher of classic `CI Gate`. A variable change does not revoke an already queued job: before
enabling or rolling back, freeze new pull requests and auto-merge, reach zero open pull requests,
cancel every in-progress v1 and v2 publisher run including jobs waiting on the protected environment,
and wait for both workflow queues and all outstanding Codex provider tasks to drain. Automatic Codex
reviews are disabled before v2 is activated, so YAGA is the sole legitimate automatic requester from
the first canary.
Change the flag only at that boundary, then open a fresh canary only after a second cancel-and-drain
check catches any v1 schedule or provider task that started during the transition. The fresh canary
lets v2 record its lifecycle event before CI can authorize anything. An emergency transition with an
open PR must keep its auto-merge disabled and move it to a new head after the flag change; a CI rerun
alone does not create the missing lifecycle boundary. A rollback must re-enable automatic reviews
after the drain or explicitly accept manual owner requests because v1 observes reviews but never
requests one. Remove both the flag and v1 workflow only after the complete canary.

Owner-authored PRs may receive one marked `@codex review` request after successful CI. Every other
author must first receive approval through the protected `codex-review-approval` environment. Only
its exact candidate-bound marker authorizes the separately serialized request worker. The immutable
owner ID is configured through `YAGA_CODEX_OWNER_ID`; the environment has Dariusz as sole reviewer,
prevents self-review and administrator bypass, contains no secrets, and exposes the exact
`YAGA_CODEX_APPROVAL_MARKER` environment variable. The required repository value is
`YAGA_CODEX_OWNER_ID=15094983`; the environment-only value is
`YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. Verify those exact values and every protection
setting before enabling v2. A direct human or app `@codex review` comment remains a provider-side
loophole: it can consume provider quota outside YAGA, and no repository gate can prevent provider
execution. Such a comment can race between YAGA's final provider-evidence read and request POST, so a
later provider outcome can appear temporally correlated even when another comment triggered it. If
trusted connector activity is already visible without the exact current-boundary Actions-owned YAGA
request, YAGA fails closed and does not post a duplicate. Protected external approval never
retroactively authorizes or reuses unsolicited activity; it authorizes only a strictly later
current-boundary YAGA request.

YAGA accepts these bounded connector outcomes:

- a clean connector issue comment from the official GitHub App with a reviewed-commit marker that
  resolves to the current head;
- a formal connector findings review whose native commit ID and reviewed-commit marker identify the
  current head; or
- the official connector's `+1` reaction on the pull-request body.

Every accepted eyes reaction or terminal outcome, including evidence for the ready `opened`
candidate, requires the exact current-boundary Actions-owned YAGA request and must be strictly after
that request. Same-second evidence is ambiguous and fails closed. A pull-request-body reaction has no
commit or base identifier. The request provides the only available temporal correlation, not native
provider binding; YAGA never accepts the reaction before or without that request. There is no
schedule, issue-comment, review, `closed`, or
`merge_group` publisher trigger. Bounded polling settles the current request; timeout or runner loss
leaves an explicit error or pending result. Rerun current CI after a temporary provider, API, or
runner failure. Capacity failures have stricter recovery: 100 or more comments, reviews, or reactions
make that evidence page incomplete and require a new pull request. Commit-status reads require fewer
than 100 visible statuses across all contexts and reserve the final two slots for terminal and repair
writes; approaching either status ceiling requires a new head, and another same-head CI rerun can
make recovery harder. Merge queues remain unsupported because the shipped workflows have no
combined-head contract.

GitHub comment and commit-status writes are not transactional. YAGA revalidates the exact source CI,
lifecycle boundary, live PR, unique head ownership, approval, provider evidence, and status lineage
around each authority-changing write. The 15-minute action-only jobs reserve bounded REST request,
wall-clock, terminal-error, and compensating-pending tails through `job-timeout-minutes`. A close can
race the final live read and comment/status POST, so YAGA cannot guarantee zero post-close writes.
The publisher admits `pull_request` completions only from `.github/workflows/ci.yml` and
`pull_request_target` completions only from `.github/workflows/review-policy.yml`. This lets the
lifecycle wake run on the `main` base branch while a post-merge `push` completion skips every
publisher job before YAGA runs. Path-and-event routing also permits an external fork whose head
branch is literally named `main`.

The beta assumes delivery of every configured lifecycle event. A missed reopen or other same-head
transition can leave an older green status visible because no native boundary check was created.
Canaries must verify lifecycle delivery; any missing delivery is an activation blocker rather than a
case for scheduled repair.

The repository token writes statuses as the shared GitHub Actions integration. Reserve both exact
classic contexts, `Codex Review` and `CI Gate`, and every case-insensitive alias of either. Reject any
colliding workflow, job, check, or other status publisher before making them required; the dynamic
bootstrap bridge is the only temporary job allowed to expose native `CI Gate` while v2 is disabled.

The maintainer-only observer includes `closed` because Maintainer Approval needs immediate
shared-head recovery. YAGA's separate lifecycle workflow deliberately does not. GitHub's native
required review-conversation resolution remains enabled separately, so a
completed Codex review with findings cannot merge until every actionable thread is answered and
resolved. YAGA does not
reinterpret review completion as approval.

## Rebase auto-merge

The repository uses pull requests for every change, but merges should preserve the descriptive commits
inside the PR. Do not squash a PR. After CI is green, enable auto-merge with the rebase method:

```bash
gh pr merge --auto --rebase <PR-number>
```

During bootstrap, auto-merge waits for `Commit Messages`, native `CI Gate`, `Maintainer Approval`, and
`Codex Review`, plus GitHub's native required review-conversation resolution. After the canaried
cutover, it also waits for native `CI Prerequisites` and `Review Policy Boundary`, while YAGA alone
publishes classic `CI Gate` last. The rebase method preserves each descriptive commit on `main`. The
`Commit Messages` workflow validates the PR title and ordinary PR commits from a base-branch checkout
with read-only repository permission. Its title-only Dependabot path is limited by trusted
`pull_request_target` event metadata to the bot's same-repository branch namespace. Native
`CI Prerequisites` runs with `always()` and rejects failed, cancelled, timed-out, or skipped blocking
jobs, including a package build skipped after an upstream failure. The bootstrap compatibility job
derives native `CI Gate` from that result; after cutover it is named `Legacy CI Gate` and YAGA derives
the final classic `CI Gate` from prerequisites plus exact-head review evidence.
The Codex publisher executes only the immutable YAGA action with bounded permissions and no checkout.
V1 review events use the unprivileged shared observer before trusted default-branch publishers write
statuses. V2 instead uses a trusted, status-writing `pull_request_target` lifecycle invalidator with
no issue-write permission or checkout. Its prepare and finalize jobs use authenticated wake election
and exact-state revalidation; only observe and request workers are serialized per pull request.
Maintainer status jobs invalidate the old state before checking out trusted default-branch code.
Neither policy path executes pull-request code with a write token.

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
conflicts, rerun the affected checks, re-evaluate the full-gate triggers below, and push. After native
CI succeeds, YAGA requires one exact current-boundary marked request, creating at most one through the
owner or protected external-author path and accepting only strictly later connector evidence.
Auto-merge waits for the new head's checks. The merge
policy's steady state requires `Commit Messages`, `CI Prerequisites`, `Review Policy Boundary`,
`CI Gate`, `Maintainer Approval`, and `Codex Review` from GitHub Actions, requires
native review conversation resolution, and permits rebase merges only. Activate a new required
context only after its workflow is merged and a ready canary pull request reports that exact context
successfully; GitHub does not retroactively create a newly required check. Staged ruleset activation
must also enable strict required-status freshness so a changed base cannot reuse results from an
older candidate. An owner-authored canary proves the contexts but does not exercise the
protected request-approval path. Treat rollout as incomplete until an external or bot-authored canary
is available and proves both that quota approval precedes the marked Codex request and that approval,
dismissal, reapproval, synchronization, and an unrelated `COMMENTED` review replace the single
current-head `Maintainer Approval` status without leaving a second required result. Existing pull
requests with legacy same-named check runs must move to a new head before serving as that canary. The
native approval count remains zero so the owner is not forced
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
