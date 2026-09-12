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
│   ├── secret.yaml          # Credential preparation pointer; no Secret resource
│   ├── postgres.yaml        # PostgreSQL deployment
│   ├── payload-storage.yaml # Evaluation-only shared rq2/input storage PVC
│   ├── ray-cluster.yaml     # Ray head + workers
│   ├── ray-tls-secret.yaml  # TLS certificate secret template
│   ├── django-web.yaml      # Django web application
│   └── django-ray-worker.yaml  # Task worker
├── operators/
│   └── kuberay-co-resident/ # Pinned operator values and shared quota
└── overlays/
    ├── co-resident/         # Five-pod profile for a shared local cluster
    ├── dev/                 # Development overlay (no TLS)
    │   └── kustomization.yaml
    ├── dev-tls/             # Development overlay with TLS
    │   ├── kustomization.yaml
    │   └── ray-tls-secret.yaml
    ├── kuberay-kind/        # KubeRay operator overlay for local kind clusters
    ├── kong-local/          # Larger private KubeRay capacity profile
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
| Payload Storage | Shared content-addressed task-input/rq2 request PVC | - |

## Prerequisites

- Kubernetes cluster (Docker Desktop, k3d, kind, minikube, or any cloud provider)
- kubectl configured to access your cluster
- Docker (for building images)
- GNU Make, if you use the `make ...` command shortcuts
- Helm, for KubeRay operator installation targets
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

Prepare [component-scoped local credentials](../docs/deployment/local-credentials.md) before
deployment. Bootstrap is disabled unless explicitly requested; no tracked Secret is applied.

```bash
python scripts/prepare_k8s_secrets.py --namespace django-ray
make k8s-bootstrap-django-ray-secret K8S_CONTEXT=docker-desktop

# Deploy using Kustomize (dev overlay)
kubectl --context docker-desktop apply -k k8s/overlays/dev

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
make k8s-deploy K8S_CONTEXT=docker-desktop   # Deploy to the explicit local cluster
```

The evaluation base configures filesystem `INPUT_STORAGE_BACKEND` at
`/payload-storage/inputs`. `payload-storage-pvc` is a separate RWX volume: Django web,
the base/default task manager, and the dedicated Ray Job task manager mount it
read/write to prepare and retain payloads, while static and KubeRay Ray head/worker
containers mount it read-only so rq2 drivers can load their exact request before
Django setup. The sample leaves inline-input spillover disabled; a deployment that
enables it must additionally give every local/synchronous executor read access. Do
not reuse `runtime-env-pvc`; request payloads have independent write, retention, and
cleanup ownership. A production design may use a scoped S3/GCS namespace instead,
with manager/cleanup writers and driver readers using component-specific ambient
credentials.

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
synchronous, ML, and Ray Job task-manager containers; they are not added to the
shared ConfigMap or `RayCluster` pod specification. The base and other overlays
remain in the plaintext compatibility mode. This verifies the envelope and
execution path, not production key isolation: the example Ray pods still import
the shared Django signing secret used by the fallback. Use a dedicated key
delivered only to Django application processes when separation from read-only
database access is required.

### Local capacity profiles

The `co-resident` overlay is the smallest stable-namespace profile. It is
designed for keeping django-ray available while another application, such as
django-ray-testing, uses the same developer-controlled cluster. It retains
PostgreSQL, Django web, one default task manager at concurrency one, one Ray
head, and one Ray worker. Monitoring, sync, ML, and Ray Job task managers are
absent. Every Service is `ClusterIP`; the render contains no Ingress,
`NodePort`, `hostPort`, or host networking.

The application namespace has one unscoped seven-coordinate quota capped at
1.6 CPU. Its five steady pods use at most 1.45 CPU, and the setup Job raises the
maximum concurrent limit to 1.55 CPU. The separately rendered
`kuberay-system` policy pins the operator chart and container to v1.6.2 and
allows a 100m operator plus one 100m rollout surge. The two hard quota ceilings
therefore total exactly 1.8 CPU. The application memory-limit ceiling is 9 GiB,
including a 6 GiB Ray-head limit, and the operator namespace is capped at 1 GiB.
LimitRanges supply bounded CPU, memory, and ephemeral-storage coordinates to
the inherited init containers.

This profile is local smoke capacity, not the guarded full KubeRay gate and not
the isolated multi-session experiment tracked by issue #418. Its switch target
requires an explicit `docker-desktop` or `kind-<name>` context and
foreground-removes only the named sample workloads and routes that are absent
from the profile. It retains the `django-ray` Namespace, the existing Secret
values, and all three PVCs. A first install requires separately prepared random
component Secrets; subsequent transitions preserve the complete set. The application
ConfigMap remains profile-managed and is converged intentionally. A transition
back to the direct full or Kong local profile first removes the co-resident
application quota and LimitRange, which an ordinary Kustomize apply would
otherwise retain. Those KubeRay profiles use the same separately provisioned Secret
boundary and never render checked-in credentials over a live object.

The direct `kuberay-kind` overlay is the laptop-oriented exploratory baseline. It
runs one default task manager for `default,high-priority,low-priority`, one
synchronous task manager, one ML task manager, one Ray Job task manager for
`ray-data`, and two fixed Ray workers. Each Ray worker still advertises two CPUs,
so the workers alone can schedule the testproject's default 12-leaf complex
workflow, whose leaves request three CPUs in total. The Ray head, web,
PostgreSQL, Prometheus, and Grafana retain their existing profiles.

`kong-local` is the explicit heavier backlog/capacity profile. It restores two
default task managers and four fixed Ray workers, then applies its larger web,
PostgreSQL, and Ray resource settings. It is not the low-resource choice for
ordinary Admin, dashboard, or one-user Locust exploration.

The following rendered steady-state totals exclude the completed setup Job,
the KubeRay operator, the Kong controller/gateway, and Docker
Desktop/Kubernetes overhead:

| Profile | Pods in `django-ray` | CPU requests | Memory requests | CPU limits | Memory limits |
|---|---:|---:|---:|---:|---:|
| Co-resident | 5 | 0.5 | 2,176 MiB | 1.45 | 8,832 MiB |
| Direct `kuberay-kind` | 11 | 3.3 | 5,056 MiB | 9.8 | 16,256 MiB |
| Heavier `kong-local` | 17 | 10.2 | 17,088 MiB | 27.3 | 38,272 MiB |

```bash
# Preserve the existing Secret and PVCs while converging to five pods.
make k8s-deploy-co-resident K8S_CONTEXT=docker-desktop

# Optional, caller-owned temporary browser access; no listener is retained.
kubectl port-forward -n django-ray service/django-web-svc 8000:80

# Build app images, load them into kind, install operator, deploy KubeRay overlay.
# Ray head/workers use the upstream image; RuntimeEnv supplies project code.
make k8s-deploy-kuberay-kind K8S_CONTEXT=docker-desktop

# Check status (includes RayCluster list)
make k8s-status

# Cleanup KubeRay overlay resources
make k8s-delete-kuberay-kind
```

If your local kind cluster has a non-default name:

```bash
make k8s-deploy-kuberay-kind K8S_CONTEXT=kind-my-kind KIND_CLUSTER_NAME=my-kind
```

## Larger private capacity profile

The historical `kong-local` name is retained for the larger web, database, and Ray resource
profile. It no longer installs Kong or creates Ingress routes. All Services remain `ClusterIP`.
Use the same explicit credential preparation and temporary loopback access as the direct profile.
It retains two default task managers, dedicated sync/ML/Ray Job managers, four three-CPU Ray
workers, the larger PostgreSQL budget, and the overload-oriented web probe configuration.

```bash
make k8s-deploy-kong-local K8S_CONTEXT=docker-desktop
```

Before switching an older installation, remove only its package-owned legacy ingress routes after
reviewing ownership; Kustomize omission does not prune them. Existing controller releases may be
shared with other workloads and are not automatically uninstalled.

### 3. Access the Application

Start only the needed bounded loopback forwards, each in a separate terminal:

```bash
make k8s-forward-web K8S_CONTEXT=docker-desktop
make k8s-forward-ray K8S_CONTEXT=docker-desktop
make k8s-forward-grafana K8S_CONTEXT=docker-desktop
make k8s-forward-prometheus K8S_CONTEXT=docker-desktop
```

| Service | Loopback URL | Authentication |
|---|---|---|
| Django Web/API | http://127.0.0.1:30080 | Operator API token or Django session |
| Ray Dashboard | http://127.0.0.1:30265 | Ray token |
| Grafana | http://127.0.0.1:30030 | Generated administrator account |
| Prometheus | http://127.0.0.1:30090 | Loopback administrative access |

Forwards bind only `127.0.0.1` and expire after 900 seconds by default; choose 1–3600 seconds with
`K8S_FORWARD_SECONDS`. No helper forwards GCS or Ray Client. See
[local credentials](../docs/deployment/local-credentials.md) for scoped delivery, rotation, bootstrap,
and migration. `make k8s-urls` prints the corresponding URLs without opening listeners.
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
6. **Externally managed, component-scoped secrets** with managed rotation. The sample now separates
   operator, metrics, bootstrap, Grafana, database, and Ray tokens, but execution nodes still share
   application database and Django signing authority. This remains an evaluation trust boundary.
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
| `DJANGO_SECRET_KEY` | generated separately | Random value of at least 50 characters in production |
| `DJANGO_API_TOKEN` | generated separately; web only | Operator bearer token; at least 32 characters in production |
| `DJANGO_METRICS_TOKEN` | generated separately; web and Prometheus only | Authorizes only the metrics scrape |
| `DJANGO_API_ENABLED` | false on non-web processes | Disables API authorization outside the web process |
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
identity paths, or runtime credential boundary production-ready. The `dev`, `local`, `dev-tls`, `kuberay-kind`,
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
