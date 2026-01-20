# Django-Ray Makefile
# Core development commands for django-ray
#
# For Kubernetes deployment: see mk/k8s.mk or use `make -f mk/k8s.mk <target>`
# For load testing: see mk/loadtest.mk
# For Docker: see mk/docker.mk

.PHONY: all install format lint typecheck test test-unit test-integration test-cov check ci build clean help
.PHONY: migrate runserver shell makemigrations createsuperuser
.PHONY: worker worker-sync worker-local worker-all

# Include optional modules (comment out if not needed)
-include mk/docker.mk
-include mk/k8s.mk
-include mk/tls.mk
-include mk/loadtest.mk

# =============================================================================
# Development
# =============================================================================

# Default target - run all checks
all: format lint typecheck test

# Install dependencies
install:
	uv sync

# Format code with Ruff
format:
	ruff format .

# Lint code with Ruff (with auto-fix)
lint:
	ruff check . --fix

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

# Run tests with coverage
test-cov:
	pytest --cov=src --cov-report=html --cov-report=term

# Run lint and typecheck (no formatting)
check:
	ruff check .
	ty check

# CI check - all validations without modifications
ci:
	ruff format --check .
	ruff check .
	ty check
	pytest
	@echo "All CI checks passed!"

# Build the package
build:
	uv build

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
	@echo "  lint           - Lint code with Ruff (auto-fix)"
	@echo "  typecheck      - Type check with ty"
	@echo "  check          - Run lint + typecheck (no formatting)"
	@echo ""
	@echo "Testing:"
	@echo "  test           - Run all tests"
	@echo "  test-unit      - Run unit tests only"
	@echo "  test-integration - Run integration tests only"
	@echo "  test-cov       - Run tests with coverage"
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
	@echo "  all            - Run format, lint, typecheck, test"
	@echo "  ci             - Run all checks (no modifications)"
	@echo "  build          - Build the package"
	@echo "  clean          - Clean cache and build files"
	@echo ""
	@echo "Additional modules (if included):"
	@echo "  Docker:     make docker-build, docker-run"
	@echo "  Kubernetes: make k8s-deploy, k8s-status, k8s-delete"
	@echo "  Load test:  make loadtest, loadtest-quick"
	@echo ""
	@echo "For full k8s commands: make -f mk/k8s.mk help"

