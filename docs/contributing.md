# Contributing

Thank you for your interest in contributing to django-ray!

Changes to an adopter-facing import, setting, command, metric, lifecycle status, or
persisted-format reader must follow the candidate
[Stability and Deprecation Policy](stability.md). Check
`tests/contracts/public_api_v1.json` before renaming or removing a Python symbol. An
intentional incompatibility needs a migration path, changelog entry, and an explicit
contract decision; changing the inventory alone is not evidence that the change is
compatible.

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- Git

### Clone and Install

```bash
git clone https://github.com/dariuszpanas/django-ray.git
cd django-ray
uv sync
```

### Verify Setup

```bash
make test
```

## Development Workflow

### Repository Conventions

Create branches from an up-to-date `main` and use lowercase kebab-case names:

| Change | Branch prefix | Example |
|---|---|---|
| Feature or enhancement | `feat/` | `feat/issues-25-33-contributor-qol` |
| Bug fix | `fix/` | `fix/ray-job-timeout-race` |
| Documentation only | `docs/` | `docs/worker-mode-selection` |
| Maintenance, tooling, or dependencies | `chore/` | `chore/ruff-upgrade` |
| Test-only change | `test/` | `test/worker-reconnect-coverage` |

`feat/` is the default for feature work. Repository guidance overrides generic tool defaults, so
automated agents must not replace it with an unrelated `agent/`, `codex/`, or similar prefix. An
explicit maintainer-requested name still takes precedence.

Use Conventional Commit syntax for commits and PR titles. For a material change, treat each retained
logical commit as the portable, PR-grade change record. Its body must stand on its own in `git log`,
mirrors, archives, changelog tooling, and other systems without GitHub metadata. Record observable
behavior and motivation; important invariants, boundaries, and non-goals; compatibility, migration,
rollout, or activation details when applicable; exact validation or a specific reason it was not run;
and repository-local documentation, ADRs, modules, migrations, or tests that are useful investigation
starting points. One large atomic commit is valid. Use proportional detail for small mechanical
changes and keep unrelated changes in separate logical commits.

The tracked template recommends this layout:

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

The headings are guidance rather than a required format. Meaningful unstructured prose is equally
valid. The gate requires at least eight body words outside headings, validation evidence, generated
metadata, and trailers, plus a specific validation result or an explicit non-placeholder reason
validation was not run. It rejects placeholders, development-only notes such as "address review
feedback," bodies that merely repeat the header, and commands with no recorded result. A non-empty
`BREAKING CHANGE:` footer may follow the descriptive body; `!` in the header is sufficient to mark
the change as breaking. Wrap ordinary **commit prose** at 72 characters. PR descriptions use natural
Markdown without artificial hard wrapping. Structurally validated generated dependency headers and
metadata, URL destinations, complete Markdown tables, and recognized Git trailers are exempt from
mechanical wrapping. The required hosted check validates only the PR title for trusted Dependabot
pull requests because GitHub controls their generated commit bodies and offers no body-formatting
option. The exception requires the exact `dependabot[bot]` author, a same-repository head, and a
`dependabot/*` branch; every other pull request retains full commit-message validation.

PR descriptions and issue trailers are supplemental: they must not be the only place durable commit
context exists. Keep material facts aligned between the PR and every retained logical commit, but
format each surface independently. PR descriptions use natural Markdown rather than copied
72-column commit wrapping.

Install the tracked template in each checkout or worktree:

```bash
git config extensions.worktreeConfig true
git config --worktree commit.template "$(git rev-parse --show-toplevel)/.gitmessage"
git config --worktree core.commentChar ";"
```

Worktree-specific configuration keeps linked worktrees pointed at their own tracked template. Git
normally removes lines beginning with `#` as comments. Setting the comment character to `;` preserves
the template's optional `##` headings; the template uses `;` for instructions that Git should remove.

The required `Commit Messages` GitHub Actions check validates the PR title and, for ordinary pull
requests, the full message of every commit. Trusted same-repository Dependabot pull requests keep the
required check but validate only their Conventional Commit PR title; the required `CI Gate` still
validates their complete change. Use one of `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `revert`, `style`, or `test`; an optional scope and `!` are allowed:

```text
<type>[optional scope][!]: <imperative summary>
```

The check enforces meaningful body context, validation evidence or a specific not-run reason, and the
wrappable commit-prose line limit without prescribing section headings. It reports the offending
commit or line.

The two required review checks apply conditional policy without forcing the repository owner to
self-approve. `Maintainer Approval` passes a pull request authored by `dariuszpanas`; every other
author needs an `APPROVED` review from `dariuszpanas` on the current head commit. Both gates validate
the live pull request against the trusted event base ref, base SHA, and head SHA before passing. A
push, base change, or title- or body-only edit requires a new Codex outcome after that event. GitHub
review records do not bind the base SHA, so even an existing exact-head connector review is not
reusable after an edit. The Codex gate also verifies that its Actions run is the newest run for this
pull request and head; manually rerunning an older lifecycle event cannot supersede a later edit.
Before success, it rechecks the exact live head and base. Review comments and reaction transitions
may update pull-request activity timestamps, but they do not replace candidate identity or workflow
lineage.

The trusted `Codex Review` workflow posts one full-SHA `@codex review` request for every eligible
pull-request event and passes its immutable comment ID to the polling gate. Contributors do not need
to post a second request. A workflow rerun creates a new request after that attempt starts rather than
reusing review state from an earlier attempt. The request body is:

```text
@codex review

<!-- django-ray:codex-review-head=<full head SHA>;run=<workflow run ID>;attempt=<run attempt>;metadata=<title/body digest>;lifecycle=<event digest> -->
```

Only an `eyes` reaction from the immutable Codex connector identity counts; reactions from Actions,
maintainers, or other users are ignored. The gate must first observe Codex `eyes` on the exact
attempt-bound request. It then waits while connector `eyes` remains on that request or the pull-request
root. After the request's connector `eyes` disappears, two consecutive eye-free polling observations
settle the review, followed by workflow-lineage and exact live head/base confirmation. The gate does
not interpret `+1`, formal review state, or any other reaction as completion evidence. A workflow
attempt waits up to 30 minutes so the observed review can finish without losing its state to a rerun;
if the connector never acknowledges that exact request with `eyes`, the attempt fails closed.
The workflow reads title and body from the runner-provided `GITHUB_EVENT_PATH` with a bounded
file-backed parser; it never places the potentially large body in an environment variable or command
argument. The request's metadata digest binds those values through final confirmation, so either field
changing invalidates the attempt without relying on reaction-driven pull-request activity timestamps.
The request also binds a canonical digest of bounded close, reopen, draft-conversion, and ready events
through the trigger. Final confirmation brackets a lifecycle re-read with two live pull-request reads
and requires a stable activity timestamp across that short window, so a lifecycle round trip cannot
restore the old predicates and satisfy a superseded attempt.

`Codex Review` proves that the requested review settled; it does not reinterpret findings as an
approval. GitHub's native required review-conversation resolution remains a separate merge condition,
so a completed Codex review with findings cannot merge until every actionable thread is resolved.
Reply to each actionable thread with the implemented fix and its validation, or with the explicit
reason for declining it, before resolving the conversation. Requests and reviews from an older event
candidate do not count, draft pull requests fail both review checks, and a workflow event superseded
by a later pull-request event fails closed.

For example, `fix(worker): preserve task ownership` is a valid header, but its commit still needs
enough body context to explain the retained change. Invalid titles or commit headers fail with the
exact offending value and expected format.

Before editing, inspect `git status --short`, the current branch, and `HEAD`. Preserve unrelated work,
stage explicit paths instead of `git add .`, and review both the working and staged diffs. Repository
automation follows the root `AGENTS.md`, including the optional Obsidian project-memory workflow when
a local vault is available.

### Code Style

We use automated tools to maintain consistent code style:

```bash
# Format code
uv run make format

# Format and apply safe lint fixes
uv run make fix

# Check lint without changing files
uv run make lint

# Type check
uv run make typecheck

# Check formatting, lint, and types without changing files
uv run make check
```

### Running Tests

```bash
# All tests
make test

# Default-resource local subset with four pytest-xdist workers
uv run make test-xdist

# Override the local worker count when useful
uv run make test-xdist TEST_XDIST_WORKERS=8

# Unit tests only
make test-unit

# Integration tests only
make test-integration

# With coverage
uv run make test-cov
```

`test-cov` and `ci` enforce the same coverage floors as CI:

- global coverage: `>= 95%`
- `src/django_ray/management/commands/django_ray_worker.py`: `>= 90%`
- `src/django_ray/runner/ray_job.py`: `>= 90%`

`uv run make ci` runs the required format, lint, type, runtime-dependency advisory,
coverage, strict-documentation, and package-build checks for the current interpreter.
GitHub Actions additionally repeats tests across supported Python versions and
minimum/latest dependency resolutions. Run the advisory check alone with
`uv run make audit-dependencies`; it exports the exact locked default and optional-extra
runtime graph without development tools, then queries current PyPI advisory data. The
command requires network access and its result can change when a new advisory is
published. The check fails when its lock export is stale, incomplete, unpinned, or
contaminated by the project or audit tool, and an advisory-service failure does not pass
silently. Blocking CI repeats it under every supported Python version on Linux and on
Python 3.12 for the documented Windows development boundary so platform markers are not
covered only by a contributor's workstation. The release workflow repeats that same
matrix before package building. Each scan also cross-checks its hashed requirements
against a second locked CycloneDX export so an omitted transitive cannot silently escape
the advisory input.

`test-xdist` is a fast local iteration target, not the full test or release gate. It invokes pytest
once with `-n 4` by default and selects tests without the resource-owning `real_ray`, `postgresql`,
and `live_cluster` markers. It does not define marker-derived phases or CI jobs, and tests must not
communicate or depend on execution order. Run the excluded resource contracts separately using the
commands under [External-resource ownership](#external-resource-ownership), and apply the validation
checkpoint policy below before handoff. Change the worker count with `TEST_XDIST_WORKERS=<count>`.

### Coverage debt review

The hard floors are safety margins, not coverage targets. Build the same report used by the monthly
tracker from a local checkout with:

```bash
uv run make coverage-debt
```

The target first runs the default-resource selection (`not real_ray and not live_cluster and not
postgresql`) and then runs the manifest-owned `local-ray` lane serially with skips forbidden. The
default-off `compiled_graph_opt_in` probe stays outside required hosted coverage. The local-Ray
phase appends to the default-resource coverage data before the target applies the central global,
worker, and Ray Job floors from `pyproject.toml`.

Both subprocess trees are bounded: the default-resource phase has a 20-minute ceiling and the
local-Ray phase has a 15-minute ceiling inside the unchanged 45-minute workflow limit. Override
`COVERAGE_DEBT_DEFAULT_TIMEOUT_SECONDS` or `COVERAGE_DEBT_LOCAL_RAY_TIMEOUT_SECONDS` only for a
focused investigation, and record the reason. Each phase retains at most 256 KiB of output, so a
failure or timeout still leaves useful diagnostics rather than consuming the complete workflow.
The runner continuously drains a bounded in-memory tail and counts discarded bytes; it never
spools the phase's full output to disk. A passing local-Ray process without fresh, valid
skip-forbidden timing evidence is a failed phase rather than a reportable success.
Each launcher is retained in a POSIX process group or kill-on-close Windows Job. Descendants get a
two-second orderly shutdown grace after the launcher exits; any survivor is terminated and fails
the phase instead of keeping an inherited output pipe or hosted runner alive.
The target writes these ignored artifacts under `artifacts/coverage-debt/`:

- `coverage.py.json`: Coverage.py's source evidence;
- `coverage-debt.json`: exact covered, missed, and statement totals plus every uncovered range;
- `coverage-debt.md`: the same measurements and ranges in reviewable Markdown;
- `coverage-phases.json` and `coverage-phases.md`: phase selection, append mode, deadline, outcome,
  and diagnostic locations, even when final report rendering cannot run;
- `coverage-default-resources.log` and `coverage-local-ray.log`: capped phase-output tails;
- `local-ray-timing.json`: source-fenced manifest evidence proving the exact selected cases,
  successful outcomes, and the local-Ray lane's skip-forbidden contract.

Files are sorted by missed lines. Every range receives the explicit per-file classification in
`.github/coverage-debt-classifications.json`; narrow range overrides identify platform-owned paths.
If a newly uncovered file has no classification, report generation fails until a maintainer reviews
it. Classify each remaining path using one of these policies:

1. **Testable behavior**: add assertion-rich unit, integration, subprocess, or real-Ray coverage for
   a meaningful contract, error, cleanup, or recovery path.
2. **Environment-specific**: exercise the path in the matching Linux, PostgreSQL, live-cluster,
   canary, or platform job. Windows-only Ray behavior remains visible but does not block the primary
   Linux/Kubernetes delivery target.
3. **Upstream/native constraint**: link upstream evidence or the applicable canary and record when
   the constraint should be reconsidered.
4. **Defensive invariant**: demonstrate why normal inputs cannot reach the path. Prefer simplifying
   or deleting the redundant branch; use only a narrow, explained exclusion when it must remain.
5. **Dead or non-behavioral code**: delete the code or justify a narrowly scoped exclusion.

Tests must assert behavior rather than mutate private state merely to execute lines. Do not add broad
coverage omissions or blanket `pragma: no cover` annotations to improve the percentage. Keep cleanup
changes in focused pull requests and create a child issue manually only when the uncovered behavior
is independently reviewable; the reporting workflow never creates issues or pull requests.

`.github/workflows/coverage-debt.yml` runs on Ubuntu and Python 3.12 on the first day of each month.
Scheduled runs use the current default branch; manual dispatch is available after substantial
runtime, workflow, or release changes. It uploads reports plus bounded phase diagnostics, appends
the phase summary and completed Markdown report to the job summary, and updates one bot-owned
comment on the issue containing
`<!-- django-ray:coverage-debt-tracker -->`. Only issues owned by the repository owner, a member, or
a collaborator participate in tracker discovery, and only the expected Actions bot's comments
participate in report discovery; matching text from untrusted authors is ignored. The updater still
fails before writing if either trusted marker is duplicated. Its first run seeds current, previous,
and high-water measurements; later runs move current to previous and retain the exact best ratio.
Repeated identical runs replace the same comment rather than creating another issue or comment.

The current metric is **line coverage only**. Enabling branch coverage changes both the evidence and
the baseline and therefore belongs in a separate follow-up. The recurring report does not change the
95% global, 90% worker, 90% Ray Job, or 80% testproject floors and does not depend on Codecov.

For changes that affect dashboard/static assets, images or RuntimeEnv packaging, Ray Client or
bootstrap behavior, Kubernetes resources, probes, workers, monitoring, or a cross-component task
lifecycle, consult the [local KubeRay final-gate trigger matrix](deployment/local-kuberay-gate.md).
Run required rows from a clean checkout after `uv run make ci`, make the cold-Ray decision explicit,
and record a concise semantic validation summary in the material commit and PR. Include the exact
gate command and result, explicit cold-Ray decision, verified source-tree match, and relevant
workload-readiness, authenticated API/task-smoke, and preservation outcomes. For a recommended row,
retain either the same passing summary or a specific reason it was not run. The complete secret-free
evidence block remains available as bounded runtime diagnostics; do not paste its image IDs, pod
hashes, cluster UIDs, checksums, or similar run-specific identifiers into durable Git history by
default. Retain an identifier only in a focused issue or PR comment or diagnostic artifact when an
investigation needs it, and explain how it will be used. Never include the API token or unbounded
cluster logs. Add the summary by amending only the retained commit message, then verify that the
emitted `source_tree` still equals `git rev-parse HEAD^{tree}` without recording the hash. A tracked
tree change invalidates the evidence and requires a new run.

### Testproject Smoke Test

The bundled `testproject` is validated as an application boundary rather than only as task fixtures:

```bash
uv run make test-testproject
```

This runs Django's system checks, the sample API/security/workflow tests, and requires at least 80%
coverage across the user-facing API, landing view, and URL configuration. CI runs it as the
`Testproject Smoke` job before building the package.

The separate blocking `Docker Compose Smoke` job builds the tracked application image, generates
disposable credentials, starts the shared-PostgreSQL Compose topology, and runs the bounded
authenticated enqueue/worker/result contract from `testproject/docker_smoke.py`. It always removes
the disposable volume and containers and only prints bounded, credential-redacted diagnostics on
failure.

### Live Cluster Fault Tests (Opt-In)

`tests/integration/test_live_failure_injection.py` runs against a real Ray cluster and is skipped by default.

Enable it explicitly:

```bash
# PowerShell
$env:DJANGO_RAY_LIVE_CLUSTER_TESTS="1"
$env:DJANGO_RAY_LIVE_RAY_ADDRESS="ray://localhost:10001"
$env:DJANGO_RAY_LIVE_MIN_NODES="2"
$env:DJANGO_RAY_LIVE_WORKING_DIR_URI="file:///runtime-env/django-ray-source.zip"
uv run pytest tests/integration/test_live_failure_injection.py -v
```

Environment variables:

- `DJANGO_RAY_LIVE_CLUSTER_TESTS`: set to `1/true/yes` to enable suite.
- `DJANGO_RAY_LIVE_RAY_ADDRESS`: Ray address for live cluster tests.
- `DJANGO_RAY_LIVE_MIN_NODES`: minimum alive node count required before tests run.
- `DJANGO_RAY_LIVE_WORKING_DIR_URI`: optional immutable project archive URI used by the real
  `RayCoreRunner.submit()` smoke test. A `file://` archive must exist at the same absolute path on
  every Ray node; without this setting, only that submission smoke test is skipped.

CI strategy:

- The default test matrix excludes `live_cluster` tests to keep its coverage runs deterministic.
- The CI workflow runs these tests separately against a disposable two-node Docker Ray cluster and
  stages the checked-out project archive on both generic Ray nodes.
- The in-container readiness check proves GCS membership first. After dependency installation, a
  separate host-side Ray Client preflight proves the published proxy, its per-client backend, both
  live nodes, and one trivial remote task. At most two disposable Python processes may try that
  preflight: each dumps threads after 45 seconds, receives `TERM` after 70 seconds, and has a
  75-second hard ceiling, so readiness cannot consume the job timeout. The live-cluster driver
  invokes the synced virtual environment directly and disables Ray's implicit `uv run` RuntimeEnv
  propagation because the generic containers do not share the runner's virtual environment; the
  explicit per-task RuntimeEnv smoke remains enabled.
- The generic-cluster submission smoke derives its remote package list from the testproject's
  declared `project` RuntimeEnv profile. Testproject-only applications such as Unfold therefore
  remain explicit remote dependencies instead of being supplied accidentally by the CI runner.
- The three declared scenarios run sequentially against that one cluster but in fresh pytest
  processes, so the deliberate Ray Client disconnect cannot contaminate the following scenario.
  A process still running after 90 seconds emits a Python thread dump. If it remains running after
  165 seconds it receives `TERM`, and if it still does not exit it is forcibly killed 15 seconds
  later, keeping a 180-second hard ceiling. The job records the exact node ID plus elapsed outcome.
  This is process isolation inside one visible CI job, not an xdist worker, matrix shard, or
  concurrent test protocol. Bounded Ray Client backend error files and container logs are emitted
  before the exact container and network cleanup runs on every outcome. The internal diagnostic
  stream is capped at 64 KiB and redacts the reviewed job credential environment inventory; the
  Ray containers receive no runner secret environment flags.
- CI also supports `workflow_dispatch` for a manual rerun; it does not need a repository variable
  or an externally reachable Ray cluster.

### Pinned Compiled Graph KubeRay Pilot (Opt-In)

Public GitHub-hosted workflows do not invoke native Ray Compiled Graph APIs. They cover the
default-off policy, subprocess containment and parsing, and the guarded evidence harness
hermetically. Maintainers collecting native issue #102 evidence must instead use a clean commit, the
pinned `desktop-linux` Docker CLI context, the `docker-desktop` Kubernetes context, KubeRay operator
1.6.2, and the dedicated pilot namespace:

```powershell
uv run python scripts/kuberay_compiled_graph_pilot.py run `
  --context docker-desktop
```

The runner verifies the local Docker context and engine before any build. It captures one clean full
Git revision, validates a bounded regular-file inventory, and safely extracts a tracked-only
`git archive`; a required Dockerfile-specific deny-by-default policy is a second boundary against
`.env`, `.vault`, Git metadata, generated source artifacts, and unrelated repository files. It then
canonicalizes only clean-checkout CRLF pairs to the archive's LF representation when hashing the
four strict UTF-8 pilot assets and rejects a BOM, NUL byte, or bare carriage return, so Windows and
Linux checkouts of one commit retain the same configuration and policy identities. It then builds a
source-labelled image from the digest-pinned Ray base; checks the exact Kubernetes, Docker runtime,
kernel, libc, Python ABI, node placement, and dependency profile, including independently pinned
`cupy-cuda12x==13.4.0` and required `fastrlock==0.8.3`; verifies the exact local image ID on every
pilot pod; exercises hard-timeout containment; executes
direct-driver and nested-owner probes in contained child processes; and deletes only
`django-ray-cgraph-pilot` by default. Namespace lookup distinguishes explicit absence from API
failure and refuses every pre-existing namespace. The namespace create response binds a random
per-run label and immutable UID to the runner. The RayCluster is create-only, and its create-response
UID is bound to the namespace UID and run token in RayCluster/pod labels, annotations, environment,
and exact pod controller references. The runner brackets pod reads and execs with current namespace
and RayCluster lease checks. It also verifies the shared KubeRay operator's exact
Deployment-to-ReplicaSet-to-pod ownership, sole container inventory, pinned running digest, Ready
state, and exact nonnegative restart count without mutating the operator. That complete observation
must remain exactly equal immediately before RayCluster creation, after pilot-pod readiness, and
after final runtime capture; any rollout, restart-count change, or controller/container drift
invalidates the evidence. Cleanup verifies the exact live namespace lease and uses
name/profile/run-token selectors, so it never adopts a stale namespace.
Kubernetes namespaced create and `kubectl delete namespace` do not expose the namespace-UID and
UID/resource-version preconditions needed to make these boundaries atomic. An external
delete/recreate inside a check/call window is unsupported; the post-checks fail evidence collection
but do not claim the scoped API call could not reach the replacement.
It never reads Kubernetes Secrets.
The same immutable local image ID first runs in a network-isolated one-shot container with a physical 256 MiB
`/dev/shm` near-neighbor. The exact-profile admission layer must admit the tracked baseline and
reject the changed identity as `PILOT_PROFILE_MISMATCH` before the hardened probe or any native
command can run. Successful cluster evidence also requires exact shared-memory entry and object
identity restoration, no active named owner task, and no surviving pilot child process.
Ray 2.56.0 currently leaves mutable-object shared-memory channels behind after otherwise
successful Compiled Graph teardown. To retain a fresh, bounded failure record without
weakening that invariant, use a new date-stamped path:

```powershell
uv run python scripts/kuberay_compiled_graph_pilot.py run `
  --context docker-desktop `
  --blocked-evidence-output `
    docs/investigations/compiled-graph-kuberay-blocked-YYYY-MM-DD.json
```

The runner checks shared-memory state immediately and again after consecutive pinned
5, 15, and 30 second waits, for a 50 second total wait. It writes that file only when
both topology probes completed, every actor, task, object-store result, and pilot child
process is gone, and shared-memory state alone still fails to return to its exact empty
baseline. The record contains only allowlisted identities, topology outcomes, hashed
shared-memory entry identities, Ray `sem.hdr`/`sem.obj` kind and pair counts, aggregate
digests, byte totals, and tracker URLs; raw semaphore names are never retained. The
write boundary rechecks the clean current Git revision, tracked profile and manifest
identities, UTC timestamp order, and exact Docker, KubeRay, Kubernetes, pod-resource,
runtime, cleanup, and zero-state schemas and JSON integer types before creating the file. The
known blocker classification requires the same fully paired Ray semaphore fingerprint
in every cleanup observation, with no stray or unpaired entry. After the final wait, the
runner refetches the pods, requires the exact sole regular container and expected KubeRay
init-container inventory, and preserves unchanged pod UIDs, container IDs, images, restart
counts, identity environment, namespace/RayCluster lease binding, and every profile-declared Ray
start parameter before it captures the final cluster state used by the zero-residual proof. Valued
parameters and KubeRay's
valueless `--disable-usage-stats` true switch have distinct pinned semantics; retained pod
evidence records the sanitized lexical form, lexical value, and semantic value independently.
It refuses to overwrite evidence and still exits nonzero. `--keep-cluster` cannot be
combined with retained blocked evidence, and persistence requires explicit verification
that the exact create-response UID and namespace are absent after selector-bound deletion. A
generic setup or probe failure is
printed as bounded JSON but is not written as the known blocked record.

Each subprocess runs inside a Windows Job or POSIX session, descendant trees are terminated at the
boundary, and every post-termination process or pipe wait is bounded. Structured stdout is actively
capped while stdout and stderr are drained concurrently; non-structured output retains only bounded
rolling tails, and concatenated JSON documents fail closed. Do not redirect arbitrary cluster diagnostics into the repository: retain only the runner's bounded,
allowlisted JSON record under `docs/investigations/`, then request a separate capability-promotion
review. A passing pilot remains candidate-native evidence and does not enable Compiled Graph product
execution.

### PostgreSQL Coordination Tests

The fast default suite continues to use SQLite. A separate integration gate runs the worker's
database coordination paths against PostgreSQL using `tests.postgres_settings`:

```bash
docker run --detach --name django-ray-postgres-tests \
  --publish 127.0.0.1:5432:5432 \
  --env POSTGRES_DB=django_ray \
  --env POSTGRES_USER=django_ray \
  --env POSTGRES_PASSWORD=django_ray \
  postgres:17
uv sync --extra postgres
```

Set the test settings and connection variables, then run the focused gate. In PowerShell:

```powershell
$env:DJANGO_SETTINGS_MODULE = "tests.postgres_settings"
$env:DATABASE_NAME = "django_ray"
$env:DATABASE_TEST_NAME = "test_django_ray"
$env:DATABASE_USER = "django_ray"
$env:DATABASE_PASSWORD = "django_ray"
$env:DATABASE_HOST = "127.0.0.1"
$env:DATABASE_PORT = "5432"
uv run make test-postgres
```

On POSIX shells, export the same variables before running `uv run make test-postgres`. The database
user must be allowed to create and drop `DATABASE_TEST_NAME`; the disposable container above has the
required permission. These credentials are local-only examples. Remove the container when finished:

```bash
docker rm --force django-ray-postgres-tests
```

CI runs this gate on Python 3.12 and Django 6.0.8, keeps it separate from coverage, and prints server
version and connection activity when the gate fails.

### Local Testing

Start the development server and worker:

```bash
# Terminal 1: Django server
make runserver

# Terminal 2: Worker
make worker-all
```

Test via the API at http://127.0.0.1:8000/api/docs

## Pull Request Process

1. **Create a branch from the latest `main`**
2. **Make your changes** as one or more clear logical commits; one large atomic commit is valid
3. **Add tests** for new functionality
4. **Update documentation** if needed
5. **Validate the affected boundary** with focused tests and static checks; run `uv run make ci` only
   when the checkpoint triggers below apply
6. **Submit a pull request** with a Conventional Commit title, naturally formatted Markdown, clear
   description, validation results, and `Closes #<number>` when applicable

### Rebase auto-merge

Pull requests are the unit of review and delivery, but their individual commits should remain visible.
Keep commits focused and do not squash the PR. Once the required checks pass, enable rebase auto-merge:

```bash
gh pr merge --auto --rebase <PR-number>
```

Before ordinary pushes, run `uv run make check` plus the narrowest affected tests and applicable
schema, documentation, or packaging checks. Every push to an open PR receives the broad exact-head
hosted CI matrix. Record the commands and results in the retained commit and PR.

A PR changing executable package or runtime behavior must pass `uv run make ci` once before final
review or auto-merge. It is also required for release candidates, break-glass merges, dependency,
packaging, build, or CI-composition changes, and before a required local KubeRay gate. Later changes
limited to PR or commit metadata, documentation, or tests do not invalidate that result; focused
delta checks and green final-head hosted CI suffice. Package, dependency, and deployment metadata or
manifests are not exempt, and a runtime-affecting review repair re-evaluates the triggers. A PR
containing only exempt deltas does not require a local full gate. Current-head `CI Gate` is the final
broad merge proof. Do not rerun the local full gate merely because an exempt focused follow-up changed
the commit hash: retain the checkpoint result and add exact delta evidence.

The `Commit Messages` workflow runs on `pull_request_target`, validates the PR title and ordinary PR
commit messages, and reports a required status check without needing secrets from the PR. Its
title-only Dependabot path is limited by trusted event metadata to the bot's same-repository branch
namespace. The separate required `CI Gate` runs after every blocking job and passes only when lint,
docs, typing, all supported Python tests, PostgreSQL coordination, live-cluster faults,
testproject, the tracked Docker Compose smoke, minimum/latest dependencies, and package build all
report `success`. Its `always()` condition
makes a failed, cancelled, timed-out, or skipped dependency visible as a failed gate instead of a
successful skip. `Maintainer Approval` and `Codex Review` use read-only permissions and check out only
the default branch, never pull-request code. This repository uses rebase auto-merge rather than a
merge queue: auto-merge waits for all four required checks and native required review-conversation
resolution, then applies the rebase method.

Native Compiled Graph evidence is produced only by the guarded local KubeRay pilot, not a public
hosted workflow. Coverage-debt review and benchmark workflows are evidence producers, not merge
checks. Documentation builds outside pull requests and release workflows run after merge, manually,
or from tags. Codecov upload is advisory inside the otherwise blocking Python 3.12 job. Add future PR
CI jobs to `CI Gate` unless contributor policy explicitly documents why they are non-blocking.

Before each push and again before enabling auto-merge, inspect and validate the exact commit range that
will be retained:

```bash
git fetch origin
git log --format=fuller origin/main..HEAD
uv run python scripts/check_conventional_commits.py --range origin/main..HEAD
```

For every material commit shown by that log, compare its body with the PR description before pushing
and again before enabling auto-merge. They should agree on observable behavior, important boundaries,
rollout or activation impact, validation, and useful investigation starting points. Semantic parity
does not require copied wording, identical headings, or identical wrapping. Commit prose wraps for
terminal history; PR descriptions remain natural Markdown.

Use `git rebase -i origin/main` to fold `fixup!`/`squash!` commits, CI repairs, review repairs,
formatting-only follow-ups, and other development iterations into the logical commit they correct.
After rewriting, push with `--force-with-lease`. Preserve genuinely independent changes as separate,
focused commits with their own descriptive bodies. Validate the final PR title together with the
range before enabling auto-merge.

If an auto-merge branch becomes stale or conflicted, rebase it onto the latest `main`, resolve the
conflicts, rerun the affected checks, re-evaluate the full-gate triggers above, and push. The trusted
workflow posts the fresh exact-head Codex request; auto-merge recalculates the required checks. The
merge policy's steady state requires
`Commit Messages`, `CI Gate`, `Maintainer Approval`, and `Codex Review` from GitHub Actions, requires
native review conversation resolution, and allows rebase merges only. Activate a new required
context only after its workflow is merged and a ready canary pull request reports that exact context
successfully; GitHub does not retroactively create a newly required check. Staged ruleset activation
must also enable strict required-status freshness so a changed base cannot reuse results from an
older candidate. An owner-authored canary proves the contexts but does not exercise the
review-submission trigger. Treat rollout as incomplete until an external or bot-authored canary is
available and proves the current-head maintainer approval path. The native approval count remains
zero so the owner does not need self-approval; `Maintainer Approval` requires the owner's
current-head approval for every other author. The current owner `pull_request` bypass remains
explicit break-glass recovery, not absolute enforcement against an intentional owner bypass. Routine
merges remain gated, and emergency use requires the explicit
`gh pr merge --admin --rebase <PR-number>` command.

The bypass is a break-glass path for a GitHub infrastructure failure, not a way to retain an invalid
commit or omit local validation. Document the outage and urgency in the PR, validate the exact range
and title with `scripts/check_conventional_commits.py`, run `uv run make ci`, and record both results.
After the emergency merge, verify the rebased `main` history and ruleset bypass event, then open or
link a follow-up incident. If GitHub's PR merge service is unavailable, export the ruleset, change
only the named owner bypass to `always`, perform the smallest recovery, immediately restore
`pull_request`, and verify the complete ruleset through the API. Never leave an `always` or `exempt`
bypass configured.

### Commit and PR Titles

Use Conventional Commit syntax for both commits and PR titles:

```
feat: add support for task priorities
fix: handle connection timeout in Ray client
docs: update deployment guide for TLS
test: add unit tests for retry logic
```

### PR Checklist

- [ ] Focused affected tests and applicable static/schema/docs/package checks pass
- [ ] Full local-gate checkpoint decision is recorded; `uv run make ci` passed when triggered
- [ ] Exact-head hosted `CI Gate` passes before merge
- [ ] Packaging builds when packaging or release metadata changed (`uv build`)
- [ ] Documentation updated (if needed)
- [ ] Changelog updated (for user-facing changes)
- [ ] Each material retained commit records exact validation or a specific not-run reason
- [ ] Exact aggregate validation commands and results included in the PR description
- [ ] Concise semantic Local KubeRay gate summary recorded when its trigger matrix is required, or
      a specific not-run reason recorded when recommended
- [ ] Material behavior, boundaries, rollout impact, and investigation paths agree between commits
      and the PR without requiring copied formatting
- [ ] PR description uses natural Markdown without artificial 72-column hard wrapping

## Code Organization

```
src/django_ray/
|-- models.py           # Database models
|-- admin.py            # Admin interface
|-- backends.py         # Django Task backend
|-- conf/               # Settings
|-- runner/             # Task runners
|   |-- ray_job.py      # Ray Job API
|   |-- ray_core.py     # Ray Core (@ray.remote)
|   `-- ...
|-- runtime/            # Task execution
|   |-- entrypoint.py   # Execution entry
|   |-- distributed.py  # parallel_map, etc.
|   `-- ...
`-- management/commands/
    `-- django_ray_worker.py
```

## Testing Guidelines

### External-resource ownership

Pytest markers describe execution contracts, not whether a test lives under `unit` or
`integration`. Tests that use only in-process Django, SQLite, mocks, or local files do not need an
external-resource marker. Keep these resource-owning contracts explicit:

| Contract | Ownership and supported execution |
|---|---|
| Required `real_ray` | Starts a local Ray runtime. Use `uv run pytest -m "real_ray and not compiled_graph_opt_in" -v` only for serial debugging without pytest-xdist. The manifest-backed evidence command below makes startup errors and skips fail. |
| `compiled_graph_opt_in` | Marks the one native Compiled Graph topology probe, also requires `real_ray`, and permits its deliberate environment-gated skip until the upstream capability gate passes. Run it separately with `uv run pytest -m compiled_graph_opt_in -v`. |
| `postgresql` | Uses the dedicated PostgreSQL coordination database. Run `uv run make test-postgres` as one serial evidence lane so lock, timing, row, and WAL observations are not distorted. |
| `live_cluster` | Connects to the shared external Ray cluster. The module skips only while opt-in is disabled; once enabled, connection or readiness failure fails the serial lane. |
| Testproject contract | Exercises the bundled application boundary through `uv run make test-testproject`; it is path-selected rather than marker-selected. |

Tests without one of the three external-resource markers above form the default-resource
`test-xdist` selection. They are expected to run independently under ordinary pytest-xdist;
pytest-django gives workers separate test databases. Treat failures caused by shared temporary
paths, ports, subprocesses, or global state as isolation defects to fix, not as reasons to add
inter-test synchronization or split CI by marker.

Plain pytest reports selected skips without failing its exit status. Record required local-Ray evidence
through the manifest runner so its `forbid` skip policy proves that every selected case executed:

```bash
uv run python scripts/test_suite_inventory.py run \
  --lane local-ray \
  --observation local-required-ray \
  --variant locked-dependencies \
  --timing-output artifacts/test-suite-inventory/local-ray-timing.json \
  --external-note "uv environment already synchronized; setup time excluded" \
  -- -v
```

Any non-collection pytest session whose final selected items include `real_ray` acquires
one OS-released host-wide django-ray test lock before executing tests. The lock spans
processes and linked worktrees, so two agents cannot accidentally start independent local
Ray test owners on the same machine. A contender fails before test execution with bounded
owner metadata; it does not wait, retry, skip, or delete another process's state. The lock
file may remain after a process exits, but the operating-system lock is released
automatically and stale contents never establish ownership. Collect-only inventory and
sessions with no selected `real_ray` case remain lock-free.

This guard protects the validity of local evidence; it is not a workaround for Ray's
native Windows lifecycle issue and does not prove that an upstream version fixed it. Run
real-Ray commands serially, then interpret the supported Linux and KubeRay gates separately
from the documented [native Windows boundary](compatibility.md#platforms).

Tests that request `ray_cluster` or `live_ray_cluster` are checked during collection for the
matching marker. Add a new external-resource fixture to `EXTERNAL_RESOURCE_FIXTURE_MARKERS` in
`tests/conftest.py` so an unmarked consumer cannot silently enter another lane.
`compiled_graph_opt_in` also fails collection unless the same case carries `real_ray`.

Treat the checked-in taxonomy and live collection as the authority for current test counts; do not
copy transient totals into contributor guidance. Run `uv run make test-suite-inventory` to generate
the exact execution-contract and CI-lane inventory without executing tests. Issue #166's frozen
2026-07-22 comparison snapshot remains immutable historical evidence rather than a current count.
Recheck marker selections without executing their resources:

```bash
uv run pytest --collect-only -q -m real_ray
uv run pytest --collect-only -q -m "real_ray and not compiled_graph_opt_in"
uv run pytest --collect-only -q -m compiled_graph_opt_in
uv run pytest --collect-only -q -m postgresql
uv run pytest --collect-only -q -m live_cluster
```

### Unit Tests

Test individual components in isolation. Use the existing
`tests/unit/test_retry.py` and `tests/unit/test_runtime_env.py` as concrete patterns;
do not copy placeholder test bodies from documentation.

### Integration Tests

Test components working together. `tests/integration/test_worker.py` contains runnable
database/worker examples, and `tests/integration/test_live_failure_injection.py`
contains the opt-in real-cluster cases.

### Test Fixtures

Use pytest fixtures for common setup:

```python
import pytest

from django_ray.models import RayTaskExecution, TaskState


@pytest.fixture
def task_execution() -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id="test-task",
        callable_path="myapp.tasks.test",
        queue_name="default",
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json="{}",
    )
```

## Documentation

Documentation is built with Zensical from `zensical.toml`.

Code examples should be copyable:

- identify the target file, such as `myapp/tasks.py` or `settings.py`;
- import or define every name used by a runnable snippet;
- use a `text` fence and label pseudocode or configuration fragments explicitly;
- mark testproject endpoints as examples rather than public library APIs;
- link to real source or tests instead of using an unexplained `...` body.

### Building Docs Locally

```bash
# Local build
make docs-build

# Strict build (CI-equivalent; fails on warnings/broken links)
make docs-build-strict

# Local dev server
make docs-serve
```

Versioned docs deployment is intentionally not wired up yet. CI validates the docs site with
`make docs-build-strict`, which first checks the Unreleased/release-heading contract and then
runs a strict Zensical build. The dedicated docs jobs fetch the complete Git history and require
every dated changelog heading to match a release tag. A release-preparation commit may have one
untagged current-version heading only when its Unreleased section is empty and the complete
release-candidate validator passes.

Read the Docs hosting is configured by `.readthedocs.yaml`. Because Zensical is a custom static
site generator from Read the Docs' perspective, that config checks the structural changelog
contract, builds with Zensical, and copies the generated `site/` directory into
`$READTHEDOCS_OUTPUT/html`. Tag inventory is enforced by the required GitHub docs job because
hosted source checkouts may not contain complete Git metadata.

## Releasing

Releases are automated via GitHub Actions, but preparation and publication are separate
operations.

### Prepare a release candidate

1. Fetch `origin` with complete tags and prepare the candidate from current
   `origin/main`.
2. Keep active release work under `Unreleased`. Update the same development version
   in `pyproject.toml`, `src/django_ray/__init__.py`, `uv.lock`, and the release
   assertion.
3. Run the candidate checks locally against both built distributions and the installed
   wheel:

```bash
uv run make ci
uv run python scripts/validate_release.py --testpypi-candidate vX.Y.Z
uv build
uv run --isolated --no-project --python 3.12 \
  --with ./dist/django_ray-X.Y.Z-py3-none-any.whl \
  python scripts/verify_wheel.py --version X.Y.Z --dist-dir dist
```

4. Merge the preparation PR only after its required checks pass. Fetch `origin` again,
   wait for the complete `origin/main` CI run, and record the exact lowercase full
   candidate SHA.
5. Stop until a maintainer explicitly authorizes that exact SHA. Preparation does not
   authorize a TestPyPI dispatch, tag push, PyPI upload, or GitHub Release.

The TestPyPI candidate validator requires the requested version, package sources,
editable lock entry, complete historical tag inventory, development changelog, and
Compiled Graph capability review to agree. It accepts the normal non-empty
`Unreleased` form and a strict-ready untagged form. It rejects an already-tagged
version. The production validator remains stricter: it requires one dated target
heading and an empty `Unreleased` section.

The distribution smoke checks the wheel `METADATA` and sdist `PKG-INFO` security floor,
then checks installed-wheel metadata, import provenance, management-command discovery,
the exact migration leaf, and a fresh database migration. The release workflow repeats
that gate on every supported Python version.

After explicit authorization, an optional TestPyPI rehearsal can be dispatched from
the exact current `main` commit:

```bash
gh workflow run release.yml \
  --ref main \
  -f version=X.Y.Z \
  -f candidate_sha=<authorized-full-sha>
```

The workflow rejects a dispatch outside the default branch or a non-canonical SHA before checkout.
It checks out the trusted default branch without persisting credentials and immediately refuses to
continue unless the input SHA, dispatch SHA, and checked-out HEAD are identical. It then freshly
fetches `origin/main` and requires that ref to have the same identity before running repository code
or building. Any concurrent tracked change creates a different candidate and requires a fresh
dispatch, validation, and authorization.

### Publish an authorized candidate

For production, move the complete `Unreleased` contents into one dated target-version
heading, update its comparison links, and run the full candidate gate again, including:

```bash
uv run python scripts/validate_release.py vX.Y.Z
```

After that strict preparation is merged, re-fetch `origin`, wait for its complete CI,
and obtain explicit authorization for the new exact SHA. A manual workflow dispatch
remains TestPyPI-only. A `v*` tag push publishes to production PyPI and then creates
the GitHub Release.

Create an annotated tag on the exact authorized commit:

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z" <authorized-full-sha>
git push origin vX.Y.Z
```

The production workflow verifies that the pushed ref is an annotated tag and that its
peeled commit, event SHA, checked-out HEAD, and freshly fetched `origin/main` are all
the exact authorized commit before it builds.

### Release failure recovery

- If candidate validation or build fails, fix the source or workflow, create a new
  candidate commit, and repeat the required checks and authorization.
- A transient infrastructure failure may be rerun against the same immutable tag.
- If a source or workflow change is required, the old tag cannot acquire that fix.
  Never move a tag to a different commit; prepare and authorize a corrected new version.
- PyPI versions are immutable. If publishing succeeds but a later test or GitHub release
  step fails, keep the published version, re-run the failed downstream job, and use a
  new version for any corrected artifacts. Never upload a replacement wheel under the
  same version.

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/dariuszpanas/django-ray/issues)
- **Discussions**: [GitHub Discussions](https://github.com/dariuszpanas/django-ray/discussions)
- **Security vulnerabilities**: read the
  [Security Policy](https://github.com/dariuszpanas/django-ray/security/policy) and use the
  [private vulnerability report](https://github.com/dariuszpanas/django-ray/security/advisories/new).
  Do not include vulnerability details, exploit instructions, credentials, or secrets in a public
  issue.

## License

By contributing, you agree that your contributions will be licensed under the BSD 3-Clause License.
