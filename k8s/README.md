# Django-Ray Kubernetes Deployment

This directory contains Kustomize manifests for local django-ray evaluation and maintainer
integration validation.

> **Evaluation only:** `k8s/base` and every tracked overlay are not production-ready. All
> `kubectl apply` and `make k8s-deploy...` commands below must target a trusted, disposable local
> environment. Replacing placeholders does not turn the bundled topology into a production
> deployment.

## Directory Structure

```
k8s/
├── base/                    # Base Kustomize configuration
│   ├── kustomization.yaml   # Main kustomization file
│   ├── namespace.yaml       # Namespace definition
│   ├── configmap.yaml       # Application config
│   ├── secret.yaml          # Shared local/render-only Secret reference
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
    ├── kuberay-kind/        # KubeRay operator overlay for local kind clusters
    ├── kong-local/          # KubeRay + Kong local ingress overlay
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
- GNU Make, if you use the `make ...` command shortcuts
- Helm, for KubeRay or Kong operator installation targets
- kind, for `make k8s-deploy-kuberay-kind` image-loading targets

## Local Evaluation Quick Start

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

## KubeRay Operator Path (Recommended for kind multi-node clusters)

Use this path to manage Ray via `RayCluster` custom resources instead of static
`Deployment/ray-head` and `Deployment/ray-worker` manifests.

This path requires `helm` and `kind` on your PATH. The default Docker Desktop
Kubernetes path above does not require either tool.

The ordinary targets below use `latest` for local iteration. Before merging a change that crosses
the deployment boundary, follow the source-bound trigger matrix and guarded commands in
[`docs/deployment/local-kuberay-gate.md`](../docs/deployment/local-kuberay-gate.md). That gate keeps
mutations in `django-ray`, preserves PostgreSQL/PVCs, and verifies the running image IDs, protected
task smoke, probes, generic-Ray RuntimeEnv boundary, and Prometheus pools together. It is maintainer
integration validation, not deployment certification or a production-readiness assessment.

This local overlay also opts Django application processes into encrypted durable
RuntimeEnv snapshots through the explicit `django-secret` fallback. The three
selection variables are patched directly onto `django-web` and the default,
synchronous, and ML task-manager containers; they are not added to the shared
ConfigMap or `RayCluster` pod specification. The base and other overlays remain in
the plaintext compatibility mode. This verifies the envelope and execution path, not
production key isolation: the example Ray pods still import the shared Django signing
secret used by the fallback. Use a dedicated key delivered only to Django application
processes when separation from read-only database access is required.

### Local capacity profiles

The direct `kuberay-kind` overlay is the laptop-oriented exploratory baseline. It
runs one default task manager for `default,high-priority,low-priority`, one
synchronous task manager, one ML task manager, and two fixed Ray workers. Each
Ray worker still advertises two CPUs, so the workers alone can schedule the
testproject's default 12-leaf complex workflow, whose leaves request three CPUs
in total. The Ray head, web, PostgreSQL, Prometheus, and Grafana retain their
existing profiles.

`kong-local` is the explicit heavier backlog/capacity profile. It restores two
default task managers and four fixed Ray workers, then applies its larger web,
PostgreSQL, and Ray resource settings. It is not the low-resource choice for
ordinary Admin, dashboard, or one-user Locust exploration.

The following rendered steady-state totals exclude the completed setup Job,
the KubeRay operator, the Kong controller/gateway, and Docker
Desktop/Kubernetes overhead:

| Profile | Pods in `django-ray` | CPU requests | Memory requests | CPU limits | Memory limits |
|---|---:|---:|---:|---:|---:|
| Direct `kuberay-kind` | 10 | 3.2 | 4,800 MiB | 9.3 | 11,648 MiB |
| Heavier `kong-local` | 16 | 10.1 | 16,832 MiB | 26.8 | 37,760 MiB |

```bash
# Build app images, load them into kind, install operator, deploy KubeRay overlay.
# Ray head/workers use the upstream image; RuntimeEnv supplies project code.
make k8s-deploy-kuberay-kind

# Check status (includes RayCluster list)
make k8s-status

# Cleanup KubeRay overlay resources
make k8s-delete-kuberay-kind
```

If your local kind cluster has a non-default name:

```bash
make k8s-deploy-kuberay-kind KIND_CLUSTER_NAME=my-kind
```

## Kong Ingress Controller Path

To evaluate Kong as a candidate ingress, validate the ingress class locally with KubeRay plus Kong
Ingress Controller.

This path requires `helm`; the one-command path shares the KubeRay build,
image-load, and operator prerequisites but applies only the `kong-local`
workload render. It expects `kind` unless your environment provides an
equivalent image-loading path.

```bash
# One command path
make k8s-deploy-kong-local

# Equivalent manual path
# Install Kong Gateway + Kong Ingress Controller
helm upgrade --install kong kong/ingress \
  --namespace kong \
  --create-namespace \
  -f k8s/overlays/kong-local/kong-values.yaml

# Deploy django-ray with Kong-specific ingress/service patches
kubectl apply -k k8s/overlays/kong-local
```

This overlay:

- switches `django-web-svc` from `NodePort` to `ClusterIP`
- switches `grafana-svc`, `prometheus-svc`, and `ray-dashboard-svc` from `NodePort` to `ClusterIP`
- sets `spec.ingressClassName: kong`
- removes the old Traefik-specific ingress annotation
- keeps the main Django app on the default root route
- adds host-based Kong routes for Grafana, Prometheus, and the Ray dashboard
- keeps two cluster-mode `django-ray-worker` replicas for `default,high-priority,low-priority`
- adds a dedicated `django-ray-worker-sync` deployment for the `sync` queue
- adds a dedicated `django-ray-worker-ml` deployment for the `ml` queue
- keeps the main cluster-mode worker submission cap conservative for local stability:
  - `DJANGO_RAY_CONCURRENCY=16` per worker pod in the Kong local overlay
  - this is still below the earlier stress setting, but high enough to push the local stack harder now
    that the web and database paths have been stabilized
- overprovisions the local PostgreSQL pod for backlog testing:
  - requests: `500m` CPU / `2Gi` memory
  - limits: `2` CPU / `4Gi` memory
  - tuned settings: `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=16MB`,
    `maintenance_work_mem=256MB`, `wal_buffers=16MB`, `max_wal_size=2GB`
- increases the local web and Ray capacity profile toward the older stress-test setup:
  - `django-web` runs `4` replicas and uses `8` Gunicorn workers
  - `4` fixed Ray worker pods advertise `3` CPUs each instead of `2`
  - Ray head gets a larger memory budget for scheduling and dashboard stability
- uses a split local web probe model aimed at overloaded containers:
  - `startupProbe`: `GET /api/livez` to confirm Django/Gunicorn actually comes up
  - `livenessProbe`: `exec kill -0 1` so kubelet does not restart a busy-but-alive Gunicorn master
  - `readinessProbe`: `tcpSocket` on port `8000` so overloaded pods stay in service as long as Gunicorn is listening
- adds container-focused Gunicorn hardening for the local web path:
  - `/dev/shm` worker tmp dir
  - request recycling via `max-requests` plus jitter
  - longer timeouts for slow in-flight requests
  - access logging disabled in the Kong local overlay to reduce stdout pressure under heavy load
  - Gunicorn 25 control socket disabled by default for this image path because the runtime user cannot
    create the default `gunicorn.ctl` socket in the read-only `/app` working directory
- reduces DB pressure from observability endpoints:
  - `/api/metrics`, `/api/executions`, and `/api/executions/stats` now aggregate task counts with grouped
    queries instead of issuing one `COUNT(*)` query per state and per queue
- spreads web pods across nodes with topology spreading and preferred anti-affinity to better exercise
  local load-balancing behavior
- sets `RAY_DASHBOARD_URL` to `http://ray.localhost:30080` so Django admin deep links match the Kong route
- patches Ray's Grafana iframe host to `http://grafana.localhost:30080`
- keeps Ray's Prometheus host on the in-cluster service URL (`http://prometheus-svc:9090`), which is what the Ray dashboard backend queries

If you apply the Kong overlay onto an already-running `RayCluster`, recycle the Ray head pod once and
restart the Django workers so the dashboard and cluster-mode workers reconnect cleanly:

```bash
kubectl delete pod -l app=ray,component=head -n django-ray
kubectl wait --for=condition=Ready pod -l app=ray,component=head -n django-ray --timeout=240s
kubectl rollout restart deployment/django-ray-worker -n django-ray
kubectl rollout restart deployment/django-ray-worker-sync -n django-ray
kubectl rollout restart deployment/django-ray-worker-ml -n django-ray
```

Notes:

- On Docker Desktop's managed kind cluster, `cloud-provider-kind` can publish the Kong proxy
  `LoadBalancer` on host ports `30080/30443`, so the local entrypoint becomes `http://localhost:30080`.
- On a plain kind cluster, host-reachable ingress still requires extra networking setup such as
  `extraPortMappings` or `cloud-provider-kind`.
- Mixed load profiles only reflect real queue throughput if the matching workers are deployed. The
  Kong local overlay covers `default`, `high-priority`, `low-priority`, `sync`, and `ml`, but an
  independently designed deployment still needs queue-specific worker planning.
- `sync` tasks are not supposed to run through Ray. They need a worker started with `--sync --queue=sync`,
  which is why the Kong local overlay deploys a separate `django-ray-worker-sync`.
- Because Docker Desktop managed-kind reports duplicated per-node capacity, the practical local ceiling comes
  more from the Ray/Kubernetes limits in this overlay than from summed node allocatable values.

### 3. Access the Application

Print the URLs for the default NodePort-oriented manifests:

```bash
make k8s-urls
```

With the default NodePort-oriented manifests, these are the intended service ports:

| Service | URL | Description |
|---------|-----|-------------|
| Django Web/API | http://localhost:30080 | Application and REST API |
| Swagger UI | http://localhost:30080/api/docs | API documentation |
| Django Admin | http://localhost:30080/admin/ | Admin interface |
| Ray Dashboard | http://localhost:30265 | Ray cluster monitoring |

With the Kong local overlay on Docker Desktop's managed kind cluster, use these URLs instead:

```bash
make k8s-urls-kong
```

| Service | URL | Description |
|---------|-----|-------------|
| Django Web/API | http://localhost:30080 | Application and REST API through Kong |
| Swagger UI | http://localhost:30080/api/docs | API documentation |
| Django Admin | http://localhost:30080/admin/ | Admin interface |
| Grafana | http://grafana.localhost:30080 | Grafana through Kong |
| Prometheus | http://prometheus.localhost:30080 | Prometheus through Kong |
| Ray Dashboard | http://ray.localhost:30080 | Ray dashboard through Kong |

Notes:

- On Docker Desktop's managed kind cluster, the direct `NodePort` services from the base/KubeRay
  manifests are not published to the host in this setup. The Kong local overlay is the intended
  host-access path.
- For non-local clusters, override the printed host, scheme, ports, or full URLs. `K8S_URL_HOST`
  changes every default NodePort host. Full URL variables such as `K8S_WEB_URL`,
  `K8S_GRAFANA_URL`, and `K8S_PROMETHEUS_URL` are per-service overrides.
- `*.localhost` hostnames work in modern browsers and also resolved correctly in this environment.
- Kong Manager is not host-exposed in the stable local overlay. The local browser-access path is the
  Kong proxy on `30080`, not a separate Kong Manager UI.

### 4. View Logs

```bash
# Django web and task-manager processes
kubectl logs -n django-ray -l app=django-ray,component=web -c django-web --prefix -f
kubectl logs -n django-ray -l app=django-ray,component=worker -c django-ray-worker --prefix -f --max-log-requests=8

# Ray execution processes
kubectl logs -n django-ray -l app=ray,component=head -c ray-head --prefix -f
kubectl logs -n django-ray -l app=ray,component=worker -c ray-worker --prefix -f --max-log-requests=8
```

The Django task managers claim durable task rows and submit cluster-mode work;
the Ray head and Ray workers execute and coordinate that submitted work. Do not
use the unqualified `component=worker` selector because it matches both worker
families.

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

## Production Architecture Checklist

⚠️ **The base configuration is for development only!**

The repository ships no production overlay or certified reference topology. Do not copy the base
and treat placeholder replacement as a security or availability review. An independently designed
production topology must address at least:

1. **KubeRay lifecycle management** instead of the base's static Ray Deployments, including operator,
   cluster/service upgrade, failure recovery, and rollback ownership.
2. **Immutable images and RuntimeEnv artifacts** identified by digest or content identity, never the
   sample's mutable `latest` tags.
3. **Service identity and application authorization** instead of the sample superuser and shared
   operator token, with Ray control surfaces kept private.
4. **TLS, ingress, and network policy**, including certificate rotation and workload-to-workload
   authorization.
5. **Managed PostgreSQL and durable object/storage services**, with backup and restore tests.
6. **Externally managed, component-scoped secrets** instead of `django-ray-secret`, which combines
   signing, API, database/bootstrap, and sample user credentials. Both the static Ray containers and
   generic upstream KubeRay head and worker pods import every value through `envFrom`; Prometheus
   mounts the operator token separately. This evaluation-only credential blast radius is a sample
   hazard, not a production endorsement.
7. **Workload-derived resource policy**, quotas, autoscaling, placement, disruption budgets, and
   tenant isolation.
8. **Backups, observability, alerting, audit access, and operational ownership** with explicit
   retention and recovery targets.
9. **Separately invokable migration and rollback operations** for database, application, Ray, and
   configuration changes.

A real reference implementation requires its own threat model, least-privilege design, upgrade and
rollback contract, and clean-checkout evidence.

### KubeRay Architecture Starting Point

The commands below install the upstream operator for exploration; they do not produce a complete
django-ray production deployment.

```bash
# Install KubeRay operator
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator

# Create RayCluster CR instead of the basic ray-cluster.yaml
# See: https://docs.ray.io/en/latest/cluster/kubernetes/index.html
```

## TLS Configuration

Ray supports TLS for encrypted communication between Ray nodes. TLS is necessary in a production
design, but the self-signed `dev-tls` example below is local-only and does not supply production
identity, authorization, ingress, or certificate lifecycle.

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

### Certificate Lifecycle Architecture

A production design can use [cert-manager](https://cert-manager.io/) or an equivalent approved PKI
integration. The resource below is a schema sketch; select an organization-approved issuer, trust
roots, SANs, rotation policy, and private-key controls instead of applying it verbatim.

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
| `DJANGO_DEPLOYMENT_MODE` | production in base, demo in local overlays | Exercises fail-closed Django settings checks; it does not certify the Kubernetes topology |
| `DJANGO_SECRET_KEY` | placeholder in base Secret | Random value of at least 50 characters in production |
| `DJANGO_API_TOKEN` | placeholder in base Secret | Bearer token for non-health API routes; at least 32 characters in production |
| `DJANGO_DEBUG` | False | Debug mode; production rejects True |
| `DJANGO_ALLOWED_HOSTS` | `django-ray.example.com` | Explicit comma-separated hosts; production mode rejects `*`. Keep web probe `Host` headers aligned in an independently designed deployment. |
| `DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE` | `plaintext` | Format for new durable RuntimeEnv snapshots; the local KubeRay overlay selects `encrypted` only on Django application containers |
| `DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY` | unset | Key ID for new encrypted snapshots; the local KubeRay overlay selects the reserved `django-secret` fallback |
| `DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK` | `False` | Explicitly permit HKDF derivation from Django signing keys; the local KubeRay overlay sets `true` |

### Database Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_ENGINE` | sqlite3 | Database engine |
| `DATABASE_NAME` | django_ray | Database name |
| `DATABASE_USER` | django_ray | Database user |
| `DATABASE_PASSWORD` | - | Database password |
| `DATABASE_HOST` | localhost | Database host |
| `DATABASE_PORT` | 5432 | Database port |

### django-ray Worker Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RAY_ADDRESS` | auto | Ray cluster address |
| `RAY_DASHBOARD_URL` | http://localhost:8265 | Ray Dashboard URL for Django admin links |
| `DJANGO_RAY_QUEUE` | default | Queue name used by Docker worker entrypoint modes |
| `DJANGO_RAY_QUEUES` | - | Comma-separated queue list; overrides `DJANGO_RAY_QUEUE` |
| `DJANGO_RAY_CONCURRENCY` | 10 | Worker concurrency used by Docker worker entrypoint modes |
| `RAY_MAX_RETRIES` | 3 | Sample project max task attempts |
| `RAY_RETRY_DELAY_SECONDS` | 5 | Sample project retry backoff seconds |

The base keeps `DJANGO_DEPLOYMENT_MODE=production` only to exercise fail-closed testproject settings;
that value does not make its static Ray Deployments, mutable images, bundled services, sample
identity paths, or shared Secret production-ready. The `dev`, `local`, `dev-tls`, `kuberay-kind`,
and `kong-local` overlays switch to `demo` for trusted local use. Only `/api/livez`, `/api/readyz`,
and `/api/health` are public; send `Authorization: Bearer $DJANGO_API_TOKEN` for all other local
sample API requests, including metrics and workflow/log observability.

## Local Kubernetes Options

For local development, you can use any of these options:

| Platform | Windows | macOS | Linux | Notes |
|----------|---------|-------|-------|-------|
| Docker Desktop K8s | ✅ | ✅ | ✅ | Enable in Docker Desktop settings |
| k3d | ⚠️ | ✅ | ✅ | Lightweight, requires image import |
| kind | ⚠️ | ✅ | ✅ | Kubernetes-in-Docker, requires image import |
| minikube | ✅ | ✅ | ✅ | Requires `eval $(minikube docker-env)` |

> **Docker Desktop Kubernetes** is recommended for Windows as it requires no additional setup - locally built images are automatically available to the cluster.
