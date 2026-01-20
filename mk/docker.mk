# Docker and Kubernetes deployment commands
# Include in main Makefile with: include mk/docker.mk

.PHONY: docker-build docker-build-dev docker-run docker-run-dev docker-run-worker

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

