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

Use Conventional Commit syntax for commits and PR titles:

```text
feat: add runtime environment cache metrics
fix(worker): preserve completion during timeout cancellation
docs: clarify Ray Job worker selection
chore(deps): update Ruff
```

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
- `src/django_ray/management/commands/django_ray_worker.py`: `>= 50%`
- `src/django_ray/runner/ray_job.py`: `>= 55%`

`uv run make ci` runs the required format, lint, type, coverage, strict-documentation, and package-build
checks for the current interpreter. GitHub Actions additionally repeats tests across supported Python
versions and minimum/latest dependency resolutions.

### Live Cluster Fault Tests (Opt-In)

`tests/integration/test_live_failure_injection.py` runs against a real Ray cluster and is skipped by default.

Enable it explicitly:

```bash
# PowerShell
$env:DJANGO_RAY_LIVE_CLUSTER_TESTS="1"
$env:DJANGO_RAY_LIVE_RAY_ADDRESS="ray://localhost:10001"
$env:DJANGO_RAY_LIVE_MIN_NODES="2"
uv run pytest tests/integration/test_live_failure_injection.py -v
```

Environment variables:

- `DJANGO_RAY_LIVE_CLUSTER_TESTS`: set to `1/true/yes` to enable suite.
- `DJANGO_RAY_LIVE_RAY_ADDRESS`: Ray address for live cluster tests.
- `DJANGO_RAY_LIVE_MIN_NODES`: minimum alive node count required before tests run.

CI strategy:

- Default CI (`.github/workflows/ci.yml`) excludes `live_cluster` tests to keep PR checks deterministic.
- Live cluster tests run in a dedicated manual workflow: `.github/workflows/live-cluster.yml`.
- Trigger the workflow with `ray_address` input, or set repository variable `DJANGO_RAY_LIVE_RAY_ADDRESS`.

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

1. **Fork the repository** and create a branch from `main`
2. **Make your changes** with clear, focused commits
3. **Add tests** for new functionality
4. **Update documentation** if needed
5. **Run all checks**: `uv run make ci`
6. **Submit a pull request** with a Conventional Commit title, clear description, validation results,
   and `Closes #<number>` when applicable

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
- [ ] Exact validation commands and results included in the PR description

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

