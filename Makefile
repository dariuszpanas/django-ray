# Django-Ray Makefile
# Core development commands for django-ray
#
# For Kubernetes deployment: see mk/k8s.mk or use `make -f mk/k8s.mk <target>`
# For load testing: see mk/loadtest.mk
# For Docker: see mk/docker.mk

.PHONY: all install format fix lint typecheck test test-xdist test-unit test-integration test-postgres test-testproject test-cov test-cov-phased _test-cov-phased-body test-suite-inventory coverage-debt check ci build clean help
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
COVERAGE_DEBT_OUTPUT_DIR ?= artifacts/coverage-debt
COVERAGE_DEBT_SOURCE_COMMIT ?= $(shell git rev-parse HEAD)
TEST_SUITE_INVENTORY_OUTPUT_DIR ?= artifacts/test-suite-inventory
TEST_SUITE_PHASED_OUTPUT_DIR ?= artifacts/test-suite-phased-coverage
TEST_SUITE_RAY_TMP_DIR ?= $(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-tmp)
TEST_SUITE_HERMETIC_EXECUTION ?= serial
TEST_SUITE_OBSERVATION ?= local-canonical
TEST_SUITE_VARIANT ?= locked-dependencies
TEST_SUITE_EXTERNAL_NOTE ?= Queue and environment setup are outside pytest timing.
TEST_XDIST_WORKERS ?= 4

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

# Run the default-resource local subset with ordinary pytest-xdist.
test-xdist:
	pytest -n $(TEST_XDIST_WORKERS) --max-worker-restart=0 \
		-m "not real_ray and not live_cluster and not postgresql"

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
		tests/integration/test_priority_migration.py \
		-m postgresql -vv --durations=20

# Validate the bundled sample project's user-facing boundary
test-testproject:
	python testproject/manage.py check
	pytest tests/integration/test_api.py \
		tests/integration/test_testproject_admin_theme.py \
		tests/integration/test_workflow_progress_api.py \
		tests/unit/test_sample_security.py \
		tests/unit/test_testproject_workflows.py \
		tests/unit/test_workflow_reporting_benchmark_command.py \
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

# Build one canonical coverage dataset while isolating external resource owners.
# Keep one outer `uv run make` boundary; the phase commands intentionally do not
# enforce coverage until hermetic, SQLite, required local-Ray, and the default
# settings self-skip remainder recreate the supported-Python canonical dataset.
test-cov-phased:
	python scripts/test_suite_benchmark.py prepare --output-dir "$(TEST_SUITE_PHASED_OUTPUT_DIR)"
	python scripts/ray_residue.py snapshot --output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-baseline.json"
	python scripts/ray_residue.py guard \
		--baseline "$(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-baseline.json" \
		--owned-temp-dir "$(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-tmp" \
		--output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-residue.json" \
		-- $(MAKE) --no-print-directory _test-cov-phased-body

# Internal body: the Python guard above owns finally-style Ray cleanup.
_test-cov-phased-body:
	@python scripts/ray_residue.py verify-guard --output-dir "$(TEST_SUITE_PHASED_OUTPUT_DIR)"
	coverage erase --data-file="$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)"
	python scripts/test_suite_inventory.py run \
		--lane hermetic \
		--execution "$(TEST_SUITE_HERMETIC_EXECUTION)" \
		--observation "$(TEST_SUITE_OBSERVATION)-hermetic" \
		--variant "$(TEST_SUITE_VARIANT)" \
		--timing-output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/hermetic.json" \
		--coverage-file "$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" \
		--ray-tmp-dir "$(TEST_SUITE_RAY_TMP_DIR)" \
		--external-note "$(TEST_SUITE_EXTERNAL_NOTE)" \
		-- --cov=src --cov-config=pyproject.toml --cov-append --cov-fail-under=0 --cov-report= -q
	python scripts/test_suite_inventory.py run \
		--lane sqlite-django \
		--execution serial \
		--observation "$(TEST_SUITE_OBSERVATION)-sqlite-django" \
		--variant "$(TEST_SUITE_VARIANT)" \
		--timing-output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/sqlite-django.json" \
		--coverage-file "$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" \
		--ray-tmp-dir "$(TEST_SUITE_RAY_TMP_DIR)" \
		--external-note "$(TEST_SUITE_EXTERNAL_NOTE)" \
		-- --cov=src --cov-config=pyproject.toml --cov-append --cov-fail-under=0 --cov-report= -q
	python scripts/test_suite_inventory.py run \
		--lane local-ray \
		--execution serial \
		--observation "$(TEST_SUITE_OBSERVATION)-local-ray" \
		--variant "$(TEST_SUITE_VARIANT)" \
		--timing-output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/local-ray.json" \
		--coverage-file "$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" \
		--ray-tmp-dir "$(TEST_SUITE_RAY_TMP_DIR)" \
		--external-note "$(TEST_SUITE_EXTERNAL_NOTE)" \
		-- --cov=src --cov-config=pyproject.toml --cov-append --cov-fail-under=0 --cov-report= -q
	python scripts/test_suite_inventory.py run \
		--lane default-serial-remainder \
		--execution serial \
		--observation "$(TEST_SUITE_OBSERVATION)-default-serial-remainder" \
		--variant "$(TEST_SUITE_VARIANT)" \
		--timing-output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/default-serial-remainder.json" \
		--coverage-file "$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" \
		--ray-tmp-dir "$(TEST_SUITE_RAY_TMP_DIR)" \
		--external-note "$(TEST_SUITE_EXTERNAL_NOTE)" \
		-- --cov=src --cov-config=pyproject.toml --cov-append --cov-fail-under=0 --cov-report= -q
	python scripts/test_suite_inventory.py collect \
		--timing "$(TEST_SUITE_PHASED_OUTPUT_DIR)/hermetic.json" \
		--timing "$(TEST_SUITE_PHASED_OUTPUT_DIR)/sqlite-django.json" \
		--timing "$(TEST_SUITE_PHASED_OUTPUT_DIR)/local-ray.json" \
		--timing "$(TEST_SUITE_PHASED_OUTPUT_DIR)/default-serial-remainder.json" \
		--json-output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/inventory.json" \
		--markdown-output "$(TEST_SUITE_PHASED_OUTPUT_DIR)/inventory.md"
	coverage report --data-file="$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" --fail-under=$(COVERAGE_GLOBAL_MIN)
	coverage report --data-file="$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" --include="src/django_ray/management/commands/django_ray_worker.py" --fail-under=$(COVERAGE_WORKER_MIN)
	coverage report --data-file="$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" --include="src/django_ray/runner/ray_job.py" --fail-under=$(COVERAGE_RAY_JOB_MIN)
	coverage xml --data-file="$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" -o "$(TEST_SUITE_PHASED_OUTPUT_DIR)/coverage.xml"
	coverage json --data-file="$(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/.coverage)" --pretty-print -o "$(TEST_SUITE_PHASED_OUTPUT_DIR)/coverage.json"

# Collect exact execution-contract and CI-lane counts without running tests.
test-suite-inventory:
	python scripts/test_suite_inventory.py collect \
		--json-output "$(TEST_SUITE_INVENTORY_OUTPUT_DIR)/test-suite-inventory.json" \
		--markdown-output "$(TEST_SUITE_INVENTORY_OUTPUT_DIR)/test-suite-inventory.md"

# Produce deterministic line-coverage debt evidence from the normal suite.
coverage-debt:
	python scripts/coverage_debt.py prepare-output --output-dir "$(COVERAGE_DEBT_OUTPUT_DIR)"
	pytest -m "not live_cluster" --cov=src --cov-config=pyproject.toml --cov-report=term --cov-fail-under=$(COVERAGE_GLOBAL_MIN)
	coverage report --include="src/django_ray/management/commands/django_ray_worker.py" --fail-under=$(COVERAGE_WORKER_MIN)
	coverage report --include="src/django_ray/runner/ray_job.py" --fail-under=$(COVERAGE_RAY_JOB_MIN)
	coverage json --rcfile=pyproject.toml --pretty-print -o "$(COVERAGE_DEBT_OUTPUT_DIR)/coverage.py.json"
	python scripts/coverage_debt.py render \
		--coverage-json "$(COVERAGE_DEBT_OUTPUT_DIR)/coverage.py.json" \
		--classifications .github/coverage-debt-classifications.json \
		--pyproject pyproject.toml \
		--source-commit "$(COVERAGE_DEBT_SOURCE_COMMIT)" \
		--json-output "$(COVERAGE_DEBT_OUTPUT_DIR)/coverage-debt.json" \
		--markdown-output "$(COVERAGE_DEBT_OUTPUT_DIR)/coverage-debt.md"

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
	uv run python scripts/validate_release.py --development --allow-release-candidate
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
	@echo "  test-xdist     - Run the default-resource subset with configurable xdist workers"
	@echo "  test-unit      - Run unit tests only"
	@echo "  test-integration - Run integration tests only"
	@echo "  test-postgres  - Run PostgreSQL coordination tests"
	@echo "  test-testproject - Validate the bundled sample project"
	@echo "  test-cov       - Run tests with coverage"
	@echo "  test-cov-phased - Build opt-in serial/xdist canonical coverage evidence"
	@echo "  test-suite-inventory - Classify collected tests by execution contract"
	@echo "  coverage-debt  - Build exact JSON and Markdown line-coverage debt reports"
	@echo "  k8s-final-gate-preflight - Validate a guarded local KubeRay gate without mutations"
	@echo "  k8s-final-gate - Run the guarded local KubeRay final integration gate"
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
	@echo "  Docker:     make docker-up, docker-smoke, docker-down"
	@echo "  Kubernetes: make k8s-deploy, k8s-urls, k8s-status, k8s-delete"
	@echo "  Load test:  make loadtest-demo, loadtest, loadtest-headless"
	@echo ""
	@echo "For full k8s commands: make -f mk/k8s.mk help"

