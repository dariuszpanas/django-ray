# Kubernetes Deployment

This guide covers deploying django-ray to Kubernetes.

## Prerequisites

- Kubernetes cluster (Docker Desktop, k3d, kind, or cloud provider)
- kubectl configured to access your cluster
- Docker for building images

## Quick Start

### 1. Build Images

```bash
# Build Django application image
docker build -t django-ray:latest .

# Build Ray worker image
docker build -f Dockerfile.ray -t django-ray-worker:latest .
```

### 2. Deploy

```bash
# Deploy using Kustomize
kubectl apply -k k8s/overlays/dev

# Wait for pods
kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s
kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s
kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s
kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s
```

### 3. Access

| Service | URL | Description |
|---------|-----|-------------|
| Django Web | http://localhost:30080 | Application |
| API Docs | http://localhost:30080/api/docs | Swagger UI |
| Admin | http://localhost:30080/admin/ | Django Admin |
| Ray Dashboard | http://localhost:30265 | Ray monitoring |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Kubernetes Cluster                       │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  PostgreSQL  │  │  Django Web  │  │  Django-Ray      │  │
│  │              │  │  (API/Admin) │  │  Worker          │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│         │                 │                   │              │
│         └─────────────────┴───────────────────┘              │
│                           │                                  │
│  ┌────────────────────────┴────────────────────────────┐    │
│  │                    Ray Cluster                       │    │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐     │    │
│  │  │  Ray Head  │  │ Ray Worker │  │ Ray Worker │     │    │
│  │  │ (Dashboard)│  │            │  │            │     │    │
│  │  └────────────┘  └────────────┘  └────────────┘     │    │
│  └──────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Components

### PostgreSQL

Database for Django and task metadata.

```yaml
# k8s/base/postgres.yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

### Django Web

Web application and API server.

```yaml
# k8s/base/django-web.yaml
replicas: 1
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

### Django-Ray Worker

Task processor that submits to Ray.

```yaml
# k8s/base/django-ray-worker.yaml
env:
  - name: RAY_ADDRESS
    value: "ray://ray-head-svc:10001"
  - name: DJANGO_RAY_CONCURRENCY
    value: "40"
```

### Ray Cluster

Ray head and worker nodes.

```yaml
# k8s/base/ray-cluster.yaml
# Ray Head
resources:
  requests:
    memory: "8Gi"
    cpu: "2"
  limits:
    memory: "12Gi"
    cpu: "4"

# Ray Workers (replicas: 2)
resources:
  requests:
    memory: "8Gi"
    cpu: "2"
  limits:
    memory: "12Gi"
    cpu: "4"
```

## Scaling

### Scale Ray Workers

```bash
kubectl scale deployment/ray-worker --replicas=4 -n django-ray
```

### Scale Django Web

```bash
kubectl scale deployment/django-web --replicas=3 -n django-ray
```

### Adjust Worker Concurrency

```bash
kubectl set env deployment/django-ray-worker DJANGO_RAY_CONCURRENCY=100 -n django-ray
```

## Configuration

### Environment Variables

Set via ConfigMap:

```yaml
# k8s/base/configmap.yaml
data:
  DJANGO_DEBUG: "False"
  DJANGO_ALLOWED_HOSTS: "*"
  DATABASE_ENGINE: "django.db.backends.postgresql"
  DATABASE_HOST: "postgres-svc"
```

### Secrets

Set via Secret:

```yaml
# k8s/base/secret.yaml
data:
  DJANGO_SECRET_KEY: <base64-encoded>
  DATABASE_PASSWORD: <base64-encoded>
```

## Overlays

### Development (default)

```bash
kubectl apply -k k8s/overlays/dev
```

- Lower resource limits
- Single replicas
- Debug enabled

### Local (high resources)

```bash
kubectl apply -k k8s/overlays/local
```

- Higher resource limits
- Optimized for powerful machines

### TLS Enabled

```bash
# Generate certificates first
./scripts/generate-ray-tls-certs.sh

# Deploy with TLS
kubectl apply -k k8s/overlays/dev-tls
```

See [TLS Configuration](tls.md) for details.

## Monitoring

### View Logs

```bash
# All components
kubectl logs -n django-ray -l app=django-ray -f

# Django web
kubectl logs -n django-ray -l app=django-ray,component=web -f

# Worker
kubectl logs -n django-ray -l app=django-ray,component=worker -f

# Ray
kubectl logs -n django-ray -l app=ray -f
```

### Check Task Stats

```bash
kubectl exec -n django-ray deployment/django-web -- \
  python manage.py shell -c "
from django_ray.models import RayTaskExecution, TaskState
for state in TaskState:
    count = RayTaskExecution.objects.filter(state=state).count()
    print(f'{state}: {count}')
"
```

### Prometheus Metrics

Metrics are available at `/api/metrics`:

```bash
curl http://localhost:30080/api/metrics
```

## Troubleshooting

### Pods Not Starting

```bash
# Check pod status
kubectl get pods -n django-ray

# Check events
kubectl get events -n django-ray --sort-by='.lastTimestamp'

# Describe failing pod
kubectl describe pod <pod-name> -n django-ray
```

### Database Connection Issues

```bash
# Check PostgreSQL
kubectl logs -n django-ray deployment/postgres

# Test connection from web pod
kubectl exec -n django-ray deployment/django-web -- \
  python -c "import psycopg; print('OK')"
```

### Ray Connection Issues

```bash
# Check Ray head
kubectl logs -n django-ray deployment/ray-head

# Test Ray connection from worker
kubectl exec -n django-ray deployment/django-ray-worker -- \
  python -c "import ray; ray.init('ray://ray-head-svc:10001'); print(ray.cluster_resources())"
```

## Production Recommendations

1. **Use managed PostgreSQL** (RDS, Cloud SQL, Azure Database)
2. **Enable TLS** for Ray cluster communication
3. **Use KubeRay operator** for production Ray clusters
4. **Configure proper resource limits** based on workload
5. **Set up monitoring** with Prometheus/Grafana
6. **Use proper secret management** (Vault, External Secrets)
7. **Configure Ingress** with TLS termination

## See Also

- [Docker Deployment](docker.md) - Running with Docker
- [TLS Configuration](tls.md) - Securing Ray communication
- [Configuration](../configuration.md) - All settings

