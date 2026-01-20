# Contributing

Thank you for your interest in contributing to django-ray!

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- Git

### Clone and Install

```bash
git clone https://github.com/yourusername/django-ray.git
cd django-ray
uv sync
```

### Verify Setup

```bash
make test
```

## Development Workflow

### Code Style

We use automated tools to maintain consistent code style:

```bash
# Format code
make format

# Lint code
make lint

# Type check
make typecheck

# Run all checks
make check
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
make test-cov
```

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
5. **Run all checks**: `make ci`
6. **Submit a pull request** with a clear description

### Commit Messages

Use clear, descriptive commit messages:

```
feat: add support for task priorities
fix: handle connection timeout in Ray client
docs: update deployment guide for TLS
test: add unit tests for retry logic
```

### PR Checklist

- [ ] Tests pass (`make test`)
- [ ] Linting passes (`make lint`)
- [ ] Type checking passes (`make typecheck`)
- [ ] Documentation updated (if needed)
- [ ] Changelog updated (for user-facing changes)

## Code Organization

```
src/django_ray/
├── models.py           # Database models
├── admin.py            # Admin interface
├── backends.py         # Django Task backend
├── conf/               # Settings
├── runner/             # Task runners
│   ├── ray_job.py      # Ray Job API
│   ├── ray_core.py     # Ray Core (@ray.remote)
│   └── ...
├── runtime/            # Task execution
│   ├── entrypoint.py   # Execution entry
│   ├── distributed.py  # parallel_map, etc.
│   └── ...
└── management/commands/
    └── django_ray_worker.py
```

## Testing Guidelines

### Unit Tests

Test individual components in isolation:

```python
# tests/unit/test_retry.py
def test_should_retry_on_retryable_error():
    """Test that retryable errors trigger retry."""
    ...
```

### Integration Tests

Test components working together:

```python
# tests/integration/test_worker.py
def test_worker_processes_task():
    """Test full task processing flow."""
    ...
```

### Test Fixtures

Use pytest fixtures for common setup:

```python
@pytest.fixture
def task_execution():
    return RayTaskExecution.objects.create(
        task_id="test-task",
        task_name="myapp.tasks.test",
        state=TaskState.QUEUED,
    )
```

## Documentation

Documentation is in `docs/` as Markdown files.

### Building Docs Locally

Currently docs are plain Markdown. For local preview, use any Markdown viewer or:

```bash
# Using Python
python -m http.server -d docs 8080
```

## Releasing

Releases are automated via GitHub Actions:

1. Update version in `pyproject.toml` and `src/django_ray/__init__.py`
2. Update `docs/changelog.md`
3. Create and push a tag:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The release workflow will:
- Build the package
- Run tests
- Publish to PyPI
- Create GitHub release

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/dpanas/django-ray/issues)
- **Discussions**: [GitHub Discussions](https://github.com/dpanas/django-ray/discussions)

## License

By contributing, you agree that your contributions will be licensed under the BSD 3-Clause License.

