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
# Production (gunicorn)
docker run -p 8000:8000 django-ray:latest web

# Development
docker run -p 8000:8000 django-ray:latest web-dev
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
docker-compose up
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DJANGO_SECRET_KEY` | Django secret key | (required) |
| `DJANGO_DEBUG` | Enable debug mode | `False` |
| `DJANGO_ALLOWED_HOSTS` | Allowed hosts | `*` |
| `DATABASE_ENGINE` | Database backend | `sqlite3` |
| `DATABASE_NAME` | Database name | `django_ray` |
| `DATABASE_USER` | Database user | `django_ray` |
| `DATABASE_PASSWORD` | Database password | - |
| `DATABASE_HOST` | Database host | `localhost` |
| `DATABASE_PORT` | Database port | `5432` |
| `RAY_ADDRESS` | Ray cluster address | `auto` |

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

## See Also

- [Kubernetes Deployment](kubernetes.md) - Production deployment
- [TLS Configuration](tls.md) - Securing connections

