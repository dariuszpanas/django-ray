# Docker Deployment

This guide covers running django-ray with Docker.

## Images

### Django Application Image

```dockerfile
# Dockerfile
FROM python:3.12-slim
# ... Django app with django-ray installed
```

Build:

```bash
docker build -t django-ray:latest .
```

### Ray Worker Image

```dockerfile
# Dockerfile.ray
FROM rayproject/ray:2.53.0-py312
# ... Ray with django-ray installed for task execution
```

Build:

```bash
docker build -f Dockerfile.ray -t django-ray-worker:latest .
```

## Running Containers

### Django Web Server

```bash
# Production (gunicorn). Set all production variables from a secret manager.
docker run -p 8000:8000 \
  -e DJANGO_DEPLOYMENT_MODE=production \
  -e DJANGO_SECRET_KEY="$(openssl rand -base64 48)" \
  -e DJANGO_API_TOKEN="$(openssl rand -base64 32)" \
  -e DJANGO_ALLOWED_HOSTS=app.example.com \
  -e DATABASE_ENGINE=django.db.backends.postgresql \
  django-ray:latest web

# Local demo (not suitable for a shared or internet-facing deployment)
docker run -p 8000:8000 django-ray:latest web-dev
```

The image is production-capable only when `DJANGO_DEPLOYMENT_MODE=production` is set and
the required secret, API token, and explicit host allow-list are supplied. The health
endpoints (`/api/livez`, `/api/readyz`, and `/api/health`) remain unauthenticated so that
container and Kubernetes probes can run. Every other API route, including task arguments,
results, logs, and workflow observability, requires `Authorization: Bearer <DJANGO_API_TOKEN>`.

For a local demo, provide a token even when using the development server:

```bash
docker run -p 8000:8000 \
  -e DJANGO_DEBUG=True \
  -e DJANGO_API_TOKEN=local-demo-token \
  django-ray:latest web-dev
```

### Django-Ray Worker

```bash
# Local Ray mode
docker run django-ray:latest worker

# Cluster mode (connect to external Ray)
docker run -e RAY_ADDRESS=ray://ray-head:10001 django-ray:latest worker-cluster
```

## Docker Compose

For local development with all services:

```yaml
# docker-compose.yml
version: '3.8'

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: django_ray
      POSTGRES_USER: django_ray
      POSTGRES_PASSWORD: secret
    volumes:
      - postgres_data:/var/lib/postgresql/data

  web:
    build: .
    command: web-dev
    ports:
      - "8000:8000"
    environment:
      DJANGO_DEPLOYMENT_MODE: demo
      DJANGO_DEBUG: "True"
      DJANGO_SECRET_KEY: local-compose-only-secret
      DJANGO_API_TOKEN: local-compose-api-token
      DJANGO_ALLOWED_HOSTS: localhost,127.0.0.1
      DATABASE_HOST: postgres
      DATABASE_PASSWORD: secret
    depends_on:
      - postgres

  worker:
    build: .
    command: worker
    environment:
      DATABASE_HOST: postgres
      DATABASE_PASSWORD: secret
    depends_on:
      - postgres

volumes:
  postgres_data:
```

Run:

```bash
docker compose up
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DJANGO_DEPLOYMENT_MODE` | `demo` for local examples or `production` for fail-closed deployment checks | `demo` |
| `DJANGO_SECRET_KEY` | Django secret key; production requires a random value of at least 50 characters | local demo placeholder |
| `DJANGO_API_TOKEN` | Bearer token for all non-health API routes; production requires a random value of at least 32 characters | unset (all protected routes return 401) |
| `DJANGO_DEBUG` | Enable debug mode | `False` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hosts; production rejects `*` | `localhost,127.0.0.1` |
| `DATABASE_ENGINE` | Database backend | `sqlite3` |
| `DATABASE_NAME` | Database name | `django_ray` |
| `DATABASE_USER` | Database user | `django_ray` |
| `DATABASE_PASSWORD` | Database password | - |
| `DATABASE_HOST` | Database host | `localhost` |
| `DATABASE_PORT` | Database port | `5432` |
| `RAY_ADDRESS` | Ray cluster address | `auto` |
| `DJANGO_RAY_QUEUE` | Queue name for Docker worker modes | `default` |
| `DJANGO_RAY_QUEUES` | Comma-separated queues for Docker worker modes; overrides `DJANGO_RAY_QUEUE` | - |
| `DJANGO_RAY_CONCURRENCY` | Worker concurrency for Docker worker modes | `10` |
| `RAY_DASHBOARD_URL` | Ray Dashboard URL for admin deep links | `http://localhost:8265` |
| `RAY_MAX_RETRIES` | Sample project retry-attempt setting | `3` |
| `RAY_RETRY_DELAY_SECONDS` | Sample project retry backoff setting | `5` |

## Commands

The Docker entrypoint supports these commands:

| Command | Description |
|---------|-------------|
| `web` | Run gunicorn (production) |
| `web-dev` | Run Django dev server |
| `worker` | Run worker (local Ray) |
| `worker-cluster` | Run worker (connect to Ray cluster) |
| `migrate` | Run migrations |
| `shell` | Django shell |

Example:

```bash
# Run migrations
docker run django-ray:latest migrate

# Open shell
docker run -it django-ray:latest shell
```

## With External Ray Cluster

If you have an existing Ray cluster:

```bash
docker run \
  -e RAY_ADDRESS=ray://ray-head:10001 \
  -e DATABASE_HOST=postgres \
  -e DATABASE_PASSWORD=secret \
  django-ray:latest worker-cluster
```

## Health Checks

```dockerfile
# In Dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1
```

Validate a production container before exposing it:

```bash
docker run --rm \
  -e DJANGO_DEPLOYMENT_MODE=production \
  -e DJANGO_SECRET_KEY="$DJANGO_SECRET_KEY" \
  -e DJANGO_API_TOKEN="$DJANGO_API_TOKEN" \
  -e DJANGO_ALLOWED_HOSTS=app.example.com \
  django-ray:latest python testproject/manage.py check --deploy
```

## See Also

- [Kubernetes Deployment](kubernetes.md) - Production deployment
- [TLS Configuration](tls.md) - Securing connections

