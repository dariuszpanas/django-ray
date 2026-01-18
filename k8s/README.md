# Django-Ray Kubernetes Deployment

This directory contains Kubernetes manifests for deploying django-ray with a Ray cluster using Kustomize.

## Directory Structure

```
k8s/
├── base/                    # Base Kustomize configuration
│   ├── kustomization.yaml   # Main kustomization file
│   ├── namespace.yaml       # Namespace definition
│   ├── configmap.yaml       # Application config
│   ├── secret.yaml          # Secrets (override in production!)
│   ├── postgres.yaml        # PostgreSQL deployment
│   ├── ray-cluster.yaml     # Ray head + workers
│   ├── django-web.yaml      # Django web application
│   └── django-ray-worker.yaml  # Task worker
└── overlays/
    └── dev/                 # Development overlay for k3d
        └── kustomization.yaml
```

## Components

| Component | Description | Ports |
|-----------|-------------|-------|
| PostgreSQL | Database for Django and task metadata | 5432 |
| Ray Head | Ray cluster coordinator | 6379, 8265, 10001 |
| Ray Workers | Ray execution nodes | - |
| Django Web | Web application and API | 8000 |
| Django-Ray Worker | Task processor | - |

## Quick Start (k3d)

### Prerequisites

- Docker
- k3d (`curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash`)
- kubectl

### Create Cluster and Deploy

```bash
# From project root
make k3d-create
make k3d-deploy
```

Or manually:

```bash
# Create k3d cluster
k3d cluster create django-ray-dev \
  --agents 2 \
  -p "8080:80@loadbalancer" \
  -p "8265:8265@loadbalancer"

# Build and import image
docker build -t django-ray:dev .
k3d image import django-ray:dev -c django-ray-dev

# Deploy
kubectl apply -k k8s/overlays/dev

# Wait for pods
kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s
kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s
kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s
```

### Access the Application

- **Django Web/API**: http://localhost:8080
- **Swagger UI**: http://localhost:8080/api/docs
- **Django Admin**: http://localhost:8080/admin/
- **Ray Dashboard**: http://localhost:8265

### View Logs

```bash
# All django-ray components
kubectl logs -n django-ray -l app=django-ray -f

# Specific components
kubectl logs -n django-ray -l component=web -f
kubectl logs -n django-ray -l component=worker -f
kubectl logs -n django-ray -l app=ray -f
```

### Cleanup

```bash
make k3d-delete
# or
k3d cluster delete django-ray-dev
```

## Production Considerations

⚠️ **The base configuration is for development only!**

For production deployment:

1. **Secrets**: Use external secret management (Vault, AWS Secrets Manager, etc.)
2. **Database**: Use managed PostgreSQL (RDS, Cloud SQL, Azure Database)
3. **Ray Cluster**: Consider using [KubeRay operator](https://ray-project.github.io/kuberay/)
4. **Ingress**: Configure proper TLS and domain
5. **Resources**: Adjust CPU/memory limits based on workload
6. **Replicas**: Scale Django web and Ray workers as needed
7. **Storage**: Use proper storage class for PVCs

### Using KubeRay Operator (Recommended for Production)

```bash
# Install KubeRay operator
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator

# Create RayCluster CR instead of the basic ray-cluster.yaml
# See: https://docs.ray.io/en/latest/cluster/kubernetes/index.html
```

## Environment Variables

### Django Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DJANGO_SECRET_KEY` | insecure-key | Django secret key |
| `DJANGO_DEBUG` | True | Debug mode |
| `DJANGO_ALLOWED_HOSTS` | * | Allowed hosts (comma-separated) |

### Database Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_ENGINE` | sqlite3 | Database engine |
| `DATABASE_NAME` | django_ray | Database name |
| `DATABASE_USER` | django_ray | Database user |
| `DATABASE_PASSWORD` | - | Database password |
| `DATABASE_HOST` | localhost | Database host |
| `DATABASE_PORT` | 5432 | Database port |

### Ray Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RAY_ADDRESS` | auto | Ray cluster address |
| `RAY_NUM_CPUS_PER_TASK` | 1 | CPUs per task |
| `RAY_MAX_RETRIES` | 3 | Max task retries |
| `RAY_RETRY_DELAY_SECONDS` | 5 | Delay between retries |

