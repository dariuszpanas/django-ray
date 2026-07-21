# Django-Ray Makefile
# Core development commands for django-ray
#
# For Kubernetes deployment: see mk/k8s.mk or use `make -f mk/k8s.mk <target>`
# For load testing: see mk/loadtest.mk
# For Docker: see mk/docker.mk

.PHONY: all install format fix lint typecheck test test-unit test-integration test-postgres test-testproject test-cov check ci build clean help
.PHONY: migrate runserver shell makemigrations createsuperuser
.PHONY: worker worker-sync worker-local worker-all
.PHONY: docs-build docs-build-strict docs-serve

# Include optional modules (comment out if not needed)
-include mk/docker.mk
-include mk/k8s.mk
-include mk/tls.mk
-include mk/loadtest.mk

COVERAGE_GLOBAL_MIN ?= 95
COVERAGE_WORKER_MIN ?= 90
COVERAGE_RAY_JOB_MIN ?= 90
COVERAGE_TESTPROJECT_MIN ?= 80

# =============================================================================
# Development
# =============================================================================

# Default target - run non-mutating checks and tests
all: check test

# Install dependencies
install:
	uv sync

# Format code with Ruff
format:
	ruff format .

# Apply formatting and safe lint fixes
fix:
	ruff format .
	ruff check . --fix

# Lint code with Ruff without modifying files
lint:
	ruff check .

# Type check with ty
typecheck:
	ty check

# Run all tests
test:
	pytest

# Run unit tests only
test-unit:
	pytest tests/unit/ -v

# Run integration tests only
test-integration:
	pytest tests/integration/ -v

# Exercise database coordination against a real PostgreSQL server.
test-postgres:
	python -m pytest \
		tests/integration/test_postgresql_coordination.py \
		tests/integration/test_postgresql_workflow_progress_storage.py \
		tests/integration/test_postgresql_workflow_progress_reads.py \
		tests/integration/test_postgresql_polling.py \
		tests/integration/test_postgresql_metrics.py \
		-m postgresql -vv --durations=20

# Validate the bundled sample project's user-facing boundary
test-testproject:
	python testproject/manage.py check
	pytest tests/integration/test_api.py \
		tests/integration/test_workflow_progress_api.py \
		tests/unit/test_sample_security.py \
		tests/unit/test_testproject_workflows.py \
		--cov=testproject.api \
		--cov=testproject.views \
		--cov=testproject.urls \
		--cov-report=term \
		--cov-fail-under=$(COVERAGE_TESTPROJECT_MIN)

# Run tests with coverage
test-cov:
	pytest -m "not live_cluster" --cov=src --cov-report=html --cov-report=term --cov-fail-under=$(COVERAGE_GLOBAL_MIN)
	coverage report --include="src/django_ray/management/commands/django_ray_worker.py" --fail-under=$(COVERAGE_WORKER_MIN)
	coverage report --include="src/django_ray/runner/ray_job.py" --fail-under=$(COVERAGE_RAY_JOB_MIN)

# Run formatting, lint, and type checks without modifying files
check:
	ruff format --check .
	ruff check .
	ty check

# CI check - current-interpreter equivalents of required CI jobs, without modifications.
# Invoke as `uv run make ci` so Ray inherits one uv-managed environment.
ci:
	ruff format --check .
	ruff check .
	ty check
	pytest -m "not live_cluster" --cov=src --cov-report=xml --cov-report=term --cov-fail-under=$(COVERAGE_GLOBAL_MIN)
	coverage report --include="src/django_ray/management/commands/django_ray_worker.py" --fail-under=$(COVERAGE_WORKER_MIN)
	coverage report --include="src/django_ray/runner/ray_job.py" --fail-under=$(COVERAGE_RAY_JOB_MIN)
	$(MAKE) test-testproject
	zensical build --strict --clean
	uv build
	@echo "All CI checks passed!"

# Build the package
build:
	uv build

# Build docs
docs-build:
	uv run zensical build

# Build docs in strict mode (CI)
docs-build-strict:
	uv run zensical build --strict --clean

# Serve docs locally at http://127.0.0.1:8000
docs-serve:
	uv run zensical serve --dev-addr 127.0.0.1:8000

# =============================================================================
# Django (testproject)
# =============================================================================

migrate:
	cd testproject && python manage.py migrate

runserver:
	cd testproject && python manage.py runserver

shell:
	cd testproject && python manage.py shell

makemigrations:
	cd testproject && python manage.py makemigrations

createsuperuser:
	cd testproject && python manage.py createsuperuser

# =============================================================================
# Worker
# =============================================================================

# Start worker (default: Ray Job API mode)
worker:
	cd testproject && python manage.py django_ray_worker --queue=default

# Start worker in sync mode (no Ray, for testing)
worker-sync:
	cd testproject && python manage.py django_ray_worker --queue=default --sync

# Start worker with local Ray (recommended for development)
worker-local:
	cd testproject && python manage.py django_ray_worker --queue=default --local

# Start worker processing all queues (development)
worker-all:
	cd testproject && python manage.py django_ray_worker --all-queues --local

# Connect to Ray cluster
worker-cluster:
	cd testproject && python manage.py django_ray_worker --queue=default --cluster=ray://localhost:10001

# =============================================================================
# Utilities
# =============================================================================

# Clean up cache and build files
clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage dist build *.egg-info src/*.egg-info db.sqlite3
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Show help
help:
	@echo "Django-Ray Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  install        - Install dependencies with uv"
	@echo "  migrate        - Run Django migrations"
	@echo ""
	@echo "Development:"
	@echo "  format         - Format code with Ruff"
	@echo "  fix            - Format code and apply safe Ruff fixes"
	@echo "  lint           - Lint code with Ruff (no modifications)"
	@echo "  typecheck      - Type check with ty"
	@echo "  check          - Check formatting, lint, and types (no modifications)"
	@echo ""
	@echo "Testing:"
	@echo "  test           - Run all tests"
	@echo "  test-unit      - Run unit tests only"
	@echo "  test-integration - Run integration tests only"
	@echo "  test-postgres  - Run PostgreSQL coordination tests"
	@echo "  test-testproject - Validate the bundled sample project"
	@echo "  test-cov       - Run tests with coverage"
	@echo "  docs-build     - Build Zensical site"
	@echo "  docs-build-strict - Build Zensical site (strict mode)"
	@echo "  docs-serve     - Serve docs locally at http://127.0.0.1:8000"
	@echo ""
	@echo "Django:"
	@echo "  runserver      - Start Django dev server"
	@echo "  shell          - Open Django shell"
	@echo "  makemigrations - Create migrations"
	@echo "  createsuperuser - Create admin user"
	@echo ""
	@echo "Worker:"
	@echo "  worker         - Start worker (Ray Job API)"
	@echo "  worker-local   - Start worker (local Ray) [recommended]"
	@echo "  worker-sync    - Start worker (no Ray, for testing)"
	@echo "  worker-all     - Process all queues (local Ray)"
	@echo "  worker-cluster - Connect to ray://localhost:10001"
	@echo ""
	@echo "CI/CD:"
	@echo "  all            - Run non-mutating checks and tests"
	@echo "  ci             - Run current-interpreter CI checks, coverage, docs, and build"
	@echo "  build          - Build the package"
	@echo "  clean          - Clean cache and build files"
	@echo ""
	@echo "Additional modules (if included):"
	@echo "  Docker:     make docker-build, docker-run"
	@echo "  Kubernetes: make k8s-deploy, k8s-urls, k8s-status, k8s-delete"
	@echo "  Load test:  make loadtest, loadtest-18, loadtest-headless"
	@echo ""
	@echo "For full k8s commands: make -f mk/k8s.mk help"

