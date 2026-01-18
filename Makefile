.PHONY: all format lint typecheck test test-unit test-integration test-cov check clean help install build migrate runserver shell docker-build docker-run k8s-build k8s-deploy k8s-delete k8s-status k8s-logs k8s-portforward k8s-restart k8s-scale-ray k8s-scale-ray-3 k8s-scale-ray-4 k8s-scale-web k8s-scale-web-2 k8s-scale-web-3 k8s-task-stats loadtest loadtest-quick loadtest-moderate loadtest-sustained loadtest-stress

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

worker-cluster:
	cd testproject && python manage.py django_ray_worker --queue=default --cluster=ray://localhost:10001

# Test connection to Ray cluster
test-cluster:
	python scripts/test_ray_cluster.py --address=ray://localhost:10001

# ============================================================================
# Load Testing (Locust)
# ============================================================================

# Default host for load testing (NodePort access, no port-forward needed)
LOADTEST_HOST ?= http://localhost:30080

# Run Locust with web UI (opens browser at http://localhost:8089)
loadtest:
	locust -f locustfile.py --host=$(LOADTEST_HOST)

# Run headless load test (100 users, 10/sec spawn rate, 60 seconds)
loadtest-quick:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 100 -r 10 -t 60s

# Run moderate load test (50 users, 5/sec spawn rate, 2 minutes)
loadtest-moderate:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 50 -r 5 -t 120s

# Run sustained load test (20 users, 2/sec spawn rate, 10 minutes)
loadtest-sustained:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 20 -r 2 -t 600s

# Run stress test (200 users, 50/sec spawn rate, 60 seconds) - USE WITH CAUTION
loadtest-stress:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 200 -r 50 -t 60s

# ============================================================================
# Docker Commands
# ============================================================================

# Build Docker image
docker-build:
	docker build -t django-ray:latest .

# Build Docker dev image
docker-build-dev:
	docker build -f Dockerfile.dev -t django-ray:dev .

# Run Docker container (Django web - production with gunicorn)
docker-run:
	docker run -p 8000:8000 django-ray:latest web

# Run Docker container (Django web - development server)
docker-run-dev:
	docker run -p 8000:8000 -v $(PWD)/db.sqlite3:/app/db.sqlite3 django-ray:latest web-dev

# Run Docker container (Django-Ray worker - local Ray)
docker-run-worker:
	docker run django-ray:latest worker

# ============================================================================
# Kubernetes Commands (Docker Desktop / any cluster)
# ============================================================================

# Build Docker images for Kubernetes
k8s-build:
	@echo "Building Django web image..."
	docker build -t django-ray:dev .
	@echo "Building Ray worker image (with django-ray installed)..."
	docker build -f Dockerfile.ray -t django-ray-worker:dev .

# Deploy to Kubernetes cluster
k8s-deploy: k8s-build
	kubectl apply -k k8s/overlays/dev
	@echo "Waiting for deployments..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	@echo ""
	@echo "Deployment complete! Run 'make k8s-portforward' to access the application."

# Deploy with full resources (for powerful machines: 16+ CPUs, 64GB+ RAM, GPU)
k8s-deploy-local: k8s-build
	kubectl apply -k k8s/overlays/local
	@echo "Waiting for deployments..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	@echo ""
	@echo "Deployment complete! Run 'make k8s-portforward' to access the application."
	@echo "Ray cluster: 2 workers x 4 CPUs = 8 CPUs total for tasks"

# Delete deployment from Kubernetes
k8s-delete:
	kubectl delete -k k8s/overlays/dev --ignore-not-found

# Complete reset - delete namespace and redeploy fresh (clears database)
k8s-reset:
	@echo "Deleting namespace django-ray..."
	kubectl delete namespace django-ray --ignore-not-found --wait=true
	@echo "Waiting for namespace to be fully deleted..."
	@powershell -Command "while (kubectl get namespace django-ray -o name 2>$$null) { Start-Sleep -Seconds 2 }"
	@echo "Redeploying..."
	$(MAKE) k8s-deploy-local

# Show deployment status
k8s-status:
	@echo "=== Pods ==="
	kubectl get pods -n django-ray
	@echo ""
	@echo "=== Services ==="
	kubectl get svc -n django-ray
	@echo ""
	@echo "=== Deployments ==="
	kubectl get deployments -n django-ray

# Port forward Django web (foreground - Ctrl+C to stop)
# NOTE: With NodePort, you can access directly at http://localhost:30080
k8s-portforward-web:
	@echo "Starting Django Web port forward on http://localhost:8000"
	@echo "NOTE: You can also access directly via NodePort at http://localhost:30080"
	@echo "Press Ctrl+C to stop"
	kubectl port-forward svc/django-web-svc 8000:80 -n django-ray

# Port forward Ray dashboard (foreground - Ctrl+C to stop)
# NOTE: With NodePort, you can access directly at http://localhost:30265
k8s-portforward-ray:
	@echo "Starting Ray Dashboard port forward on http://localhost:8265"
	@echo "NOTE: You can also access directly via NodePort at http://localhost:30265"
	@echo "Press Ctrl+C to stop"
	kubectl port-forward svc/ray-head-svc 8265:8265 -n django-ray

# Port forward all services
# NOTE: With NodePort services, you can skip port-forwarding and access directly:
#   Django Web:     http://localhost:30080
#   Ray Dashboard:  http://localhost:30265
k8s-portforward:
	@echo "Starting port forwards..."
	@echo ""
	@echo "  Django Web:     http://localhost:8000 (or http://localhost:30080 via NodePort)"
	@echo "  Swagger UI:     http://localhost:8000/api/docs"
	@echo "  Ray Dashboard:  http://localhost:8265 (or http://localhost:30265 via NodePort)"
	@echo ""
ifeq ($(OS),Windows_NT)
	@echo "Opening port forwards in new windows..."
	@powershell -Command "Start-Process -FilePath 'kubectl' -ArgumentList 'port-forward','svc/django-web-svc','8000:80','-n','django-ray'"
	@powershell -Command "Start-Process -FilePath 'kubectl' -ArgumentList 'port-forward','svc/ray-head-svc','8265:8265','-n','django-ray'"
	@echo "Close the terminal windows to stop port forwards, or run: make k8s-portforward-stop"
else
	@kubectl port-forward svc/django-web-svc 8000:80 -n django-ray > /dev/null 2>&1 &
	@kubectl port-forward svc/ray-head-svc 8265:8265 -n django-ray > /dev/null 2>&1 &
	@echo "Run 'make k8s-portforward-stop' to stop all port forwards"
endif

# Stop all kubectl port-forward processes
k8s-portforward-stop:
	@echo "Stopping all kubectl port-forward processes..."
ifeq ($(OS),Windows_NT)
	@taskkill /F /IM kubectl.exe >nul 2>&1 && echo "Port forwards stopped." || echo "No kubectl processes found."
else
	@pkill -f "kubectl port-forward" 2>/dev/null && echo "Port forwards stopped." || echo "No port-forward processes found"
endif

# Show logs for all django-ray components
k8s-logs:
	kubectl logs -n django-ray -l app=django-ray --tail=50 -f

# Show logs for specific components
k8s-logs-web:
	kubectl logs -n django-ray -l app=django-ray,component=web --tail=50 -f

k8s-logs-worker:
	kubectl logs -n django-ray -l app=django-ray,component=worker --tail=50 -f

k8s-logs-ray:
	kubectl logs -n django-ray -l app=ray --tail=50 -f

k8s-logs-postgres:
	kubectl logs -n django-ray -l app=postgres --tail=50 -f

# Restart deployments
k8s-restart:
	kubectl rollout restart deployment/django-web -n django-ray
	kubectl rollout restart deployment/django-ray-worker -n django-ray

k8s-restart-ray:
	kubectl rollout restart deployment/ray-head -n django-ray
	kubectl rollout restart deployment/ray-worker -n django-ray

# Scale Ray workers (default: 2)
k8s-scale-ray:
	@echo "Current Ray worker replicas:"
	@kubectl get deployment ray-worker -n django-ray -o jsonpath='{.spec.replicas}'
	@echo ""
	@echo "To scale: kubectl scale deployment/ray-worker --replicas=N -n django-ray"

# Scale to specific replica count
k8s-scale-ray-3:
	kubectl scale deployment/ray-worker --replicas=3 -n django-ray

k8s-scale-ray-4:
	kubectl scale deployment/ray-worker --replicas=4 -n django-ray

# Scale Django web pods for higher API throughput
k8s-scale-web:
	@echo "Current Django web replicas:"
	@kubectl get deployment django-web -n django-ray -o jsonpath='{.spec.replicas}'
	@echo ""
	@echo "To scale: kubectl scale deployment/django-web --replicas=N -n django-ray"

k8s-scale-web-2:
	kubectl scale deployment/django-web --replicas=2 -n django-ray

k8s-scale-web-3:
	kubectl scale deployment/django-web --replicas=3 -n django-ray

# Update django-ray worker concurrency (how many tasks run in parallel)
# This is the PRIMARY setting that controls parallelism
k8s-set-concurrency-20:
	kubectl set env deployment/django-ray-worker DJANGO_RAY_CONCURRENCY=20 -n django-ray

k8s-set-concurrency-50:
	kubectl set env deployment/django-ray-worker DJANGO_RAY_CONCURRENCY=50 -n django-ray

k8s-set-concurrency-100:
	kubectl set env deployment/django-ray-worker DJANGO_RAY_CONCURRENCY=100 -n django-ray

# Show current concurrency setting
k8s-get-concurrency:
	@echo "Current DJANGO_RAY_CONCURRENCY:"
	@kubectl get deployment django-ray-worker -n django-ray -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DJANGO_RAY_CONCURRENCY")].value}'
	@echo ""

# Show current task queue stats (requires port-forward or NodePort)
k8s-task-stats:
	@echo "Task Queue Stats:"
ifeq ($(OS),Windows_NT)
	@powershell -Command "try { Invoke-RestMethod http://localhost:30080/api/tasks/stats | Format-List } catch { Write-Host 'Cannot connect - is the service running?' }"
else
	@curl -s http://localhost:30080/api/tasks/stats 2>/dev/null || echo "Cannot connect - is the service running?"
endif

# Reset stuck RUNNING tasks back to QUEUED (workaround for orphaned tasks)
k8s-reset-stuck:
	@echo "Resetting stuck tasks..."
ifeq ($(OS),Windows_NT)
	@powershell -Command "try { Invoke-RestMethod -Method POST http://localhost:30080/api/tasks/reset | Format-List } catch { Write-Host 'Cannot connect - is the service running?' }"
else
	@curl -s -X POST http://localhost:30080/api/tasks/reset 2>/dev/null || echo "Cannot connect - is the service running?"
endif

# Shell into a pod
k8s-shell-web:
	kubectl exec -it -n django-ray deployment/django-web -- /bin/bash

k8s-shell-worker:
	kubectl exec -it -n django-ray deployment/django-ray-worker -- /bin/bash

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
	@echo "  test-cluster     - Test connection to Ray cluster (ray://localhost:10001)"
	@echo ""
	@echo "Load Testing (validates Django→Ray integration under load):"
	@echo "  loadtest         - Run Locust with web UI (http://localhost:8089)"
	@echo "  loadtest-quick   - 100 users, 60s - validates system stability"
	@echo "  loadtest-moderate - 50 users, 2min"
	@echo "  loadtest-sustained - 20 users, 10min - long-running stability"
	@echo "  loadtest-stress  - 200 users, 60s (may overwhelm resources)"
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
	@echo "  worker-cluster   - Start django-ray worker (connect to ray://localhost:10001)"
	@echo "  worker-sync      - Start django-ray worker (sync mode, no Ray)"
	@echo ""
	@echo "Docker:"
	@echo "  docker-build     - Build production Docker image"
	@echo "  docker-build-dev - Build development Docker image"
	@echo "  docker-run       - Run web server (gunicorn)"
	@echo "  docker-run-dev   - Run dev web server (with local DB)"
	@echo "  docker-run-worker - Run task worker (local Ray)"
	@echo ""
	@echo "Kubernetes (Docker Desktop / any cluster):"
	@echo "  k8s-build        - Build Docker image for Kubernetes"
	@echo "  k8s-deploy       - Build and deploy (conservative, works everywhere)"
	@echo "  k8s-deploy-local - Deploy with full resources (16+ CPUs, 32GB+ RAM)"
	@echo "  k8s-delete       - Delete deployment from Kubernetes"
	@echo "  k8s-status       - Show deployment status"
	@echo "  k8s-task-stats   - Show current task queue stats"
	@echo "  k8s-reset-stuck  - Reset stuck RUNNING/FAILED tasks to QUEUED"
	@echo ""
	@echo "Scaling (for load testing):"
	@echo "  k8s-get-concurrency    - Show current task concurrency limit"
	@echo "  k8s-set-concurrency-20 - Set to 20 parallel tasks"
	@echo "  k8s-set-concurrency-50 - Set to 50 parallel tasks"
	@echo "  k8s-set-concurrency-100 - Set to 100 parallel tasks"
	@echo "  k8s-scale-ray-3  - Scale Ray workers to 3 replicas"
	@echo "  k8s-scale-ray-4  - Scale Ray workers to 4 replicas"
	@echo "  k8s-scale-web-2  - Scale Django web to 2 replicas"
	@echo "  k8s-scale-web-3  - Scale Django web to 3 replicas"
	@echo ""
	@echo "Port Forwarding (optional with NodePort):"
	@echo "  k8s-portforward  - Start all port forwards (background)"
	@echo "  k8s-portforward-web  - Port forward Django web only (8000)"
	@echo "  k8s-portforward-ray  - Port forward Ray dashboard only (8265)"
	@echo "  k8s-portforward-stop - Stop all port forwards"
	@echo ""
	@echo "Logs & Debugging:"
	@echo "  k8s-logs         - Show all django-ray logs"
	@echo "  k8s-logs-web     - Show Django web logs"
	@echo "  k8s-logs-worker  - Show django-ray worker logs"
	@echo "  k8s-logs-ray     - Show Ray cluster logs"
	@echo "  k8s-restart      - Restart Django deployments"
	@echo "  k8s-restart-ray  - Restart Ray deployments"
	@echo "  k8s-shell-web    - Shell into web pod"
	@echo "  k8s-shell-worker - Shell into worker pod"
	@echo ""
	@echo "Direct Access (NodePort - no port-forward needed):"
	@echo "  Django Web:      http://localhost:30080"
	@echo "  Swagger UI:      http://localhost:30080/api/docs"
	@echo "  Django Admin:    http://localhost:30080/admin/"
	@echo "  Ray Dashboard:   http://localhost:30265"
	@echo ""
	@echo "API (run 'make runserver' first):"
	@echo "  Swagger UI:      http://127.0.0.1:8000/api/docs"
	@echo "  Task management, enqueueing, and monitoring via REST API"
	@echo ""
	@echo "Utilities:"
	@echo "  clean            - Clean up cache and build files"
	@echo "  help             - Show this help message"

