# Local KubeRay final gate

The local KubeRay final gate complements, but never replaces, `uv run make ci` and the required
GitHub Actions matrix. It exercises the Docker Desktop or Kind deployment boundary that unit and
disposable CI clusters cannot reproduce: locally built images, Kustomize, the setup Job, the shared
RuntimeEnv archive, generic Ray nodes, application task managers, protected HTTP APIs, kubelet
probes, and live Prometheus discovery.

Run it only against the dedicated local `django-ray` namespace. The command fails before its first
Docker build or Kubernetes mutation unless the checkout is clean, the active context is the named
context, the context is `docker-desktop` or `kind-<name>`, its API server is local, and the rendered
resource set cannot escape `django-ray`. It also rejects a remote Docker context or `DOCKER_HOST`.
Docker receives an archive of the committed Git tree, never the checkout directory, and both
Dockerfiles have deny-by-default context allowlists. The same one-time archive is the only source for
the rendered Kubernetes manifests. Preflight also creates a private, flattened kubeconfig snapshot;
every later Kubernetes command uses that immutable file, and every Docker daemon command uses the
validated explicit endpoint rather than ambient context state. Kubernetes, Docker, and Kind
subprocesses receive sanitized environments: proxy, kubeconfig, Docker context/TLS, BuildKit/Buildx,
and ambient Kind provider routing overrides are removed; Kind is then explicitly pinned to Docker.
Named-pipe Docker endpoints must use the local `//./pipe/` authority.

## Trigger matrix

Use the strongest row that applies to a change. When uncertain about a cross-component boundary,
run the gate and choose the cold Ray restart.

| Change class | Gate | `K8S_RAY_RESTART` | Why |
|---|---|---|---|
| Isolated Compiled Graph pilot files under `k8s/pilots/compiled-graph/`, `scripts/kuberay_compiled_graph_pilot.py`, its focused tests, or its retained investigation evidence for #102 | Not applicable | N/A | The pinned pilot has its own isolated namespace, manifests, resource profile, teardown checks, and evidence contract. It must not mutate or validate the supported local application stack. |
| Testproject dashboard, templates, JavaScript, static collection, web image, entrypoint, or web dependencies | Required | `skip`, unless the RuntimeEnv or Ray boundary also changed | Proves the exact asset and image reached the live web pod and protected actions still work. |
| `Dockerfile.ray`, package or RuntimeEnv contents, source archive construction, dependency delivery, or remote bootstrap/import behavior | Required | `required` | Proves a newly built archive reaches newly created generic Ray interpreters without preinstalling `django_ray`. |
| Ray Client submission, reconnect, cancellation, retry, task context, result persistence, or cross-component task lifecycle | Required | `required` | Exercises a fresh Ray session plus fresh task managers and a durable result. |
| KubeRay `RayCluster`, Ray services, Ray pod volumes/environment, or Ray metrics configuration | Required | `required` | The tested Ray pods must be cold replacements of the prior pods. |
| Kubernetes application resources, setup Job, web/worker Deployments, probes, Secrets/ConfigMaps, RBAC, services, ingress, Prometheus, or Grafana | Required | `required` when Ray resources or the RuntimeEnv mount changed; otherwise `skip` | Proves rendered resources, rollouts, probes, authentication, and scrape ownership together. |
| Worker command, queue selection, application image, polling, or database-backed execution behavior | Required | `required` for cluster-mode submission changes; otherwise `skip` | Proves all task-manager Deployments reconnect and consume a real task. |
| Dependency update plausibly affecting Django, Ray, Gunicorn, Psycopg, Ninja, container packaging, or the sample deployment | Recommended | Match the affected boundary; use `required` for Ray or RuntimeEnv uncertainty | Catches integration drift that focused dependency tests may not expose. |
| Package behavior with no container, Kubernetes, RuntimeEnv, HTTP, worker, or task-lifecycle effect | Recommended before a release; otherwise not applicable | `skip` | The normal CI matrix is the primary gate. |
| Documentation, issue templates, repository policy, comments, or tests that do not change deployed behavior | Not applicable | N/A | Do not turn deployment-independent work into a cluster requirement. |

The gate decision and exact evidence belong in every material retained commit and PR to which a
**Required** row applies. For a **Recommended** row, record either the passing evidence or a specific
reason it was not run. The evidence binds to the immutable Git tree rather than relying on a commit
SHA that changes when the evidence is added to the commit message. Never copy a token, Secret
payload, unbounded log, or browser credential into history.

## Prerequisites

- A clean checkout whose committed tree is the exact source under test. Untracked files also fail
  the preflight.
- `uv`, Docker, and `kubectl`; a named Kind context also needs `kind`.
- A running Docker Desktop Kubernetes cluster or a Kind cluster whose context is `kind-<name>`.
- The KubeRay CRD and operator already installed. The gate deliberately does not install or mutate
  cluster-wide operators.
- The local access path already exposed, either through the direct NodePorts or the documented Kong
  local routes.
- A `django-ray-secret` in `django-ray`. During preflight, the gate reads `DJANGO_API_TOKEN` through a
  sensitive-output-suppressed command path and accepts only 32-512 characters from the Bearer
  `token68` alphabet (`A-Z`, `a-z`, `0-9`, `-`, `.`, `_`, `~`, `+`, and `/`) with at most two
  trailing `=` padding characters. The 32-512-character bound includes any trailing padding.
  Quotes, backslashes, whitespace, controls, non-ASCII characters, embedded padding, and longer
  padding are rejected. The gate immediately registers the token,
  Kubernetes base64 value, and their JSON-, repr-, and URL-escaped forms with the output redactor. It
  treats percent-escape hex case as equivalent, never prints the token, and never places it in a
  subprocess argument. The rendered placeholder Secret is intentionally excluded from apply so an
  existing conforming local token is preserved.
- Static bearer/basic-auth, auth-provider, private-key, and sensitive exec argument/environment
  values embedded in the private kubeconfig snapshot are registered with the same redactor before
  the first snapshot-backed Kubernetes command. The snapshot command itself suppresses captured
  failure output because its credentials are not known until capture succeeds.

Older checkouts created cluster-scoped `ClusterRole/prometheus-django-ray` and
`ClusterRoleBinding/prometheus-django-ray`. Current manifests use a namespace-scoped Role and
RoleBinding because Prometheus discovers pods only in `django-ray`. The gate refuses to proceed while
either obsolete cluster-scoped object exists; it will not silently mutate cluster-wide RBAC. Review
the migration, then remove only those two old objects once before running this gate:

```bash
kubectl --context docker-desktop delete \
  clusterrole/prometheus-django-ray \
  clusterrolebinding/prometheus-django-ray \
  --ignore-not-found
```

This one-time RBAC migration deletes no workload or data. Do not broaden it to other roles or
bindings.

Run `uv run make ci` first. Then run the non-mutating preflight with an explicit context, namespace,
and Ray decision:

```powershell
uv run make k8s-final-gate-preflight `
  K8S_CONTEXT=docker-desktop `
  K8S_NAMESPACE=django-ray `
  K8S_RAY_RESTART=required
```

The gate renders and owns the `kuberay-kind` overlay, so its HTTP acceptance boundary is the
direct NodePort pair: Django at `http://localhost:30080` and Prometheus at
`http://localhost:30090`. Keep those defaults unless the same services are already exposed through
equivalent reviewed local routes. In particular, `http://prometheus.localhost:30080` belongs to the
optional, independently deployed `kong-local` overlay. Without that Prometheus ingress, port `30080`
routes to Django and the Prometheus `/api/v1/targets` request returns HTTP 404.

For a plain Kind cluster, the context encodes the cluster name. The optional override must match it:

```bash
uv run make k8s-final-gate-preflight \
  K8S_CONTEXT=kind-django-ray \
  K8S_NAMESPACE=django-ray \
  K8S_RAY_RESTART=required \
  K8S_FINAL_GATE_EXTRA_ARGS="--kind-cluster-name django-ray"
```

Preflight verifies Git cleanliness, captures `HEAD` once, derives the tree from that captured commit,
and exports one immutable archive used by both Kustomize and Docker. It checks a local Docker daemon,
pins its explicit endpoint, validates local Kubernetes context identity, writes a private flattened
kubeconfig snapshot without a proxy route, verifies that snapshot's SHA-256 before every later
command, strips ambient routing overrides from Kubernetes, Docker, and Kind subprocesses, checks the
KubeRay CRD, enforces an exact GVK/name inventory and namespace confinement, loads the existing token
into the in-memory redactor, and runs a client-side apply. It does not build images, load images, or
change Kubernetes. Unknown resources have no implicit apply phase: adding a new workload requires an
explicit review and gate classification.

## Run the gate

Replace the preflight target with the full target after reviewing the trigger matrix:

```powershell
uv run make k8s-final-gate `
  K8S_CONTEXT=docker-desktop `
  K8S_NAMESPACE=django-ray `
  K8S_RAY_RESTART=required
```

The gate performs these bounded layers:

1. Repeats preflight and derives a unique tag containing the first 12 characters of the immutable
   Git tree, a UTC timestamp, and a random suffix.
2. Builds `django-ray:<source-tag>` and `django-ray-worker:<source-tag>` from the same preflight
   archive used to render Kubernetes. Deny-by-default Dockerfile context rules admit
   only reviewed image inputs; the gate rejects a missing or altered Dockerfile-specific policy
   instead of falling back to the broader root context. Both images carry the commit-at-run and
   stable source-tree OCI labels, and named Kind clusters receive those exact tags through
   `kind load docker-image`.
3. Renders a temporary copy of `k8s/overlays/kuberay-kind`, rejects floating application tags or any
   resource outside `django-ray`, and preserves the existing live API Secret. It applies only setup
   prerequisites first, then rolls Prometheus and waits for PostgreSQL.
4. Recreates `Job/django-setup` with timeout-bounded deletion and completion waits. Setup must prove
   migrations, static collection, RuntimeEnv bundle creation, the exact Job UID ownership of its
   pod, and the rendered `django-setup` container name, tag, and image ID without a substitute
   sidecar. A failure stops the run before any application
   Deployment or `RayCluster` is reconciled.
5. Applies the staged application Deployments and `RayCluster` only after setup passes. With
   `required`, a restart-discovery phase inventories only pods with the exact live RayCluster UID,
   sole controlling `RayCluster/ray` owner, `ray.io/cluster=ray` and `app=ray` labels, recognized
   head/worker role and worker group, unique bounded pod UIDs, and the exact supported container-name
   inventory. That phase intentionally permits the prior pinned Ray image so a newly applied profile
   can safely cold-replace the old generation. It then deletes only those verified pod names with a
   timeout and proves the RayCluster UID remains pinned and all replacement pod UIDs are new. Final
   convergence requires the live head spec, named worker groups, min/replicas/max values, Ray start
   parameters, image references, and pod counts to match the rendered static topology. It also
   validates KubeRay 1.6.2's effective worker pod contract: exactly one injected `wait-gcs-ready`
   init container, the rendered pinned `ray-worker` image, `/bin/bash -c --`, the exact generated GCS
   health-check loop targeting `ray-head-svc.<namespace>.svc.cluster.local:6379`, successful init
   termination, the exact regular/status inventories, and no extra init or ephemeral container.
   Namespace-wide pod inventory also rejects owned pods hidden by missing labels. The converged pod
   UID plus complete named init/regular container image and runtime image-ID set is retained and must
   remain identical before and after Prometheus and before evidence. With `skip`, no relaxed discovery
   is used: the gate waits for and pins that same strict effective topology without deleting pods.
6. Restarts exactly the three task-manager Deployments and waits for them plus `django-web`.
   Because `kubectl rollout status` can return before an old pod finishes terminating, a second
   deadline-bounded convergence barrier polls the complete application inventory. Every rendered
   Deployment must report its exact desired, updated, ready, and available replicas and exactly one
   current-revision ReplicaSet. The gate binds Deployment UID -> current ReplicaSet UID -> Pod UID
   and waits until no pod remains under an older ReplicaSet, including pods with a
   `deletionTimestamp`. It never deletes those pods directly. Kubernetes may retain inert,
   zero-replica historical ReplicaSets for rollback history; they are accepted only when they own no
   remaining pod and are not themselves terminating. Hidden, unowned, substituted, malformed, or
   unexpected-container resources remain immediate failures rather than retryable rollout state.
7. Strictly rechecks that converged application topology without polling and verifies the full named
   init/regular container image and runtime image-ID set. This prevents a terminating or old-image
   pod from being omitted from final evidence. It also proves every generic Ray interpreter lacks an
   installed `django_ray` and sees the same RuntimeEnv archive, SHA-256, and remote-bootstrap member.
8. Verifies live readiness/liveness paths and `Host: django-ray.localhost`, a Ready web pod, and its
   restart count.
9. Requires unauthenticated enqueue/stats/metrics/executions requests to return `401`; requires the
   same protected reads to return `200` with the in-memory token; then polls only the fresh task via
   an exact `task_id` filter and `limit=1` until it reaches durable `SUCCEEDED` with `result_data=5`.
10. Reuses the checked-in Prometheus checker through the same proxy-disabled, redirect-rejecting
    local HTTP opener. It requires exactly one `django-ray`, one `ray-head`, and one target for every
    converged Ray worker, plus the absence of the removed `django-ray-worker` pool. The exact
    RayCluster UID/topology is rechecked before and after Prometheus and again before evidence.

Every subprocess and Kubernetes API request has a configurable upper bound. The defaults are 120
seconds for ordinary commands, 1,200 seconds for each Docker build or Kind image load, and 30 seconds
for an individual Kubernetes API request. Override them only for a reviewed slow local environment:

```powershell
uv run make k8s-final-gate `
  K8S_CONTEXT=docker-desktop `
  K8S_NAMESPACE=django-ray `
  K8S_RAY_RESTART=required `
  K8S_FINAL_GATE_EXTRA_ARGS="--command-timeout 180 --build-timeout 1800 --kubectl-request-timeout 45"
```

The KubeRay overlay intentionally runs upstream `rayproject/ray` images. The separately built
`django-ray-worker` image covers the legacy custom-Ray image build boundary and is revision-checked,
but it is not substituted into the generic KubeRay pods. Running image-ID checks apply to the
complete Django web, setup, task-manager, and generic-Ray pod container sets. The locally built image
ID is required for every source-bound container; upstream/helper containers retain their observed
runtime image IDs in the exact identity contract. Ray-node checks additionally prove the generic-image
and RuntimeEnv boundary.

## Evidence to retain

On success, copy the complete `=== Local KubeRay final gate evidence ===` block into the PR and amend
only the retained commit message to include it. The amend changes the commit SHA, so
`source_commit_at_run` is an invocation reference, not the durable source identity. Verify that the
recorded `source_tree` still equals `git rev-parse HEAD^{tree}` after the amend. Any tracked file or
tree change invalidates the run and requires a new gate. The block contains no token and is
deliberately concise. The gate prepares this block during its final identity check but emits neither
the final success line nor any evidence until its private workspace and kubeconfig have been removed
successfully. A workspace creation or cleanup failure therefore cannot leave a false passing block.
Every emitted line is at most 72 characters, so the complete block can be copied into a commit
without reflowing it. A long value is emitted as `key_parts=<count>` followed by ordered
`key_part_001=...` lines; concatenate the numbered parts without a separator to recover the original
value. Do not omit, wrap, or reorder those lines.

The block records:

- commit at run time, stable source tree, context, namespace, private kubeconfig digest, local API
  server, and pinned Docker endpoint;
- both unique tags and local image IDs;
- RuntimeEnv byte size and SHA-256;
- whether Ray was cold-restarted, the pinned RayCluster UID, converged head/worker counts, and the
  retained Ray pod UID/container/image identity-set SHA-256;
- ready replica counts for all application Deployments;
- unauthenticated/authenticated status summary plus the fresh task ID, `SUCCEEDED`, and result `5`;
- probe path/Host, web restart count, and Prometheus pool counts;
- the preservation statement.

Do not paraphrase a partial run as passing. A preflight-only result is useful preparation but is not
final-gate evidence.

## Failure diagnostics and recovery

Failures are labeled by layer: `preflight`, `images`, `apply`, `setup`, `workloads`, `ray`, `rollouts`,
`app-convergence`, `image-identity`, `runtime-env`, `probes`, `api-smoke`, `prometheus`, or
`final-identity`. After a Kubernetes mutation,
the command prints only bounded status plus the relevant tail of setup, Ray, application, or
Prometheus logs. Every line uses the same redacting emitter; sensitive kubeconfig and Secret command
failures suppress their captured output before the credential values could be registered, while
captured static kubeconfig and API credentials are registered before later command output is handled.
Non-command exception details are redacted before they are tail-bounded to 16,000 characters. A
truncated detail starts with `[truncated redacted error; original_characters=<count>]` so retained
diagnostics never present a silent partial value as the complete failure. Relevant Kubernetes
diagnostics run before private-workspace cleanup. Workspace creation and cleanup errors use the same
bounded, redacted failure contract; if cleanup also fails after a primary gate error, the primary
layer remains authoritative and the cleanup error is retained as separately bounded context.

Fix the named layer and rerun the same source tree with a new unique tag. Normal recovery must not
use `k8s-reset`, delete the namespace, delete PostgreSQL, delete a PVC, prune Docker, or remove other
local images. The gate itself never performs those actions. It only:

- applies namespace-confined prerequisites first and defers application/Ray workloads until setup;
- preserves the existing `Secret/django-ray-secret` rather than applying its checked-in placeholder;
- rolls the namespaced Prometheus Deployment so target checks use the applied configuration;
- deletes/recreates `Job/django-setup` with bounded waits;
- optionally deletes individually verified `RayCluster/ray` head/worker pod names with a bounded
  wait; and
- restarts the three named task-manager Deployments.

If a manifest change would require destructive data migration or cluster-wide mutation, stop and
review that change separately. It is outside this local final gate.

## Optional rendered browser check

Use a browser after the automated gate when a dashboard/template/static-assets row triggered it.
Confirm the version, token controls, statistics, Metrics, Executions, and browser console. Paste the
token manually into the page only in a trusted local session, reload, and confirm a protected action
works without another paste. Then select **Forget token**, reload again, and confirm protected actions
require a token. Do not automate token retrieval into browser logs, screenshots, recordings, or
artifacts, and do not include the token in a URL.

## Initial reference evidence

The July 21, 2026 recovery remains the reference that motivated this gate:

- Issues #144, #145, #146, and #147 captured the independently testable dashboard
  authentication, generic-Ray bootstrap, probe Host, and Prometheus ownership failures.
- PRs #148, #149, #150, and #151 independently repaired dashboard authentication, generic-Ray
  bootstrap, probe Host headers, and Prometheus target ownership.
- Rebase-merged `main` at `1cef8e6042ed0fe811cc9ee99b8332a75c887c75` was rebuilt as
  `django-ray:main-1cef8e6`.
- The setup Job rebuilt `/runtime-env/django-ray-source.zip` at 293,956 bytes; one Ray head and four
  workers were cold-replaced; every generic interpreter lacked `django_ray`.
- Fresh task `80310cfa-f453-4be4-a73e-86d0d9c92266` reached `SUCCEEDED` with result `5`, while the
  protected endpoints returned `401` without authentication.
- Both web probes used `Host: django-ray.localhost`; the web pod was Ready with zero restarts; the
  Prometheus result was `django-ray=1`, `ray-head=1`, and `ray-workers=4`, with no stale worker pool.

That historical evidence proves the recovery, not future commits. Each triggered change needs its
own clean-checkout gate result.
