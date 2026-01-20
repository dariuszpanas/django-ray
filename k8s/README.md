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
│   ├── ray-tls-secret.yaml  # TLS certificate secret template
│   ├── django-web.yaml      # Django web application
│   └── django-ray-worker.yaml  # Task worker
└── overlays/
    ├── dev/                 # Development overlay (no TLS)
    │   └── kustomization.yaml
    ├── dev-tls/             # Development overlay with TLS
    │   ├── kustomization.yaml
    │   └── ray-tls-secret.yaml
    └── local/               # Local development overlay
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

## Prerequisites

- Kubernetes cluster (Docker Desktop, k3d, kind, minikube, or any cloud provider)
- kubectl configured to access your cluster
- Docker (for building images)

## Quick Start

### 1. Build Docker Images

```bash
# Build Django application image
docker build -t django-ray:latest .

# Build Ray worker image (includes django-ray for task execution)
docker build -f Dockerfile.ray -t django-ray-worker:latest .
```

> **Note**: If using k3d, kind, or minikube, you'll need to import images into the cluster.
> For Docker Desktop Kubernetes, locally built images are automatically available.

### 2. Deploy to Kubernetes

```bash
# Deploy using Kustomize (dev overlay)
kubectl apply -k k8s/overlays/dev

# Wait for deployments
kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s
kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s
kubectl wait --for=condition=available deployment/ray-worker -n django-ray --timeout=180s
kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s
kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s
```

Or use the Makefile:

```bash
make k8s-build    # Build images
make k8s-deploy   # Deploy to cluster
```

### 3. Access the Application

With NodePort (default configuration):

| Service | URL | Description |
|---------|-----|-------------|
| Django Web/API | http://localhost:30080 | Application and REST API |
| Swagger UI | http://localhost:30080/api/docs | API documentation |
| Django Admin | http://localhost:30080/admin/ | Admin interface |
| Ray Dashboard | http://localhost:30265 | Ray cluster monitoring |

### 4. View Logs

```bash
# All django-ray components
kubectl logs -n django-ray -l app=django-ray -f

# Specific components
kubectl logs -n django-ray -l component=web -f
kubectl logs -n django-ray -l component=worker -f
kubectl logs -n django-ray -l app=ray -f
```

### 5. Check Status

```bash
kubectl get pods -n django-ray
kubectl get svc -n django-ray
kubectl get deployments -n django-ray
```

### 6. Cleanup

```bash
kubectl delete -k k8s/overlays/dev
# or to delete everything including namespace:
kubectl delete namespace django-ray
```

## Production Considerations

⚠️ **The base configuration is for development only!**

For production deployment:

1. **Secrets**: Use external secret management (Vault, AWS Secrets Manager, etc.)
2. **Database**: Use managed PostgreSQL (RDS, Cloud SQL, Azure Database)
3. **Ray Cluster**: Consider using [KubeRay operator](https://ray-project.github.io/kuberay/)
4. **TLS**: Enable TLS for Ray cluster communication (see below)
5. **Ingress**: Configure proper TLS and domain
6. **Resources**: Adjust CPU/memory limits based on workload
7. **Replicas**: Scale Django web and Ray workers as needed
8. **Storage**: Use proper storage class for PVCs

### Using KubeRay Operator (Recommended for Production)

```bash
# Install KubeRay operator
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator

# Create RayCluster CR instead of the basic ray-cluster.yaml
# See: https://docs.ray.io/en/latest/cluster/kubernetes/index.html
```

## TLS Configuration

Ray supports TLS for encrypted communication between Ray nodes. This is **required** for production deployments.

### Quick Start with TLS

```bash
# 1. Generate self-signed certificates (development only)
./scripts/generate-ray-tls-certs.sh

# 2. Create the Kubernetes secret
kubectl create namespace django-ray --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic ray-tls-certs \
  --namespace=django-ray \
  --from-file=ca.crt=./certs/ray-tls/ca.crt \
  --from-file=tls.crt=./certs/ray-tls/tls.crt \
  --from-file=tls.key=./certs/ray-tls/tls.key

# 3. Deploy with TLS overlay
kubectl apply -k k8s/overlays/dev-tls
```

Or use the Makefile:

```bash
make k8s-gen-tls-certs     # Generate certificates
make k8s-deploy-tls        # Deploy with TLS enabled
```

### TLS Environment Variables

When TLS is enabled, these environment variables are set on all Ray components:

| Variable | Value | Description |
|----------|-------|-------------|
| `RAY_USE_TLS` | `1` | Enable TLS |
| `RAY_TLS_SERVER_CERT` | `/etc/ray/tls/tls.crt` | Server certificate path |
| `RAY_TLS_SERVER_KEY` | `/etc/ray/tls/tls.key` | Private key path |
| `RAY_TLS_CA_CERT` | `/etc/ray/tls/ca.crt` | CA certificate path |

### Certificate Requirements

The TLS certificates must include these SANs (Subject Alternative Names):

- `ray-head`
- `ray-head.django-ray`
- `ray-head.django-ray.svc`
- `ray-head.django-ray.svc.cluster.local`
- `localhost`
- `127.0.0.1`

The `scripts/generate-ray-tls-certs.sh` script automatically includes these.

### How TLS Works in Kubernetes

TLS certificates are **mounted as Kubernetes secrets**, not embedded in Docker images. This approach:

1. **Enables certificate rotation** without rebuilding images
2. **Keeps secrets secure** - certificates are stored in Kubernetes secrets management
3. **Supports different certs per environment** - dev, staging, production can use different CAs

The `dev-tls` overlay adds:
- Volume mounts for the `ray-tls-certs` secret at `/etc/ray/tls/`
- Environment variables (`RAY_USE_TLS=1`, `RAY_TLS_*`) pointing to the mounted certificates
- TLS configuration to Ray head, Ray workers, and Django-Ray workers

### Production TLS with cert-manager

For production, use [cert-manager](https://cert-manager.io/) to manage certificates:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ray-tls
  namespace: django-ray
spec:
  secretName: ray-tls-certs
  duration: 8760h  # 1 year
  renewBefore: 720h  # 30 days
  subject:
    organizations:
      - django-ray
  isCA: false
  privateKey:
    algorithm: RSA
    size: 4096
  usages:
    - server auth
    - client auth
  dnsNames:
    - ray-head
    - ray-head.django-ray
    - ray-head.django-ray.svc
    - ray-head.django-ray.svc.cluster.local
    - localhost
  ipAddresses:
    - 127.0.0.1
  issuerRef:
    name: your-cluster-issuer
    kind: ClusterIssuer
```

For more details, see the [Ray TLS documentation](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/tls.html).

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

## Local Kubernetes Options

For local development, you can use any of these options:

| Platform | Windows | macOS | Linux | Notes |
|----------|---------|-------|-------|-------|
| Docker Desktop K8s | ✅ | ✅ | ✅ | Enable in Docker Desktop settings |
| k3d | ⚠️ | ✅ | ✅ | Lightweight, requires image import |
| kind | ⚠️ | ✅ | ✅ | Kubernetes-in-Docker, requires image import |
| minikube | ✅ | ✅ | ✅ | Requires `eval $(minikube docker-env)` |

> **Docker Desktop Kubernetes** is recommended for Windows as it requires no additional setup - locally built images are automatically available to the cluster.
