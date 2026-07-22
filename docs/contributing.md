# Contributing

Thank you for your interest in contributing to django-ray!

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
mechanical wrapping. Structurally complete Dependabot messages keep their generated validation path
without a bot-wide exception.

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

The required `Commit Messages` GitHub Actions check validates the PR title and the full message of
every commit in the PR. Use one of `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`,
`revert`, `style`, or `test`; an optional scope and `!` are allowed:

```text
<type>[optional scope][!]: <imperative summary>
```

The check enforces meaningful body context, validation evidence or a specific not-run reason, and the
wrappable commit-prose line limit without prescribing section headings. It reports the offending
commit or line.

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

`uv run make ci` runs the required format, lint, type, coverage, strict-documentation, and package-build
checks for the current interpreter. GitHub Actions additionally repeats tests across supported Python
versions and minimum/latest dependency resolutions.

### Coverage debt review

The hard floors are safety margins, not coverage targets. Build the same report used by the monthly
tracker from a local checkout with:

```bash
uv run make coverage-debt
```

The target runs the normal `not live_cluster` suite, including tests marked `real_ray`, and reads the
central coverage settings in `pyproject.toml`. It writes these ignored artifacts under
`artifacts/coverage-debt/`:

- `coverage.py.json`: Coverage.py's source evidence;
- `coverage-debt.json`: exact covered, missed, and statement totals plus every uncovered range;
- `coverage-debt.md`: the same measurements and ranges in reviewable Markdown.

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
runtime, workflow, or release changes. It uploads all three artifacts, appends the Markdown report to
the job summary, and updates one bot-owned comment on the issue containing
`<!-- django-ray:coverage-debt-tracker -->`. The updater scans all issues and fails before writing if
the tracker marker or latest-report comment is duplicated. Its first run seeds current, previous, and
high-water measurements; later runs move current to previous and retain the exact best ratio.
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
- CI also supports `workflow_dispatch` for a manual rerun; it does not need a repository variable
  or an externally reachable Ray cluster.

### Pinned Compiled Graph KubeRay Pilot (Opt-In)

The Compiled Graph candidate matrix in ordinary CI is discovery evidence. It does not replace the
exact Linux/KubeRay profile required by the fail-closed capability policy. Maintainers collecting
promotion-grade issue #102 evidence must use a clean commit, the pinned `desktop-linux` Docker CLI
context, the `docker-desktop` Kubernetes context, KubeRay operator 1.6.2, and the dedicated pilot
namespace:

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

CI runs this gate on Python 3.12 and Django 6.0, keeps it separate from coverage, and prints server
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
5. **Run all checks**: `uv run make ci`
6. **Submit a pull request** with a Conventional Commit title, naturally formatted Markdown, clear
   description, validation results, and `Closes #<number>` when applicable

### Rebase auto-merge

Pull requests are the unit of review and delivery, but their individual commits should remain visible.
Keep commits focused and do not squash the PR. Once the required checks pass, enable rebase auto-merge:

```bash
gh pr merge --auto --rebase <PR-number>
```

The `Commit Messages` workflow runs on `pull_request_target`, validates the PR title and every full
commit message, and reports a required status check without needing secrets from the PR. The separate
required `CI Gate` runs after every blocking job and passes only when lint, docs, typing, all supported
Python tests, PostgreSQL coordination, live-cluster faults, testproject, minimum/latest dependencies,
Compiled Graph candidates, and package build all report `success`. Its `always()` condition makes a
failed, cancelled, timed-out, or skipped dependency visible as a failed gate instead of a successful
skip. This repository is private, so use rebase auto-merge rather than merge queue: auto-merge waits
for both protected checks and then applies the rebase method.

Scheduled/manual Compiled Graph canary, coverage-debt review, and benchmark workflows are evidence
producers, not merge checks. Documentation builds outside pull requests and release workflows run
after merge, manually, or from tags. Codecov upload is advisory inside the otherwise blocking Python
3.12 job. Add future PR CI jobs to `CI Gate` unless contributor policy explicitly documents why they
are non-blocking.

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
conflicts, run `uv run make ci`, and push. Auto-merge will recalculate the required checks. The
`main` ruleset requires `Commit Messages` and `CI Gate` from GitHub Actions, allows rebase merges only,
and does not require approval in the sole-developer workflow. The owner's bypass is limited to pull requests,
so routine merges remain gated and emergency use requires the explicit
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

- [ ] CI-equivalent checks pass (`uv run make ci`)
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

Plain pytest reports selected skips without failing its exit status. Record required local-Ray evidence
through the manifest runner so its `forbid` skip policy proves that all 22 selected cases executed:

```bash
uv run python scripts/test_suite_inventory.py run \
  --lane local-ray \
  --observation local-required-ray \
  --variant locked-dependencies \
  --timing-output artifacts/test-suite-inventory/local-ray-timing.json \
  --external-note "uv environment already synchronized; setup time excluded" \
  -- -v
```

Tests that request `ray_cluster` or `live_ray_cluster` are checked during collection for the
matching marker. Add a new external-resource fixture to `EXTERNAL_RESOURCE_FIXTURE_MARKERS` in
`tests/conftest.py` so an unmarked consumer cannot silently enter another lane.
`compiled_graph_opt_in` also fails collection unless the same case carries `real_ray`.

The current post-#168 collection has 23 raw `real_ray` marker cases: 22 required local-Ray cases and
one separately owned `compiled_graph_opt_in` case. It also has 32 `postgresql` cases, 3
`live_cluster` cases, and 85 path-selected testproject cases. This is distinct from issue #166's
frozen 2026-07-22 comparison snapshot, which records 33 `local-ray` cases before this ownership
correction and must remain unchanged. The five required real-Ray cases in
`TestRayRemoteExecution` share one module-scoped runtime and dashboard on port 8265; the other ten
tests in that module are ordinary in-process Django tests. Recheck marker counts without executing
their resources:

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
`make docs-build-strict`, which runs `uv run zensical build --strict`.

Read the Docs hosting is configured by `.readthedocs.yaml`. Because Zensical is a custom static
site generator from Read the Docs' perspective, that config builds with Zensical and copies the
generated `site/` directory into `$READTHEDOCS_OUTPUT/html`.

## Releasing

Releases are automated via GitHub Actions:

1. Update the same version in `pyproject.toml` and `src/django_ray/__init__.py`.
2. Update `docs/changelog.md` and run the release checks locally:

```bash
uv run python scripts/validate_release.py vX.Y.Z
uv build
uv venv --clear
uv pip install dist/*.whl
uv run --no-sync python scripts/verify_wheel.py --version X.Y.Z
```

3. Create and push a tag:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

The workflow validates that the tag (or the manual `version` input) matches both
package version sources before building. It then tests the installed wheel's metadata,
migrations, management-command discovery, and expected package contents on every
supported Python version before publishing.

For a manual dispatch, enter the exact version already committed to the branch. Manual
dispatches publish to TestPyPI; versioned tag pushes publish to PyPI and create the
GitHub release.

### Release failure recovery

- If validation or build fails, fix the source or workflow and push a new commit; do not
  move a tag to a different commit.
- If a tag build fails before publishing, re-run the failed workflow after the fix or
  push a new patch-version tag once the commit is ready.
- PyPI versions are immutable. If publishing succeeds but a later test or GitHub release
  step fails, keep the published version, re-run the failed downstream job, and use a
  new version for any corrected artifacts. Never upload a replacement wheel under the
  same version.

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/dariuszpanas/django-ray/issues)
- **Discussions**: [GitHub Discussions](https://github.com/dariuszpanas/django-ray/discussions)

## License

By contributing, you agree that your contributions will be licensed under the BSD 3-Clause License.

