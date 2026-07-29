# Tracked Docker Compose quickstart commands.
#
# DJANGO_API_TOKEN and POSTGRES_PASSWORD must be generated in the calling shell.
# See testproject/README.md for copyable POSIX and PowerShell setup.

.PHONY: docker-build docker-build-dev docker-up docker-smoke docker-down
.PHONY: docker-run docker-run-dev docker-run-worker

docker-build:
	docker compose build

docker-build-dev:
	docker build -f Dockerfile.dev -t django-ray:dev .

docker-up:
	docker compose up --build --detach web worker

docker-smoke:
	docker compose --profile smoke run --rm --no-deps smoke

docker-down:
	docker compose --profile smoke down --volumes --remove-orphans

# Backward-compatible aliases now use the migrated shared-PostgreSQL topology.
docker-run: docker-up

docker-run-dev: docker-up

docker-run-worker:
	docker compose up --build --detach worker
