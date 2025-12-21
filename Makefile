.PHONY: all format lint typecheck test test-unit test-integration test-cov check clean help install build migrate runserver shell

# Default target - run everything
all: format lint typecheck test

# Install dependencies
install:
	uv sync

# Format code with Ruff
format:
	ruff format .

# Check formatting without modifying
format-check:
	ruff format --check .

# Lint code with Ruff (with auto-fix)
lint:
	ruff check . --fix

# Lint check only (no auto-fix)
lint-check:
	ruff check .

# Type check with ty
# Note: Ignoring Django ORM-related errors (ty doesn't support Django stubs yet)
typecheck:
	ty check --ignore unresolved-attribute --ignore invalid-argument-type --ignore possibly-missing-attribute

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

# Run lint and typecheck without formatting
check: lint-check typecheck

# CI check - all validations without modifications
ci: format-check lint-check typecheck test
	@echo "All CI checks passed!"

# Build the package
build:
	uv build

# Django commands for testproject
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

# Worker commands
worker:
	cd testproject && python manage.py django_ray_worker --queue=default

worker-sync:
	cd testproject && python manage.py django_ray_worker --queue=default --sync

worker-local:
	cd testproject && python manage.py django_ray_worker --queue=default --local


# Clean up cache and build files
clean:
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	rm -rf htmlcov
	rm -rf .coverage
	rm -rf dist
	rm -rf build
	rm -rf *.egg-info
	rm -rf src/*.egg-info
	rm -rf db.sqlite3
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Show help
help:
	@echo "Available targets:"
	@echo ""
	@echo "Prerequisites: Activate the virtual environment first!"
	@echo "  Windows: .venv\\Scripts\\activate"
	@echo "  Linux/Mac: source .venv/bin/activate"
	@echo ""
	@echo "Development:"
	@echo "  install          - Install dependencies with uv"
	@echo "  format           - Format code with Ruff"
	@echo "  format-check     - Check formatting without modifying"
	@echo "  lint             - Lint code with Ruff (with auto-fix)"
	@echo "  lint-check       - Lint code without auto-fix"
	@echo "  typecheck        - Type check with ty"
	@echo ""
	@echo "Testing:"
	@echo "  test             - Run all tests with pytest"
	@echo "  test-unit        - Run unit tests only"
	@echo "  test-integration - Run integration tests only"
	@echo "  test-cov         - Run tests with coverage report"
	@echo ""
	@echo "CI/CD:"
	@echo "  all              - Run format, lint, typecheck, and test"
	@echo "  check            - Run lint-check and typecheck (no formatting)"
	@echo "  ci               - Run all CI checks (no modifications)"
	@echo "  build            - Build the package"
	@echo ""
	@echo "Django (testproject):"
	@echo "  migrate          - Run Django migrations"
	@echo "  makemigrations   - Create new migrations"
	@echo "  runserver        - Start Django development server"
	@echo "  shell            - Open Django shell"
	@echo "  createsuperuser  - Create a Django superuser"
	@echo ""
	@echo "Worker:"
	@echo "  worker           - Start django-ray worker (Ray Job API mode)"
	@echo "  worker-local     - Start django-ray worker (local Ray, recommended)"
	@echo "  worker-sync      - Start django-ray worker (sync mode, no Ray)"
	@echo ""
	@echo "API (run 'make runserver' first):"
	@echo "  Swagger UI:      http://127.0.0.1:8000/api/docs"
	@echo "  Task management, enqueueing, and monitoring via REST API"
	@echo ""
	@echo "Utilities:"
	@echo "  clean            - Clean up cache and build files"
	@echo "  help             - Show this help message"

